"""Deterministic validation amplification and final mutation fences (issue #7)."""
from contextlib import contextmanager
import json
from pathlib import Path
import os

import pytest
from quirkbench import retarget_evidence as archive,retarget_local
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.store import atomic_write
from test_retarget_endpoint import (moved,prepare,activate,spool,received,publication,bound,args,issuer,initialized,
    test_scoped_archived_drain_uses_approved_endpoint_without_new_target_changes as _round_trip)
from test_retarget_local import NEW,CONFIG


@pytest.fixture(params=[0,8],ids=['shallow','deep'])
def tmp_path(tmp_path_factory,request):
    root=tmp_path_factory.mktemp('archived-validation')
    for index in range(request.param):
        root=root/('configured-private-state-level-'+str(index));root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def test_export_drain_replay_do_not_reconstruct_inside_individual_source_reads(moved,monkeypatch):
    captures=[0];inside=[False];phases=[];reads=[]
    native_capture=archive._capture_source;native_source=archive._source;native_context=archive._archived
    def capture(*a,**kw):
        assert not inside[0];inside[0]=True;captures[0]+=1
        try:return native_capture(*a,**kw)
        finally:inside[0]=False
    def source(*a,**kw):
        before=captures[0];value=native_source(*a,**kw)
        reads.append(captures[0]-before)
        assert reads[-1]==0,'per-file source reads must not recursively recapture the source'
        return value
    for name in ('completed','_history'):
        original=getattr(archive,name)
        def checked(*a,_original=original,**kw):
            assert not inside[0],'source capture callbacks must not reconstruct completion/history'
            return _original(*a,**kw)
        monkeypatch.setattr(archive,name,checked)
    @contextmanager
    def context(*a,**kw):
        before=captures[0];start=len(reads)
        with native_context(*a,**kw) as value:yield value
        phases.append({'operation':kw['operation'],'captures':captures[0]-before,'source_reads':len(reads)-start})
    monkeypatch.setattr(archive,'_capture_source',capture);monkeypatch.setattr(archive,'_source',source)
    monkeypatch.setattr(archive,'_archived',context)
    _round_trip(moved,monkeypatch)
    assert len(phases)==3 and [row['operation'] for row in phases]==['archived evidence export','archived evidence drain','archived evidence drain']
    assert all(row['source_reads']>=2 for row in phases)
    print('archived validation counts (export, drain, replay): '+json.dumps(phases,sort_keys=True))


@pytest.mark.parametrize('mutation',['old-key','new-bundle','journal-mode','journal-link'])
def test_last_native_capture_callback_cannot_change_private_source_or_current_completion(moved,monkeypatch,mutation):
    control=moved[0][1];prepare(moved);receipt=activate(moved);old=Path(receipt['original_archive'])
    directory=retarget_local._location(control,'retarget-1');source=json.loads((directory/'source.json').read_bytes())
    native_capture=archive._capture_source;changed=[False]
    def capture(control,intent,verify,**kw):
        calls=[0];final=len(source['files'])-(intent['schema_version']==3)
        def guarded():
            verify();calls[0]+=1
            if calls[0]==final and not changed[0]:
                if mutation=='old-key':atomic_write(old/'enrollment-pending/key.pem',b'changed original key')
                elif mutation=='new-bundle':atomic_write(directory/'enrollment/pending/activation-bundle/device.token',b'changed new credential')
                elif mutation=='journal-mode':(old/'agent/journal.json').chmod(0o644)
                else:os.link(old/'agent/journal.json',old/'agent/journal-alias')
                changed[0]=True
        return native_capture(control,intent,guarded,**kw)
    monkeypatch.setattr(archive,'_capture_source',capture)
    with pytest.raises((Conflict,ContractError)):
        archive.export_archived_plan(control,CONFIG,'retarget-1','changed-at-final-callback',verify_target=lambda:True,
            binding_reader=lambda:NEW,recovery_verifier=lambda _:True)
    assert changed[0] and not list((control/'evidence-drain').rglob('plan.json'))


def test_coherent_alternative_reader_attribution_cannot_replace_immutable_archive(moved,monkeypatch):
    control=moved[0][1];prepare(moved);receipt=activate(moved)
    source=json.loads((retarget_local._location(control,'retarget-1')/'source.json').read_bytes())
    native_source=archive._source
    def alternate(*a,**kw):
        result,journal,ca,identity=native_source(*a,**kw)
        journal=json.loads(canonical(journal));journal['pending']['boot_id']='different-original-boot'
        return result,journal,ca,archive._source_identity(source,journal)
    monkeypatch.setattr(archive,'_source',alternate)
    with pytest.raises(Conflict,match='exact retained source identity'):
        archive.export_archived_plan(control,CONFIG,'retarget-1','alternative-source',verify_target=lambda:True,
            binding_reader=lambda:NEW,recovery_verifier=lambda _:True)
    assert not list((control/'evidence-drain').rglob('plan.json'))


@pytest.mark.parametrize('operation',['export','drain'])
def test_archived_deadline_names_operation_without_resetting_budget(operation):
    times=iter([0,121])
    kwargs={'verify_target':lambda:pytest.fail('expired operation touched storage'),'clock':lambda:next(times)}
    with pytest.raises(Conflict,match='archived evidence '+operation+' total deadline expired'):
        if operation=='export':archive.export_archived_plan(Path('/unopened'),None,'retarget-1','plan',**kwargs)
        else:archive.drain_archived(Path('/unopened'),None,'retarget-1','plan','grant',**kwargs)
