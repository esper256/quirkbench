"""Bazzite-specific baseline selection and target-tools image preparation."""
import json
from pathlib import Path
import re
import shutil

from .files import atomic_write, identifier, read_json, save_json, build_lock, disk
from .image import build_target_rpm, build_tools, tools_identity, check_packaging_source
from .runtime import files_hash, runtime_hash
from .files import digest_file
from .preparation import preparation_key

BASE = 'ghcr.io/ublue-os/bazzite:stable'
_BUILDER_PATH = Path(__file__)
_LOADED_BUILDER = _BUILDER_PATH.read_bytes()


async def prepare_candidate(root, linux, work, progress):
    check_packaging_source()
    if _BUILDER_PATH.read_bytes() != _LOADED_BUILDER:
        raise ValueError('Candidate builder changed while this process was running. '
                         'Ask the controller operator to coordinate a restart before preparing software. '
                         'Existing target inspection and command access remain available.')
    with build_lock(root):
        return await _prepare_candidate(root, linux, work, progress)


async def _prepare_candidate(root, linux, work, progress):
    base = work['inputs']['base']
    if base != BASE and not re.fullmatch(r'ghcr.io/ublue-os/bazzite@sha256:[a-f0-9]{64}', base):
        raise ValueError('The first implementation supports Bazzite stable or an exact Bazzite image digest.')
    folder = root / 'candidates' / work['id']
    folder.mkdir(parents=True, exist_ok=True)
    log = root / 'logs' / (work['id'] + '.log')
    # A baseline selection is reused across retries, not re-resolved to a newer OS.
    selection_file = root / 'baselines' / identifier(work['scope']) / 'selection.json'
    selection = read_json(selection_file)
    if selection and selection['requested'] != base:
        raise ValueError('This investigation already selected a different baseline. Start a new investigation to change it.')
    if not selection:
        progress('Downloading and recording the selected Bazzite version')
        await linux.command('podman', 'pull', base, log=log)
        info = await linux.command('podman', 'image', 'inspect', base)
        info = json.loads(info['output'])[0]
        digest = info['Digest']
        selection = {'requested': base, 'image': 'ghcr.io/ublue-os/bazzite@' + digest,
                     'version': info.get('Labels', {}).get('org.opencontainers.image.version', 'not reported')}
        save_json(selection_file, selection)
    changes = work['inputs'].get('changes', {})
    tools = await build_tools(root, linux, log)
    tools = await tools_identity(linux, tools)
    rpm = await build_target_rpm(root, linux, tools, log)
    preparation = files_hash(('bazzite.py', 'assets/bazzite/health-report', 'assets/bazzite/quirkbench-bazzite-health.service'))
    cache_key = preparation_key(selection, preparation + digest_file(rpm) + tools, changes)
    cache_file = root / 'cache' / 'prepared-candidates' / (cache_key + '.json')
    cached = read_json(cache_file)
    if cached and (root / 'repository' / 'objects' / cached['commit'][:2] / (cached['commit'][2:] + '.commit')).exists():
        progress('Reusing the prepared candidate')
        save_json(folder / 'candidate.json', cached)
        return cached
    shutil.copy2(rpm, folder / 'target.rpm')
    assets = Path(__file__).with_name('assets') / 'bazzite'
    shutil.copy2(assets / 'health-report', folder / 'health-report')
    shutil.copy2(assets / 'quirkbench-bazzite-health.service', folder / 'quirkbench-bazzite-health.service')
    additions = await disk(stage_changes, root, folder, changes)
    atomic_write(folder / 'Containerfile', f'''FROM {selection['image']}
COPY target.rpm /tmp/quirkbench-target.rpm
COPY health-report /usr/libexec/quirkbench-bazzite-health
COPY quirkbench-bazzite-health.service /usr/lib/systemd/system/quirkbench-bazzite-health.service
RUN rpm-ostree install /tmp/quirkbench-target.rpm && rm /tmp/quirkbench-target.rpm && \\
    chmod 755 /usr/libexec/quirkbench-bazzite-health && systemctl enable quirkbench-bazzite-health.service quirkbench-prepare.service quirkbench-target.service quirkbench-console.service && \\
    systemctl mask uupd.timer uupd.service bazzite-hardware-setup.service udisks2.service ublue-os-media-automount.service bootloader-update.service greenboot-healthcheck.service greenboot-rollback.service redboot-auto-reboot.service fwupd.service fwupd-refresh.service fwupd-refresh.timer rpm-ostreed-automatic.timer bootc-fetch-apply-updates.timer && \\
    ostree container commit
{additions}
''')
    tag = 'localhost/quirkbench-candidate:' + work['id']
    progress('Adding target connection support to Bazzite')
    await linux.command('podman', 'build', '-t', tag, str(folder), log=log)
    archive = folder / 'candidate.oci'
    progress('Exporting the prepared container image')
    await linux.command('podman', 'save', '--format', 'oci-archive', '-o', archive, tag, log=log)
    # Import in the tools container, so the controller host needs only Podman.
    progress('Importing candidate into the shared OSTree repository')
    commit = await import_candidate(root, linux, tools, folder, work['id'], log)
    result = dict(commit=commit, base=selection, source=runtime_hash(), inputs=changes, changes=['Quirkbench target RPM', 'Bazzite automatic bootloader updates disabled', 'Greenboot checks run as bounded diagnostics without reboot or rollback actions', 'Bazzite automatic hardware-setup deployment/reboot disabled', 'Automatic updates, automount and firmware update services disabled'])
    save_json(folder / 'candidate.json', result)
    save_json(cache_file, result)
    archive.unlink(missing_ok=True)
    return result



