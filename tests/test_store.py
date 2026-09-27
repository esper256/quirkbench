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
