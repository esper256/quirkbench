"""Bazzite-specific baseline selection and target-tools image preparation."""
import json
from pathlib import Path
import re
import shutil

from .files import atomic_write, identifier, read_json, save_json, build_lock
from .image import build_target_rpm, build_tools

BASE = 'ghcr.io/ublue-os/bazzite:stable'


async def prepare_candidate(root, linux, work, progress):
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
    tools = await build_tools(root, linux, log)
    rpm = await build_target_rpm(root, linux, tools, log)
    shutil.copy2(rpm, folder / 'target.rpm')
    atomic_write(folder / 'Containerfile', f'''FROM {selection['image']}
COPY target.rpm /tmp/quirkbench-target.rpm
RUN rpm-ostree install /tmp/quirkbench-target.rpm && rm /tmp/quirkbench-target.rpm && \\
    systemctl enable quirkbench-prepare.service quirkbench-target.service quirkbench-console.service && \\
    systemctl mask uupd.timer uupd.service bazzite-hardware-setup.service udisks2.service fwupd.service fwupd-refresh.service fwupd-refresh.timer rpm-ostreed-automatic.timer bootc-fetch-apply-updates.timer && \\
    ostree container commit
''')
    tag = 'localhost/quirkbench-candidate:' + work['id']
    progress('Adding target connection support to Bazzite')
    await linux.command('podman', 'build', '-t', tag, str(folder), log=log)
    archive = folder / 'candidate.oci'
    await linux.command('podman', 'save', '--format', 'oci-archive', '-o', archive, tag, log=log)
    # Import in the tools container, so the controller host needs only Podman.
    progress('Importing candidate into the shared OSTree repository')
    commit = await import_candidate(root, linux, tools, folder, work['id'], log)
    result = dict(commit=commit, base=selection, source=rpm.parent.parent.name, changes=['Quirkbench target RPM', 'Bazzite automatic hardware-setup deployment/reboot disabled', 'Automatic updates, automount and firmware update services disabled'])
    save_json(folder / 'candidate.json', result)
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