async def import_candidate(root, linux, tools, folder, work_id, log):
    """Import as container root so layered merges preserve ownership and modes."""
    script = '''set -eu
mkdir -p /import /publish
[ -f /import/config ] || ostree --repo=/import init --mode=bare
ostree container image pull --ostree-digestfile=/work/commit /import ostree-unverified-image:oci-archive:/work/candidate.oci
commit=$(cat /work/commit)
# Reject a conversion which lost root ownership or privileged executable modes.
ostree --repo=/import ls "$commit" /usr | grep -Eq '^d[0-7]+ +0 +0 '
ostree --repo=/import ls "$commit" /usr/bin/sudo | grep -Eq '^-[04]?[4-7][0-7]{3} +0 +0 '
[ -f /publish/config ] || ostree --repo=/publish init --mode=archive
ostree --repo=/publish pull-local /import "$commit"
ostree --repo=/publish refs --force --create=quirkbench/''' + work_id + ''' "$commit"
ostree --repo=/publish summary -u
printf '%s\\n' "$commit" > /work/commit
'''
    atomic_write(folder / 'import.sh', script)
    repo = root / 'repository'
    repo.mkdir(exist_ok=True)
    imported = root / 'cache' / 'candidate-import-bare'
    imported.mkdir(parents=True, exist_ok=True)
    await linux.command('podman', 'run', '--rm', '--user', '0', '--security-opt', 'label=disable', '-v', f'{folder.resolve()}:/work',
                        '-v', f'{imported.resolve()}:/import',
                        '-v', f'{repo.resolve()}:/publish', tools, 'bash', '/work/import.sh', log=log)
    commit = (folder / 'commit').read_text().strip()
    if not re.fullmatch('[a-f0-9]{64}', commit):
        raise ValueError('OSTree import did not produce a commit.')
    return commit


def stage_changes(root, folder, changes):
    """Bazzite/RPM-specific image edits; never execute agent probes on the controller."""
    lines = []
    for kind, operation in (('packages', 'install'), ('replacements', 'override replace')):
        names = []
        for index, item in enumerate(changes.get(kind, [])):
            name = f'{kind}-{index}.rpm'
            shutil.copy2(root / 'inputs' / item['sha256'], folder / name)
            lines.append(f'COPY {name} /tmp/{name}')
            names.append('/tmp/' + name)
        if names:
            files = ' '.join(names)
            lines.append(f'RUN rpm-ostree {operation} {files} && rm {files} && ostree container commit')
    probe = changes.get('startup_probe')
    if probe:
        shutil.copy2(root / 'inputs' / probe['sha256'], folder / 'startup-probe')
        atomic_write(folder / 'quirkbench-probe.service', f"""[Unit]
Description=Investigation startup probe
DefaultDependencies=no
After=local-fs.target quirkbench-prepare.service
Requires=quirkbench-prepare.service
Before=NetworkManager.service
[Service]
Type=oneshot
EnvironmentFile=/run/quirkbench-experiment.env
ExecStart=/usr/libexec/quirkbench-startup-probe
TimeoutStartSec={changes['startup_probe_timeout_seconds']}
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
""", mode=0o644)
        lines += ['COPY startup-probe /usr/libexec/quirkbench-startup-probe',
                  'COPY quirkbench-probe.service /usr/lib/systemd/system/quirkbench-probe.service',
                  'RUN chmod 755 /usr/libexec/quirkbench-startup-probe && systemctl enable quirkbench-probe.service && ostree container commit']
    return '\n'.join(lines)
