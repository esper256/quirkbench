"""Target OSTree adapter: exact revisions, isolated attempts, no bootloader updates."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import re
import os
import selectors
import signal
import time
import shutil
import subprocess
from urllib.parse import urlsplit

from .kernel_policy import validate_kernel_config
from .contracts import ContractError, canonical, identifier, sha256
from .deployment import DeploymentManifest, PreparedDeployment
from .store import atomic_write, StoragePressure, sync_directory


# `ostree show --gpg-verify-remote` deliberately tolerates unsigned commits.
# Use the library's strict signature result instead of parsing human CLI output.
STRICT_SIGNATURE_SCRIPT = """import gi, sys
 gi.require_version('OSTree', '1.0')
 from gi.repository import Gio, OSTree
 repo = OSTree.Repo.new(Gio.File.new_for_path(sys.argv[1]))
 repo.open(None)
 result = repo.verify_commit_for_remote(sys.argv[2], sys.argv[3], None)
 if not result.require_valid_signature():
  raise SystemExit('no valid trusted signature')
 print('signature-valid')
""".replace('\n ', '\n')


@dataclass(frozen=True)
class Remote:
    url: str
    ca: Path
    public_key: Path
    client_cert: Path
    client_key: Path

    def __post_init__(self):
        url = urlsplit(self.url)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ContractError('deployment remote requires a configured HTTPS URL')
        for path in (self.ca, self.public_key, self.client_cert, self.client_key):
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ContractError('remote trust files must be absolute regular files')


def run(argv):
    return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=1800).stdout


class CommandRunner:
    """Bounded output and live activity for slow pulls and deployment commands."""
    def __init__(self, progress, guard, timeout_s=1800, diagnostic=None, *,
                 operation='OSTree command', phase='deployment-command',
                 failure_guidance='deployment remains unarmed'):
        self.progress, self.guard, self.timeout_s = progress, guard, timeout_s
        self.diagnostic = diagnostic or (lambda raw: None)
        self.operation, self.phase = operation, phase
        self.failure_guidance = failure_guidance

    def __call__(self, argv):
        start = time.monotonic()
        from .process_ownership import launch
        try:
            process = launch(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
        except OSError as exc:
            log = self.diagnostic(str(exc).encode())
            raise OSError(f'{self.operation} could not start (errno={exc.errno}); '
                          f'{self.failure_guidance}; diagnostic={log}') from exc
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        captured = bytearray()
        errors = bytearray()
        count = 0
        last_report = 0
        last_output = start
        try:
            while selector.get_map():
                if time.monotonic() - start > self.timeout_s:
                    raise TimeoutError(f'{self.operation} deadline exceeded')
                self.guard()
                for key, _ in selector.select(timeout=1):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    count += len(block)
                    last_output = time.monotonic()
                    if key.data == 'stderr':
                        errors.extend(block)
                        del errors[:-65536]
                    if key.data == 'stdout':
                        if len(captured) + len(block) > 16 * 1024**2:
                            log = self.diagnostic(bytes(errors))
                            raise ContractError(f'{self.operation} response exceeds bounded output size; '
                                                f'{self.failure_guidance}; diagnostic={log}')
                        captured.extend(block)
                if time.monotonic() - last_report >= 5:
                    now = time.monotonic()
                    self.progress(self.phase,
                                  f'{self.operation} output: {count} bytes; '
                                  f'last output {now - last_output:.0f}s ago; '
                                  f'elapsed {now - start:.0f}s; deadline in '
                                  f'{max(0, self.timeout_s - (now - start)):.0f}s; waiting for completion')
                    last_report = time.monotonic()
            code = process.wait(timeout=max(1, self.timeout_s - (time.monotonic() - start)))
            if code:
                log = self.diagnostic(bytes(errors))
                raise ContractError(f'{self.operation} failed with exit status {code}; '
                                    f'{self.failure_guidance}; diagnostic={log}')
            return captured.decode('utf-8')
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            log = self.diagnostic(bytes(errors))
            raise TimeoutError(f'{self.operation} deadline exceeded; '
                               f'{self.failure_guidance}; diagnostic={log}') from exc
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            selector.close()
            process.stdout.close()
            process.stderr.close()


class OstreeBackend:
    def __init__(self, sysroot: Path, remotes: dict[str, Remote], *, verify_storage,
                 runner=None, progress=None, reserve_bytes=20 * 1024**3, can_remove=None):
        self.sysroot = Path(sysroot)
        if (not self.sysroot.is_absolute() or self.sysroot == Path('/')
                or self.sysroot.resolve() != self.sysroot or not self.sysroot.is_dir()):
            raise ContractError('OSTree adapter requires a real explicit USB sysroot')
        if verify_storage is None:
            raise ContractError('USB storage verification is mandatory')
        self.remotes = {identifier(k): v for k, v in remotes.items()}
        self.verify_storage = verify_storage
        self.progress = progress or (lambda phase, message: None)
        self.runner = runner or CommandRunner(self.progress, self._guard, diagnostic=self._diagnostic)
        self.reserve_bytes = reserve_bytes
        self.can_remove = can_remove or (lambda deployment: False)
        self.repo = self.sysroot / 'ostree/repo'

    def _safe(self, path, *, links=False, required=False):
        try:
            relative = path.relative_to(self.sysroot)
            if '..' in relative.parts:
                raise ValueError('parent traversal')
            if not links:
                current = self.sysroot
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError('symlink in privileged path')
            path.resolve(strict=required).relative_to(self.sysroot.resolve(strict=True))
        except (OSError, ValueError, RuntimeError) as exc:
            raise ContractError('deployment path escapes or is missing from USB sysroot') from exc
        return path

    def _diagnostic(self, raw):
        import uuid
        self.verify_storage(self.sysroot)
        folder = self._safe(self.sysroot / 'quirkbench/diagnostics')
        folder.mkdir(parents=True, exist_ok=True)
        path = self._safe(folder / (uuid.uuid4().hex + '.stderr.log'))
        atomic_write(path, raw)
        return str(path)

    def _guard(self):
        if self.sysroot.resolve() != self.sysroot:
            raise ContractError('USB sysroot identity changed')
        self.verify_storage(self.sysroot)
        for relative in ('quirkbench', 'quirkbench/attempts', 'ostree/repo', 'ostree/repo/config',
                         'ostree/repo/objects', 'ostree/deploy', 'boot'):
            self._safe(self.sysroot / relative)
        if shutil.disk_usage(self.sysroot).free < self.reserve_bytes:
            raise StoragePressure('USB deployment reserve reached')

    def _cmd(self, *args):
        self._guard()
        return self.runner(['ostree', *map(str, args)])

    @contextmanager
    def _lock(self):
        self._guard()
        folder = self._safe(self.sysroot / 'quirkbench')
        folder.mkdir(exist_ok=True)
        lockpath = self._safe(folder / 'deployment.lock')
        fd = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+b') as lock:
            deadline = time.monotonic() + 60
            next_report = 0
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self._guard()
                    now = time.monotonic()
                    if now >= deadline:
                        raise TimeoutError('deployment lock deadline exceeded; another preparation may still be active')
                    if now >= next_report:
                        self.progress('deployment-lock', f'Waiting for another preparation; deadline in {deadline - now:.0f}s')
                        next_report = now + 5
                    time.sleep(0.1)
            self._guard()
            yield

    def _attempt(self, attempt_id):
        return self._safe(self.sysroot / 'quirkbench/attempts' / identifier(attempt_id))

    def _json(self, path):
        self._safe(path, required=True)
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ContractError('invalid deployment metadata file')
        try:
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict):
                raise ValueError('object required')
            return value
        except (ValueError, TypeError) as exc:
            raise ContractError('invalid deployment metadata') from exc

    @staticmethod
    def _stateroot(attempt_id):
        return 'qb-' + hashlib.sha256(identifier(attempt_id).encode()).hexdigest()

    @staticmethod
    def _id(attempt_id, manifest):
        return hashlib.sha256(canonical({'attempt': attempt_id, 'manifest': manifest.sha256})).hexdigest()

    @staticmethod
    def _file_hash(path):
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest()

    def _validate_manifest(self, manifest):
        if manifest.protection_profile != 'usb-excluded-controllers-v1':
            raise ContractError('unsupported target protection profile')
        if manifest.repository not in self.remotes:
            raise ContractError('unconfigured deployment repository')
        release = manifest.provenance.get('kernel_release', '')
        if not isinstance(release, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}', release):
            raise ContractError('deployment needs a bounded kernel release identity')
        sha256(manifest.provenance.get('config_sha256'))
        artifacts = manifest.provenance.get('artifact_sha256')
        if not isinstance(artifacts, dict):
            raise ContractError('deployment requires boot artifact provenance')
        for role in ('kernel', 'initramfs'):
            sha256(artifacts.get(role))
        return release

    def _trust(self, manifest, *, configure=False):
        remote = self.remotes[manifest.repository]
        identity = {'url': remote.url}
        for name in ('ca', 'public_key', 'client_cert', 'client_key'):
            path = getattr(remote, name)
            if path.resolve() != path or not path.is_file() or path.is_symlink():
                raise ContractError('remote trust path changed')
            identity[name] = self._file_hash(path)
        folder = self._safe(self.sysroot / 'quirkbench/remotes')
        path = self._safe(folder / (manifest.repository + '.json'))
        keyring = self._safe(self.repo / (manifest.repository + '.trustedkeys.gpg'))
        previous = self._json(path) if path.exists() else None
        if previous and previous.get('identity') != identity:
            raise ContractError('remote trust changed; recommissioning is required')
        if previous and previous.get('keyring_sha256'):
            if not keyring.is_file() or self._file_hash(keyring) != previous['keyring_sha256']:
                raise ContractError('remote keyring changed; recommissioning is required')
        if not configure:
            if not previous or not previous.get('keyring_sha256'):
                raise ContractError('remote trust is not durably configured')
            return
        if previous is None:
            # Refuse to adopt unknown accumulated keys from a pre-existing remote.
            if keyring.exists():
                raise ContractError('unrecorded remote keyring requires recommissioning')
            folder.mkdir(parents=True, exist_ok=True)
            atomic_write(path, canonical({'identity': identity}))
        self._cmd('--repo=' + str(self.repo), 'remote', 'add', '--force',
                  '--gpg-import=' + str(remote.public_key), '--set=gpg-verify=true',
                  '--set=tls-permissive=false', '--set=tls-ca-path=' + str(remote.ca),
                  '--set=tls-client-cert-path=' + str(remote.client_cert),
                  '--set=tls-client-key-path=' + str(remote.client_key), manifest.repository, remote.url)
        self._safe(keyring, required=True)
        atomic_write(path, canonical({'identity': identity, 'keyring_sha256': self._file_hash(keyring)}))

    def _verify_commit(self, manifest, folder):
        release = self._validate_manifest(manifest)
        self._trust(manifest)
        self.progress('deployment-verify', 'Checking signature, object integrity and kernel protection')
        self._guard()
        signature = self.runner(['python3', '-c', STRICT_SIGNATURE_SCRIPT, str(self.repo), manifest.revision, manifest.repository])
        if signature.strip() != 'signature-valid':
            raise ContractError('strict signature verification did not succeed')
        self._cmd('--repo=' + str(self.repo), 'fsck')
        config = self._cmd('--repo=' + str(self.repo), 'cat', manifest.revision,
                           '/usr/lib/modules/' + release + '/config')
        config_file = self._safe(folder / 'kernel.config')
        atomic_write(config_file, config.encode())
        validate_kernel_config(config_file)
        if hashlib.sha256(config.encode()).hexdigest() != manifest.provenance['config_sha256']:
            raise ContractError('deployed config differs from manifest provenance')

    def _boot_entry(self, attempt_id, revision):
        osname = self._stateroot(attempt_id)
        entries = self._safe(self.sysroot / 'boot/loader/entries', links=True)
        matches = []
        for entry in entries.glob('*.conf'):
            self._safe(entry, links=True, required=True)
            if entry.is_symlink() or not entry.is_file() or entry.stat().st_size > 65536:
                raise ContractError('invalid boot entry file')
            lines = entry.read_text().splitlines()
            options = [line.split(None, 1)[1] for line in lines if line.startswith('options ')]
            for option in options:
                paths = [arg[7:] for arg in option.split() if arg.startswith('ostree=')]
                if len(paths) != 1 or not paths[0].startswith('/ostree/'):
                    continue
                link = self.sysroot / paths[0].lstrip('/')
                deployment = self._safe(link, links=True, required=True).resolve(strict=True)
                expected = self._safe(self.sysroot / 'ostree/deploy' / osname / 'deploy')
                if deployment.parent == expected and deployment.name == revision + '.0':
                    if not deployment.is_dir():
                        raise ContractError('deployment root missing')
                    matches.append(entry)
        if len(matches) > 1:
            raise ContractError('ambiguous prepared boot entries')
        return matches[0] if matches else None

    def _record(self, attempt_id):
        folder = self._attempt(attempt_id)
        path = self._safe(folder / 'prepared.json')
        if not path.exists():
            return None
        manifest = DeploymentManifest.from_dict(self._json(folder / 'intent.json'))
        self._validate_manifest(manifest)
        value = self._json(path)
        if set(value) != set(PreparedDeployment.__dataclass_fields__):
            raise ContractError('invalid prepared deployment fields')
        try:
            relative = Path(value['boot_entry'])
            if relative.is_absolute() or '..' in relative.parts or not str(relative).startswith('boot/loader'):
                raise ValueError('invalid relative boot path')
            value['boot_entry'] = self.sysroot / relative
            prepared = PreparedDeployment(**value)
        except (ValueError, TypeError) as exc:
            raise ContractError('invalid prepared deployment metadata') from exc
        if (prepared.attempt_id != attempt_id or prepared.manifest_digest != manifest.sha256
                or prepared.revision != manifest.revision or prepared.deployment_id != self._id(attempt_id, manifest)):
            raise ContractError('prepared deployment differs from recorded intent')
        return manifest, prepared

    @staticmethod
    def _same_identity(a, b):
        return (a.deployment_id, a.attempt_id, a.manifest_digest, a.revision) == (b.deployment_id, b.attempt_id, b.manifest_digest, b.revision)

    def _verify_boot_files(self, prepared, manifest):
        from .boot import read_boot_entry
        fields = read_boot_entry(prepared, self.sysroot)
        for field, role in (('linux', 'kernel'), ('initrd', 'initramfs')):
            path = self._safe(self.sysroot / ('boot' + fields[field]), links=True, required=True)
            if self._file_hash(path) != manifest.provenance['artifact_sha256'][role]:
                raise ContractError('deployed boot artifact differs from manifest provenance')

    def inspect(self, attempt_id):
        self._guard()
        record = self._record(attempt_id)
        if record is None:
            return None
        folder = self._attempt(attempt_id)
        if self._safe(folder / 'removed').exists():
            raise ContractError('attempt deployment was removed; use a new attempt ID')
        manifest, prepared = record
        self._verify_commit(manifest, folder)
        entry = self._boot_entry(attempt_id, manifest.revision)
        if entry is None:
            raise ContractError('prepared deployment no longer exists')
        # OSTree renumbers BLS entries. The durable identity does not depend on that alias.
        prepared = PreparedDeployment(prepared.deployment_id, attempt_id, manifest.sha256, manifest.revision, entry)
        self._verify_boot_files(prepared, manifest)
        return prepared

    def prepare(self, manifest: DeploymentManifest, attempt_id: str):
        self._validate_manifest(manifest)
        with self._lock():
            record = self._record(attempt_id)
            if record:
                if record[0].to_dict() != manifest.to_dict():
                    raise ContractError('attempt is already bound to a different deployment')
                return self.inspect(attempt_id)
            folder = self._attempt(attempt_id)
            folder.mkdir(parents=True, exist_ok=True)
            intent = self._safe(folder / 'intent.json')
            identity = canonical(manifest.to_dict())
            if intent.exists() and intent.read_bytes() != identity:
                raise ContractError('interrupted attempt has different deployment intent')
            atomic_write(intent, identity)
            self.progress('deployment-fetch', 'Fetching the authorized OSTree revision')
            if not (self.repo / 'config').exists():
                self._cmd('admin', 'init-fs', self.sysroot)
            self._cmd('--repo=' + str(self.repo), 'config', 'set', 'core.fsync', 'true')
            self._cmd('--repo=' + str(self.repo), 'config', 'set', 'sysroot.bootloader', 'none')
            self._cmd('--repo=' + str(self.repo), 'config', 'set', 'core.default-repo-finders', 'config')
            self._trust(manifest, configure=True)
            self._cmd('--repo=' + str(self.repo), 'pull', '--verbose', manifest.repository, manifest.revision)
            self._verify_commit(manifest, folder)
            self.progress('deployment-prepare', 'Preparing isolated system and configuration state')
            osname = self._stateroot(attempt_id)
            state = self._safe(self.sysroot / 'ostree/deploy' / osname)
            if not state.exists():
                self._cmd('admin', '--sysroot=' + str(self.sysroot), 'os-init', osname)
            entry = self._boot_entry(attempt_id, manifest.revision)
            if entry is None:
                self._cmd('admin', '--sysroot=' + str(self.sysroot), 'deploy', '--os=' + osname,
                          '--no-merge', '--no-prune', '--retain', '--not-as-default', '--karg-none',
                          '--karg=ro', '--karg=panic=10', '--karg=noresume', manifest.revision)
                entry = self._boot_entry(attempt_id, manifest.revision)
            if entry is None:
                raise ContractError('OSTree deployment produced no matching boot entry')
            prepared = PreparedDeployment(self._id(attempt_id, manifest), attempt_id, manifest.sha256, manifest.revision, entry)
            self._verify_boot_files(prepared, manifest)
            record = asdict(prepared)
            record['boot_entry'] = str(entry.relative_to(self.sysroot))
            atomic_write(self._safe(folder / 'prepared.json'), canonical(record))
            self.progress('deployment-ready', 'Verified deployment is ready for one-shot boot')
            return prepared

    def running_revision(self):
        root = Path('/sysroot/ostree/deploy')
        paths = [arg[7:] for arg in Path('/proc/cmdline').read_text().split() if arg.startswith('ostree=')]
        if len(paths) != 1:
            return None
        actual = (Path('/sysroot') / paths[0].lstrip('/')).resolve(strict=True)
        if not actual.is_relative_to(root):
            raise ContractError('running OSTree root is outside deployment tree')
        return sha256(actual.name.split('.')[0])

    def retain(self, deployment):
        with self._lock():
            current = self.inspect(deployment.attempt_id)
            if current is None or not self._same_identity(current, deployment):
                raise ContractError('cannot retain unknown deployment')
            atomic_write(self._safe(self._attempt(deployment.attempt_id) / 'retained'), b'1\n')

    def remove(self, deployment):
        with self._lock():
            record = self._record(deployment.attempt_id)
            if record is None or not self._same_identity(record[1], deployment):
                raise ContractError('cannot remove unknown deployment')
            folder = self._attempt(deployment.attempt_id)
            if self._safe(folder / 'retained').exists():
                raise ContractError('retained deployment cannot be removed')
            if not self.can_remove(deployment):
                raise ContractError('deployment cleanup requires reconciled evidence acknowledgement and disarmed boot state')
            if self._safe(folder / 'removed').exists():
                return
            status = json.loads(self._cmd('admin', '--sysroot=' + str(self.sysroot), 'status', '--json'))
            matching = [row for row in status['deployments']
                        if row['stateroot'] == self._stateroot(deployment.attempt_id) and row['checksum'] == deployment.revision]
            if len(matching) > 1 or any(row['booted'] or row['pinned'] or row['staged'] for row in matching):
                raise ContractError('active, pinned or ambiguous deployment cannot be removed')
            if matching:
                index = matching[0]['index']
                if type(index) is not int or index < 0:
                    raise ContractError('invalid deployment index')
                self._cmd('admin', '--sysroot=' + str(self.sysroot), 'undeploy', str(index))
            state = self._safe(self.sysroot / 'ostree/deploy' / self._stateroot(deployment.attempt_id))
            if state.exists():
                if list((state / 'deploy').glob('*')):
                    raise ContractError('stateroot still contains deployments')
                shutil.rmtree(state)
                sync_directory(state.parent)
            atomic_write(self._safe(folder / 'removed'), b'1\n')
