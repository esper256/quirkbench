import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from quirkbench.contracts import CapabilityReport, Conflict, ContractError, canonical, digest
from quirkbench.controller import Controller
from quirkbench.library import LibraryManifest, LibrarySelection
from quirkbench.library_maintenance import install_requested, target_lock
from quirkbench.transport import HTTPSDeviceClient, make_server


@pytest.fixture
def maintenance(tmp_path, cert_files):
    controller = Controller(tmp_path/'controller', reserve_bytes=0)
    controller.register(CapabilityReport('target', 'recovery-1', [], mode='recovery'))
    controller.create_campaign('campaign','target')
    data = controller.store.put(b'model fixture'*200000)
    manifest = LibraryManifest('any', (), {'model.bin': {'sha256': data.sha256, 'size': data.size, 'executable': False}})
    pack = controller.store.put(canonical(manifest.to_dict())).sha256
    selection = controller.store.put(canonical(LibrarySelection((pack,)).to_dict())).sha256
    controller.library_maintenance('target', selection)
    cert, key = cert_files
    server = make_server(controller, certfile=str(cert), keyfile=str(key), device_tokens={'target':'A'*32})
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    client = HTTPSDeviceClient(f'https://localhost:{server.server_address[1]}','target','A'*32,str(cert),timeout=2)
    library = tmp_path/'library'; library.mkdir()
    mounts=[]; events=[]
    def install():
        return install_requested(client, state_dir=tmp_path/'control/agent', cache_root=tmp_path/'experiments/cache',
            library_root=library, verify_storage=lambda: True, set_writable=mounts.append,
            architecture='x86_64', reserve_bytes=0, event=lambda **r: events.append(r))
    yield controller, client, install, library, pack, selection, mounts, events, tmp_path
    server.shutdown(); server.server_close(); worker.join(2)


def test_fenced_install_is_repeatable_and_never_finishes_controller_fence(maintenance):
    controller, client, install, library, pack, selection, mounts, events, root = maintenance
    result = install()
    assert result['state'] == 'installed' and result['controller_fence'] == 'retained'
    assert (library/'packs'/pack/'content/model.bin').read_bytes().startswith(b'model fixture')
    assert mounts == [True, False]
    assert client.maintenance_status()['selection'] == selection
    assert install() == result
    with pytest.raises(Conflict, match='maintenance'):
        controller.resume('campaign')
    assert any(e['phase']=='library-download' and e['completed']==e['total'] for e in events)
    controller.library_maintenance('target', finish=True)
    controller.resume('campaign')


def test_revoked_fence_stops_copy_and_restores_readonly(maintenance):
    controller, client, install, library, pack, selection, mounts, events, root = maintenance
    original = client.maintenance_status
    count = 0
    def revoke():
        nonlocal count
        count += 1
        # Revoke only after rw transition and first file chunk check.
        if mounts and mounts[-1] is True and count > 15:
            controller.library_maintenance('target', finish=True)
        return original()
    client.maintenance_status = revoke
    with pytest.raises(ContractError, match='fence'):
        install()
    assert mounts[-1] is False
    assert not (library/'packs'/pack).exists()


def test_pending_journal_and_active_supervisor_block_maintenance(maintenance):
    controller, client, install, library, pack, selection, mounts, events, root = maintenance
    state = root/'control/agent'; state.mkdir(parents=True)
    (state/'journal.json').write_text(json.dumps({'pending':{'attempt_id':'active'}}))
    with pytest.raises(ContractError, match='pending'):
        install()
    (state/'journal.json').write_text(json.dumps({'pending':None}))
    with target_lock(state), pytest.raises(ContractError, match='supervisor'):
        install()
    assert mounts == []


def test_maintenance_backup_restores_complete_pack_closure_and_fence(maintenance):
    controller, client, install, library, pack, selection, mounts, events, root = maintenance
    backup = root/'backup'
    controller.backup(backup)
    restored = Controller.restore(backup, root/'restored', reserve_bytes=0)
    assert restored.maintenance_status('target')['selection'] == selection
    manifest = json.loads(restored.store.get(pack))
    assert restored.store.get(manifest['files']['model.bin']['sha256']).startswith(b'model fixture')
    with pytest.raises(Conflict, match='maintenance'):
        restored.resume('campaign')


def test_ranged_download_resumes_after_interruption(maintenance):
    controller, client, install, library, pack, selection, mounts, events, root = maintenance
    value = json.loads(controller.store.get(pack))['files']['model.bin']
    alias=root/'cache-alias';alias.symlink_to(root,target_is_directory=True)
    output = alias/'cache/object'
    def interrupt(**progress):
        raise OSError('lost network after synchronized chunk')
    with pytest.raises(OSError):
        client.download_artifact(value['sha256'],value['size'],output,progress=interrupt,reserve_bytes=0)
    assert not output.exists() and output.with_name('object.part').stat().st_size == 1024**2
    client.download_artifact(value['sha256'],value['size'],output,reserve_bytes=0)
    assert digest(output.read_bytes()) == value['sha256']


def test_large_artifact_range_beyond_old_256_mib_limit(maintenance):
    controller, client, install, library, pack, selection, mounts, events, root = maintenance
    # Sparse files avoid allocating a giant bytes object. Pretend an earlier run
    # already synchronized the zero prefix, then request only the remaining range.
    size = 257*1024**2
    source = root/'large-source'
    with source.open('wb') as stream:
        stream.truncate(size)
    with source.open('rb') as stream:
        value = hashlib.file_digest(stream,'sha256').hexdigest()
    os.rename(source,controller.store.path(value))
    manifest = LibraryManifest('any',(),{'large.bin':{'sha256':value,'size':size,'executable':False}})
    pack2 = controller.store.put(canonical(manifest.to_dict())).sha256
    selected = controller.store.put(canonical(LibrarySelection((pack2,)).to_dict())).sha256
    controller.library_maintenance('target',finish=True)
    controller.library_maintenance('target',selected)
    destination = root/'download/large'; destination.parent.mkdir()
    with destination.with_name('large.part').open('wb') as stream:
        stream.truncate(256*1024**2)
    progress=[]
    client.download_artifact(value,size,destination,progress=lambda **p: progress.append(p),reserve_bytes=0)
    assert destination.stat().st_size == size
    assert progress == [{'completed':size,'total':size,'unit':'bytes'}]
