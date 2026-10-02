"""Immutable development archive installation and idle, rollbackable activation.

Archive hashes establish identity/integrity, not publisher authenticity. This helper
preserves the unsigned development archive status and never installs host packages.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time

from .contracts import Conflict, ContractError, canonical
from .controller_archive import MAX_PACKAGE_BYTES
from .controller_service import UNIT, configuration, require_ready
from .product_contracts import _pairs
from .state_config import outside_checkout
from .state_reader import StateReader, read_file
from .store import atomic_write, sync_directory

LIMIT = 64 * 1024**2


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs)


def _home(explicit, env, suffix):
    path = Path(explicit or os.environ.get(env) or Path.home() / suffix).expanduser().resolve()
    return outside_checkout(path)


def _managed(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ContractError('managed installation paths cannot contain symlinks')
    outside_checkout(path)
    return path


@contextmanager
def _lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ContractError('installation lock must be regular')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict('installation/controller ownership is busy') from exc
        yield
    finally:
        os.close(fd)


def _verified_archive(archive, *, expected_archive_sha256=None, expected_version=None):
    path = Path(archive)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        raise ContractError('controller archive must be a bounded regular file')
    raw = read_file(path.parent, path.name, limit=LIMIT)
    archive_digest = hashlib.sha256(raw).hexdigest()
    if expected_archive_sha256 is not None and archive_digest != expected_archive_sha256:
        raise ContractError('controller archive changed or differs from authenticated release identity')
    files = {}; modes = {}; total = 0; prefix = None
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as source:
        for member in source:
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or '..' in name.parts
                    or '\\' in member.name or len(name.parts) < 2
                    or member.name != str(name) or member.mode & 0o7000):
                raise ContractError('unsafe controller archive member')
            prefix = prefix or name.parts[0]
            relative = str(PurePosixPath(*name.parts[1:]))
            if name.parts[0] != prefix or relative in files:
                raise ContractError('duplicate or mixed controller archive root')
            total += member.size
            if member.size < 0 or total > MAX_PACKAGE_BYTES or len(files) >= 8192:
                raise ContractError('controller archive exceeds expanded budget')
            files[relative] = source.extractfile(member).read()
            modes[relative] = member.mode
    manifest = _json(files.get('controller-manifest.json', b'{}'))
    fields = {'schema_version','version','requires_python','qualified','signed','wheel_sha256','files'}
    if (set(manifest) != fields or manifest['schema_version'] != 1
            or not isinstance(manifest['version'], str)
            or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,63}', manifest['version'])
            or prefix != 'quirkbench-controller-' + manifest['version']
            or manifest['signed'] is not False or manifest['qualified'] is not False
            or manifest['requires_python'] != '>=3.11'
            or not re.fullmatch('[0-9a-f]{64}', manifest['wheel_sha256'])
            or not isinstance(manifest['files'], dict)
            or set(manifest['files']) != set(files) - {'controller-manifest.json'}):
        raise ContractError('unsupported development controller manifest')
    required = {'bin/quirkbench','bin/quirkbench-controller-service','bin/quirkbench-job-worker',
                'bin/quirkbench-worker','lib/quirkbench/quirkbench-controller.service'}
    if not required <= files.keys():
        raise ContractError('archive lacks fixed runtime/service launchers')
    if expected_version is not None and manifest['version'] != expected_version:
        raise ContractError('controller archive changed or differs from authenticated release version')
    for name, expected in manifest['files'].items():
        if hashlib.sha256(files[name]).hexdigest() != expected:
            raise ContractError('controller archive checksum mismatch')
        if modes[name] != (0o755 if name == 'install' or name.startswith('bin/') else 0o644):
            raise ContractError('unexpected controller archive file mode')
    return manifest, files, archive_digest


def install(archive, *, data_home=None, expected_archive_sha256=None, expected_version=None):
    manifest, files, archive_digest = _verified_archive(archive,
        expected_archive_sha256=expected_archive_sha256, expected_version=expected_version)
    if ((expected_archive_sha256 is not None and archive_digest != expected_archive_sha256)
            or (expected_version is not None and manifest['version'] != expected_version)):
        raise ContractError('controller archive changed or differs from authenticated release identity')
    base = _managed(_home(data_home, 'XDG_DATA_HOME', '.local/share') / 'quirkbench/controller')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = base / (manifest['version'] + '-' + archive_digest)
    record = {'schema_version':1,'version':manifest['version'],'archive_sha256':archive_digest,
              'runtime_root':str(runtime),'signed':False,'qualified':False}
    expected_files = {**files, 'installation.json':canonical(record)}
    with _lock(base / '.install.lock'):
        if runtime.exists() or runtime.is_symlink():
            verify_installation(runtime, expected_files)
        else:
            stage = Path(tempfile.mkdtemp(prefix='.install-', dir=base))
            try:
                for name, raw in expected_files.items():
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    atomic_write(target, raw)
                    target.chmod(0o755 if name == 'install' or name.startswith('bin/') else 0o644)
                os.rename(stage, runtime)
                sync_directory(base)
            finally:
                if stage.exists(): shutil.rmtree(stage)
        atomic_write(base / 'last-installed.json', canonical(record))
    return record


def verify_installation(runtime, expected_files=None):
    runtime = Path(runtime)
    if runtime.is_symlink() or runtime.resolve() != runtime or not runtime.is_dir():
        raise ContractError('installation must be an existing canonical directory')
    manifest_raw = read_file(runtime, 'controller-manifest.json', limit=LIMIT)
    manifest = _json(manifest_raw)
    record_raw = read_file(runtime, 'installation.json', limit=4096)
    record = _json(record_raw)
    if (record.get('schema_version') != 1 or record.get('runtime_root') != str(runtime)
            or record.get('version') != manifest.get('version')
            or not re.fullmatch('[0-9a-f]{64}', record.get('archive_sha256',''))
            or runtime.name != record['version'] + '-' + record['archive_sha256']):
        raise ContractError('invalid installed software identity')
    names = set(manifest['files']) | {'controller-manifest.json','installation.json'}
    if expected_files is not None and names != set(expected_files):
        raise ContractError('installation contents differ from authenticated archive')
    actual = set()
    for directory, dirs, entries in os.walk(runtime, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ContractError('installation contains linked directory')
        for name in entries:
            actual.add(str((Path(directory) / name).relative_to(runtime)))
    if actual != names:
        raise ContractError('installation contents differ; refusing replacement')
    for name in names:
        raw = read_file(runtime, name, limit=LIMIT)
        mode = stat.S_IMODE((runtime/name).stat().st_mode)
        if mode != (0o755 if name == 'install' or name.startswith('bin/') else 0o644):
            raise ContractError('installation file mode differs')
        if name in manifest['files'] and hashlib.sha256(raw).hexdigest() != manifest['files'][name]:
            raise ContractError('installation bytes differ; refusing replacement')
        if expected_files is not None and raw != expected_files[name]:
            raise ContractError('installation bytes differ; refusing replacement')
    return record


def runtime_identity(executable):
    if not executable: return None
    path = Path(executable).resolve()
    root = path.parent.parent
    try:
        manifest = _json(read_file(root, 'controller-manifest.json', limit=LIMIT))
        record_path = root/'installation.json'
        record = _json(read_file(root, 'installation.json', limit=4096)) if record_path.exists() else {}
        return {'path':str(path),'version':manifest['version'],
                'manifest_sha256':hashlib.sha256(canonical(manifest)).hexdigest(),
                'archive_sha256':record.get('archive_sha256'),
                'managed':bool(record),'available':path.is_file()}
    except (OSError,ValueError,KeyError,TypeError):
        return {'path':str(path),'version':None,'manifest_sha256':None,
                'archive_sha256':None,'managed':False,'available':path.is_file()}


def installation_report(root, *, cli_executable=None, service_ready=False):
    configured = active = None
    try: configured = configuration(root)['runtime']
    except (OSError,ValueError): pass
    try:
        with StateReader(root).connection() as db:
            row = db.execute('SELECT runtime FROM controller_job_service WHERE id=1').fetchone()
            if row: active = row['runtime']
    except (OSError,ValueError,__import__('sqlite3').Error): pass
    cli_executable = cli_executable or Path(__file__).resolve().parents[2]/'bin/quirkbench'
    cli_id, configured_id, active_id = (runtime_identity(x) for x in (cli_executable,configured,active))
    identities = [i for i in (cli_id,configured_id,active_id) if i]
    mismatch = len({i['manifest_sha256'] or i['path'] for i in identities}) > 1
    return {'installations':{'cli':cli_id,'configured_service':configured_id,
                            'active_service':active_id if service_ready else None,
                            'last_advertised_service':active_id,'mismatch':mismatch}}


def _idle(root):
    """Refuse changes while a worker or unresolved physical journey exists."""
    with StateReader(root).connection() as db:
        busy = db.execute("SELECT 1 FROM operations WHERE state IN ('QUEUED','RUNNING','WAITING') OR worker_unit IS NOT NULL LIMIT 1").fetchone()
        busy = busy or db.execute("SELECT 1 FROM attempts WHERE state IN ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN') OR (handoff_revision IS NOT NULL AND recovery_returned IS NULL) LIMIT 1").fetchone()
        busy = busy or db.execute("SELECT 1 FROM jobs WHERE state IN ('QUEUED','ACTIVE') LIMIT 1").fetchone()
        busy = busy or db.execute("SELECT 1 FROM storage_groups WHERE state='RUNNING' AND stop_proof IS NULL LIMIT 1").fetchone()
    if busy: raise Conflict('outstanding work requires completion/pause and reconciliation before activation')


def _systemctl(runner, *args):
    result = runner(['systemctl','--user',*args], capture_output=True,text=True,check=False,timeout=20)
    if result.returncode:
        raise Conflict('user service action failed: ' + ' '.join(args))
    return result.stdout


def _stopped(runner):
    raw = _systemctl(runner,'show',UNIT,'--property=ActiveState','--property=MainPID')
    fields = dict(line.split('=',1) for line in raw.splitlines())
    if fields.get('ActiveState') not in ('inactive','failed') or fields.get('MainPID') != '0':
        raise Conflict('controller unit shutdown is unverified')


def _link(path, target):
    temporary = path.with_name('.'+path.name+'.install-link')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary,path)
    sync_directory(path.parent)


def _wait_ready(root, ready):
    deadline = time.monotonic()+12
    while True:
        try: return ready(root)
        except (OSError,ValueError):
            if time.monotonic() >= deadline: raise
            time.sleep(0.25)


def activate(record, root, *, config_home=None, bin_home=None,
             runner=subprocess.run, ready=require_ready):
    root = outside_checkout(Path(root))
    runtime = Path(record['runtime_root'])
    verify_installation(runtime)
    config = _home(config_home,'XDG_CONFIG_HOME','.config')
    launchers = _managed(outside_checkout(Path(bin_home or Path.home()/'.local/bin').resolve()))
    directory = _managed(config/'quirkbench')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    journal = directory/'installation-activation.json'
    unit = _managed(config/'systemd/user'/UNIT)
    link = launchers/'quirkbench'
    selection = directory/'installation.json'
    with _lock(directory/'.installation.lock'), _lock(root/'command.lock'):
        if journal.exists(): raise Conflict('unfinished activation; run controller-install --rollback first')
        old_config = configuration(root)
        _idle(root)
        # Refuse to overwrite a user-written executable or a different unit contract.
        if link.exists() and not link.is_symlink(): raise Conflict('launcher is not an installation symlink')
        old_unit = unit.read_bytes()
        text = old_unit.decode()
        matches = re.findall(r'^ExecStart=(.*)$',text,re.M)
        expected = old_config['runtime']+' --state '+str(root)
        if matches != [expected] or not re.search(r'^KillMode=control-group$',text,re.M):
            raise Conflict('unit differs from supported fixed controller service template')
        saved = {'schema_version':1,'state_root':str(root),'unit':str(unit),'launcher':str(link),
                 'old_unit':old_unit.decode(),'old_config':old_config,
                 'old_link':os.readlink(link) if link.is_symlink() else None,
                 'old_selection':selection.read_text() if selection.exists() else None,
                 'new_record':record,'phase':'PREPARED'}
        atomic_write(journal,canonical(saved))
        try:
            _systemctl(runner,'stop',UNIT)
            _stopped(runner)
            with _lock(root/'coordinator.lock'):
                # No startup, epoch advance or work-state mutation by the installer.
                _idle(root)
                new_config = dict(old_config)
                for name, executable in (('runtime','quirkbench-controller-service'),
                                         ('job_worker','quirkbench-job-worker'),('recovery_worker','quirkbench-worker')):
                    if name == 'recovery_worker' and name not in old_config: continue
                    new_config[name] = str(runtime/'bin'/executable)
                new_unit = text.replace('ExecStart='+expected,'ExecStart='+new_config['runtime']+' --state '+str(root)).encode()
                atomic_write(root/'private/controller-service.json',canonical(new_config))
                atomic_write(unit,new_unit)
                launchers.mkdir(parents=True,exist_ok=True,mode=0o700)
                _link(link,runtime/'bin/quirkbench')
                atomic_write(selection,canonical(record))
            _systemctl(runner,'daemon-reload')
            _systemctl(runner,'start',UNIT)
            status = _wait_ready(root,ready)
            saved['phase']='VERIFIED';atomic_write(journal,canonical(saved))
            atomic_write(directory/'last-activation.json',canonical(saved))
            journal.unlink();sync_directory(directory)
            return {**record,**status,'activated':True,'launcher':str(link)}
        except Exception:
            if journal.exists():
                _rollback(journal,root,runner,ready)
            else:
                _systemctl(runner,'start',UNIT)
            raise


def _rollback(journal,root,runner,ready):
    saved = _json(read_file(journal.parent,journal.name,limit=65536))
    _managed(Path(saved['unit']));_managed(Path(saved['launcher']).parent)
    if saved['state_root'] != str(root): raise Conflict('rollback belongs to another controller state')
    _idle(root)
    _systemctl(runner,'stop',UNIT);_stopped(runner)
    with _lock(root/'coordinator.lock'):
        _idle(root)
        atomic_write(root/'private/controller-service.json',canonical(saved['old_config']))
        atomic_write(Path(saved['unit']),saved['old_unit'].encode())
        link = Path(saved['launcher'])
        if saved['old_link'] is None:
            link.unlink(missing_ok=True)
            if link.parent.exists(): sync_directory(link.parent)
        else: _link(link,saved['old_link'])
        selection = journal.parent/'installation.json'
        if saved['old_selection'] is None: selection.unlink(missing_ok=True)
        else: atomic_write(selection,saved['old_selection'].encode())
    _systemctl(runner,'daemon-reload');_systemctl(runner,'start',UNIT)
    status = _wait_ready(root,ready)
    saved['phase']='ROLLED_BACK';atomic_write(journal.parent/'last-activation.json',canonical(saved))
    journal.unlink();sync_directory(journal.parent)
    return {**status,'rolled_back':True}


def rollback(root, *, config_home=None, runner=subprocess.run, ready=require_ready):
    directory = _managed(_home(config_home,'XDG_CONFIG_HOME','.config')/'quirkbench')
    with _lock(directory/'.installation.lock'), _lock(Path(root)/'command.lock'):
        return _rollback(directory/'installation-activation.json',Path(root).resolve(),runner,ready)
