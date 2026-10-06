import pytest
from quirkbench.contracts import Conflict, ContractError, digest
from quirkbench.store import ArtifactStore

@pytest.mark.parametrize('stage',['before_publish','after_publish'])
def test_interrupted_publication_converges_without_partial_objects(tmp_path,stage):
    def fail(actual):
        if actual==stage: raise RuntimeError('simulated process death')
    store=ArtifactStore(tmp_path,reserve_bytes=0,fault_hook=fail)
    with pytest.raises(RuntimeError): store.put(b'complete artifact')
    recovered=ArtifactStore(tmp_path,reserve_bytes=0)
    obj=recovered.put(b'complete artifact')
    assert recovered.get(obj.sha256)==b'complete artifact'
    assert list(recovered.objects.iterdir())==[recovered.path(obj.sha256)]

def test_resumable_upload_and_lost_ack(tmp_path):
    store=ArtifactStore(tmp_path,reserve_bytes=0)
    raw=b'abcdef'; value=digest(raw)
    assert store.append_upload('upload',0,raw[:3],value,6)['offset']==3
    assert store.append_upload('upload',0,raw[:3],value,6)['offset']==3
    store=ArtifactStore(tmp_path,reserve_bytes=0)
    assert store.append_upload('upload',5,b'f',value,6)['offset']==3
    assert store.append_upload('upload',3,raw[3:],value,6)['complete']
    assert store.append_upload('upload',3,raw[3:],value,6)['complete']
    assert store.get(value)==raw
    with pytest.raises(Conflict): store.append_upload('upload',0,b'bad',value,6)

def test_invalid_and_conflicting_uploads(tmp_path):
    store=ArtifactStore(tmp_path,reserve_bytes=0)
    value=digest(b'correct')
    with pytest.raises(ContractError): store.append_upload('../escape',0,b'',value,0)
    with pytest.raises(ContractError): store.append_upload('one',-1,b'',value,0)
    with pytest.raises(ContractError): store.append_upload('bad-hash',0,b'incorrect',value,9)
    assert not store.path(value).exists()
    store.append_upload('immutable',0,b'cor',value,7)
    with pytest.raises(Conflict): store.append_upload('immutable',0,b'other',digest(b'other'),5)

def test_empty_upload_and_corrupt_object(tmp_path):
    store=ArtifactStore(tmp_path,reserve_bytes=0)
    assert store.append_upload('empty',0,b'',digest(b''),0)['complete']
    obj=store.put(b'valid')
    store.path(obj.sha256).write_bytes(b'tampered')
    with pytest.raises(ContractError): store.get(obj.sha256)
    with pytest.raises(ContractError): store.put(b'valid')


def test_streamed_artifact_is_same_object_and_bad_hash_never_publishes(tmp_path):
    store=ArtifactStore(tmp_path/'store',reserve_bytes=0)
    source=tmp_path/'large';source.write_bytes(b'symbol'*500000)
    artifact=store.put_file(source)
    assert artifact.sha256==digest(source.read_bytes())
    assert store.put_file(source)==artifact
    source.write_bytes(b'changed')
    with pytest.raises(ContractError):store.put_file(source,artifact.sha256)
    assert len(list(store.objects.iterdir()))==1


def test_atomic_updates_preserve_user_modes_and_new_secret_defaults(tmp_path):
    from quirkbench.store import atomic_write
    path = tmp_path / 'record'
    atomic_write(path, b'new secret')
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o640)
    atomic_write(path, b'updated data')
    assert path.read_bytes() == b'updated data'
    assert path.stat().st_mode & 0o777 == 0o640


def test_controller_reopen_preserves_existing_database_mode(tmp_path):
    from quirkbench.controller import Controller
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    path = controller.db_path
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o640)
    Controller(controller.root, reserve_bytes=0)
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('fault',['short','no_progress','enospc'])
def test_atomic_storage_failures_preserve_existing_record_and_candidate_visibility(tmp_path,monkeypatch,fault):
    import errno,os
    from quirkbench import store
    path=tmp_path/'candidate.json';store.atomic_write(path,b'previous-complete')
    original=os.fdopen
    class Writer:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):return self
        def __exit__(self,*args):self.handle.close()
        def __getattr__(self,name):return getattr(self.handle,name)
        def write(self,data):
            if fault=='enospc':raise OSError(errno.ENOSPC,'filesystem full')
            if fault=='no_progress':return 0
            return self.handle.write(data[:max(1,len(data)//2)])
    monkeypatch.setattr(os,'fdopen',lambda *a,**k:Writer(original(*a,**k)))
    if fault=='short':
        store.atomic_write(path,b'next-complete');assert path.read_bytes()==b'next-complete'
    else:
        with pytest.raises(OSError):store.atomic_write(path,b'partial-not-complete')
        assert path.read_bytes()==b'previous-complete'
    assert not list(tmp_path.glob('.pending-*'))


def test_inode_exhaustion_rejects_new_artifact_without_losing_existing(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from quirkbench.store import StoragePressure
    store=ArtifactStore(tmp_path,reserve_bytes=0);old=store.put(b'previous-unuploaded')
    monkeypatch.setattr('os.statvfs',lambda _:SimpleNamespace(f_files=100,f_favail=0))
    with pytest.raises(StoragePressure,match='inodes'):store.put(b'new')
    assert store.get(old.sha256)==b'previous-unuploaded'


@pytest.mark.parametrize('fault',['short','no_progress','enospc'])
def test_actual_file_cas_publication_handles_short_writes_and_keeps_prior_artifacts(tmp_path,monkeypatch,fault):
    import os,errno
    from quirkbench.contracts import digest
    store=ArtifactStore(tmp_path/'cas',reserve_bytes=0);prior=store.put(b'retained unuploaded')
    source=tmp_path/'source';source.write_bytes(b'new exact candidate bytes')
    original=os.fdopen
    class Writer:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):return self
        def __exit__(self,*args):self.handle.close()
        def __getattr__(self,name):return getattr(self.handle,name)
        def write(self,data):
            if fault=='no_progress':return 0
            if fault=='enospc':raise OSError(errno.ENOSPC,'injected full CAS')
            return self.handle.write(data[:max(1,len(data)//2)])
    monkeypatch.setattr(os,'fdopen',lambda *a,**k:Writer(original(*a,**k)))
    if fault=='short':
        value=store.put_file(source);assert value.size==len(source.read_bytes())
        assert store.get(value.sha256)==source.read_bytes()
    else:
        with pytest.raises(OSError):store.put_file(source)
        assert not store.path(digest(source.read_bytes())).exists()
    assert store.get(prior.sha256)==b'retained unuploaded'
    assert not list(store.objects.glob('.pending-*'))


def test_unreported_inode_pool_allows_verified_publication_and_preserves_byte_reserve(tmp_path,monkeypatch):
    from quirkbench.store import StoragePressure
    store=ArtifactStore(tmp_path,reserve_bytes=0)
    import os
    values=list(os.statvfs(tmp_path));values[5:8]=[0,0,0]
    monkeypatch.setattr('os.statvfs',lambda _:os.statvfs_result(values))
    artifact=store.put(b'exact recovery input')
    assert store.get(artifact.sha256)==b'exact recovery input'
    assert store.verify(artifact.sha256)==len(b'exact recovery input')
    store.reserve_bytes=10**30
    with pytest.raises(StoragePressure,match='reserve'):store.put(b'another input')
    assert store.get(artifact.sha256)==b'exact recovery input'
