"""Cached container-based recovery and target RPM builds. Never runs at import."""
import hashlib
import importlib.resources
import json
from pathlib import Path
import shutil

from .files import atomic_write, digest_file, read_json, save_json, build_lock

ASSETS = Path(str(importlib.resources.files('quirkbench') / 'assets'))
TOOLS_BASE = 'registry.fedoraproject.org/fedora:43'


def source_hash():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
            digest.update(p.relative_to(root).as_posix().encode() + b'\0' + p.read_bytes())
    return digest.hexdigest()


async def build_tools(root, linux, log):
    context = root / 'cache' / 'build-tools'
    context.mkdir(parents=True, exist_ok=True)
    selection = read_json(context / 'selection.json')
    if not selection:
        await linux.command('podman', 'pull', TOOLS_BASE, log=log)
        response = await linux.command('podman', 'image', 'inspect', TOOLS_BASE)
        info = json.loads(response['output'])[0]
        selection = {'image': TOOLS_BASE.split(':')[0] + '@' + info['Digest']}
        save_json(context / 'selection.json', selection)
    recipe = (ASSETS / 'tools.Containerfile').read_text().replace('@BASE@', selection['image'])
    key = hashlib.sha256(recipe.encode()).hexdigest()[:20]
    tag = 'localhost/quirkbench-tools:' + key
    found = await linux.command('podman', 'image', 'exists', tag, check=False)
    if found['exit_code']:
        atomic_write(context / 'Containerfile', recipe)
        await linux.command('podman', 'build', '-t', tag, context, log=log)
    return tag


async def build_target_rpm(root, linux, tools, log):
    folder = root / 'cache' / 'target-rpm' / source_hash()
    output = folder / 'output'
    existing = list(output.glob('*.rpm'))
    if len(existing) == 1 and (folder / 'complete.json').exists():
        return existing[0]
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(__file__).parent, folder / 'quirkbench', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(ASSETS / 'target.spec', folder / 'target.spec')
    output.mkdir(exist_ok=True)
    await linux.command('podman', 'run', '--rm', '--userns=keep-id', '-v', f'{folder.resolve()}:/work:Z',
                        tools, 'bash', '-ec', 'rpmbuild -bb --define "_topdir /tmp/rpmbuild" /work/target.spec; cp /tmp/rpmbuild/RPMS/noarch/*.rpm /work/output/', log=log)
    files = list(output.glob('*.rpm'))
    if len(files) != 1:
        raise ValueError('Target RPM build did not produce one package. Read the build log.')
    save_json(folder / 'complete.json', {'sha256': digest_file(files[0])})
    return files[0]


async def build_recovery(root, linux, force=False, progress=print):
    with build_lock(root):
        return await _build_recovery(root, linux, force, progress)


async def _build_recovery(root, linux, force, progress):
    log = root / 'logs' / 'recovery-build.log'
    progress(f'Preparing recovery build tools. Log: {log}')
    tools = await build_tools(root, linux, log)
    # The built tools image retains downloaded tool/package versions for this cache entry.
    info = await linux.command('podman', 'image', 'inspect', tools)
    tools_id = json.loads(info['output'])[0]['Id']
    key = hashlib.sha256((source_hash() + tools_id).encode()).hexdigest()[:24]
    selection_path = root / 'cache' / 'recovery-selections' / (key + '.json')
    selected = read_json(selection_path, {})
    result = root / 'images' / selected.get('id', key)
    existing = read_json(result / 'image.json')
    if existing and not force:
        progress(f'Recovery image {key} is already available. Use --force to rebuild.')
        return existing
    stage = root / 'images' / (key + '.building')
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    rpm = await build_target_rpm(root, linux, tools, log)
    shutil.copy2(rpm, stage / 'target.rpm')
    shutil.copy2(ASSETS / 'build-recovery.sh', stage / 'build.sh')
    progress('Installing recovery packages and preparing boot files')
    await linux.command('podman', 'run', '--rm', '--user', '0', '-v', f'{stage.resolve()}:/work:Z',
                        tools, 'bash', '/work/build.sh', log=log)
    files = ['recovery.img', 'BOOTX64.EFI', 'packages.txt']
    for name in files:
        if not (stage / name).is_file() or not (stage / name).stat().st_size:
            raise ValueError(f'Recovery build did not produce {name}. See {log}.')
    hashes = {name: digest_file(stage / name) for name in files}
    image_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:24]
    result = root / 'images' / image_id
    record = {'id': image_id, 'inputs': key, 'files': hashes,
              'source': source_hash(), 'tools_image': tools_id, 'secure_boot': False,
              'architecture': 'x86_64', 'boot_checked': False}
    save_json(stage / 'image.json', record)
    if result.exists():
        shutil.rmtree(stage)
    else:
        stage.rename(result)
    save_json(selection_path, {'id': image_id})
    progress(f'Recovery image {image_id} ready. Boot has not been checked. Next: quirkbench recovery flash')
    return record
