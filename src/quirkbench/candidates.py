"""First OS implementation: derive Bazzite, publish OSTree, deploy a fresh run."""
import hashlib
import json
from pathlib import Path
import re
import shutil

from .files import atomic_write, identifier, read_json, save_json, build_lock
from .image import build_target_rpm, build_tools, source_hash

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
    systemctl mask udisks2.service fwupd.service fwupd-refresh.service fwupd-refresh.timer rpm-ostreed-automatic.timer bootc-fetch-apply-updates.timer && \\
    ostree container commit
''')
    tag = 'localhost/quirkbench-candidate:' + work['id']
    progress('Adding target connection support to Bazzite')
    await linux.command('podman', 'build', '-t', tag, str(folder), log=log)
    archive = folder / 'candidate.oci'
    await linux.command('podman', 'save', '--format', 'oci-archive', '-o', archive, tag, log=log)
    # Import in the tools container, so the controller host needs only Podman.
    progress('Importing candidate into the shared OSTree repository')
    script = '''set -eu
mkdir -p /work/import /publish
ostree --repo=/work/import init --mode=bare-user
ostree container image pull /work/import ostree-unverified-image:oci-archive:/work/candidate.oci
refs=$(ostree --repo=/work/import refs --list ostree/container/image)
[ "$(printf '%s\\n' "$refs" | wc -l)" = 1 ]
commit=$(ostree --repo=/work/import rev-parse "$refs")
[ -f /publish/config ] || ostree --repo=/publish init --mode=archive
ostree --repo=/publish pull-local /work/import "$commit"
ostree --repo=/publish refs --create=quirkbench/''' + work['id'] + ''' "$commit"
ostree --repo=/publish summary -u
printf '%s\\n' "$commit" > /work/commit
'''
    atomic_write(folder / 'import.sh', script)
    repo = root / 'repository'
    repo.mkdir(exist_ok=True)
    await linux.command('podman', 'run', '--rm', '--userns=keep-id', '-v', f'{folder.resolve()}:/work:Z',
                        '-v', f'{repo.resolve()}:/publish:Z', tools, 'bash', '/work/import.sh', log=log)
    commit = (folder / 'commit').read_text().strip()
    if not re.fullmatch('[a-f0-9]{64}', commit):
        raise ValueError('OSTree import did not produce a commit.')
    result = dict(commit=commit, base=selection, source=source_hash(), changes=['Quirkbench target RPM', 'Automatic updates, automount and firmware update services disabled'])
    save_json(folder / 'candidate.json', result)
    archive.unlink(missing_ok=True)
    shutil.rmtree(folder / 'import', ignore_errors=True)
    return result


async def deploy_candidate(data: Path, boot: Path, linux, request, settings, progress):
    run_id = identifier(request['run_id'])
    commit = request['commit']
    if not re.fullmatch('[a-f0-9]{64}', commit):
        raise ValueError('Expected an exact OSTree commit.')
    media = read_json(data / 'quirkbench' / 'media.json')
    if not media:
        raise ValueError('Missing USB layout record. Prepare this USB with quirkbench recovery flash.')
    log = data / 'quirkbench' / 'logs' / (run_id + '-deploy.log')
    (data / 'boot').mkdir(parents=True, exist_ok=True)
    mounted = await linux.command('mountpoint', '-q', data / 'boot', check=False)
    if mounted['exit_code']:
        await linux.command('mount', '--bind', boot, data / 'boot', log=log)
    repo = data / 'ostree' / 'repo'
    if not (repo / 'config').exists():
        await linux.command('ostree', 'admin', f'--sysroot={data}', 'init-fs', data, log=log)
    await linux.command('ostree', f'--repo={repo}', 'config', 'set', 'sysroot.bootloader', 'none', log=log)
    await linux.command('ostree', f'--repo={repo}', 'config', 'set', 'sysroot.readonly', 'false', log=log)
    ca = data / 'quirkbench' / 'settings' / 'controller.crt'
    await linux.command('ostree', f'--repo={repo}', 'remote', 'add', '--if-not-exists', '--no-gpg-verify',
                        f'--set=tls-ca-path={ca}', 'controller', settings['url'] + '/ostree', log=log)
    progress('Downloading missing candidate content')
    # The token is passed to the child, never printed in a log or MCP response.
    await linux.command('ostree', f'--repo={repo}', 'pull', f'--http-header=Authorization=Bearer {settings["token"]}',
                        'controller', commit, log=log)
    progress('Creating fresh run files')
    os_root = data / 'ostree' / 'deploy' / run_id
    if os_root.exists():
        raise ValueError('This run already has a deployment. Inspect interrupted work instead of overwriting it.')
    await linux.command('ostree', 'admin', f'--sysroot={data}', 'os-init', run_id, log=log)
    await linux.command('ostree', 'admin', f'--sysroot={data}', 'deploy', f'--os={run_id}', '--no-merge', '--retain',
                        '--karg-none', f'--karg=root=UUID={media["data_uuid"]}', '--karg=rw',
                        '--karg=panic=10', '--karg=rd.shell=0', '--karg=rd.emergency=reboot',
                        f'--karg=quirkbench.run={run_id}', f'--karg=quirkbench.data={media["data_uuid"]}',
                        '--karg=selinux=0', '--karg=noresume', '--karg=rd.driver.blacklist=nvme,ahci,mmc_block',
                        '--karg=modprobe.blacklist=nvme,ahci,mmc_block', commit, log=log)
    entries = list((data / 'boot' / 'loader' / 'entries').glob('*.conf'))
    matching = [p for p in entries if f'quirkbench.run={run_id}' in p.read_text()]
    if len(matching) != 1:
        raise ValueError('Expected one OSTree boot entry for the new run; recovery remains selected.')
    # Fedora GRUB reads the standard OSTree BLS entry. We do not generate kernel entries.
    entry_id = matching[0].stem
    identifier(entry_id)
    progress('Candidate files prepared')
    await linux.command('sync', log=log)
    return {'entry': entry_id, 'run_id': run_id}


async def cleanup_run(data, linux, run_id):
    identifier(run_id)
    status = await linux.command('ostree', 'admin', f'--sysroot={data}', 'status')
    deployments = []
    for line in status['output'].splitlines():
        match = re.match(r'^([ *])\s*(\S+)\s+([a-f0-9]{64})\.\d+', line)
        if match:
            deployments.append((match[1], match[2]))
    indices = [i for i, (active, name) in enumerate(deployments) if name == run_id and active != '*']
    if any(active == '*' and name == run_id for active, name in deployments):
        raise ValueError('Cannot remove a booted candidate.')
    for i in reversed(indices):
        await linux.command('ostree', 'admin', f'--sysroot={data}', 'undeploy', str(i))
    # OSTree owns deployment removal; its per-OS /var is ours to discard afterwards.
    folder = data / 'ostree' / 'deploy' / run_id
    if (folder / 'deploy').exists() and any((folder / 'deploy').iterdir()):
        raise ValueError('Deployment still present; retaining run files.')
    shutil.rmtree(folder, ignore_errors=False) if folder.exists() else None
