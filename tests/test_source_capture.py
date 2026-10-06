"""Full tracked source freeze without user commits or partial build inputs."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import pytest
from quirkbench import source_capture as source
from quirkbench.contracts import Conflict,ContractError
from quirkbench.store import ArtifactStore


def git(root,*argv):
    return subprocess.run(['git','-C',str(root),*argv],check=True,capture_output=True).stdout.decode().strip()


@pytest.fixture
def repository(tmp_path):
    root=tmp_path/'original';root.mkdir();git(root,'init','-q')
    (root/'driver.c').write_text('original\n');(root/'removed.c').write_text('old\n')
    (root/'bin').mkdir();(root/'bin/helper').write_text('original executable\n');(root/'bin/helper').chmod(0o755)
    git(root,'add','.');git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','actual original')
    return root,git(root,'rev-parse','HEAD'),tmp_path/'state'


def capture(repository,**kwargs):
    root,base,state=repository
    store=ArtifactStore(state/'artifacts',reserve_bytes=0)
    result=source.capture(root,base,kwargs.pop('allowed',['new.c']),state/'workers',store,
        writer_quiesced=kwargs.pop('quiesced',True),verify=kwargs.pop('verify',lambda:None),**kwargs)
    return result,store


def test_full_capture_modes_deletions_allowed_untracked_and_actual_oid(repository):
    root,base,state=repository
    (root/'driver.c').write_text('approved edit\n');(root/'removed.c').unlink();(root/'bin/helper').chmod(0o644)
    (root/'new.c').write_text('approved new\n');(root/'generated.o').write_text('excluded generated\n')
    before=git(root,'status','--porcelain');result,store=capture(repository)
    assert result['base_oid']==base and len(base)==40 and result['complete']
    entries=[json.loads(line) for line in store.get(result['manifest_sha256']).splitlines()]
    assert {e['path'] for e in entries}=={'driver.c','removed.c','bin/helper','new.c'}
    assert next(e for e in entries if e['path']=='removed.c')['kind']=='deleted'
    with tarfile.open(store.path(result['archive_sha256'])) as archive:
        assert archive.extractfile('source/driver.c').read()==b'approved edit\n'
        assert archive.getmember('source/bin/helper').mode==0o644
        assert 'source/removed.c' not in archive.getnames() and 'source/generated.o' not in archive.getnames()
    assert git(root,'status','--porcelain')==before and git(root,'rev-parse','HEAD')==base
    assert capture(repository)[0]==result


@pytest.mark.parametrize('mutation',['contents','mode','deletion','head','index','root'])
def test_concurrent_source_or_scope_change_never_publishes_receipt(repository,mutation):
    root,base,state=repository;fired=[False]
    def fault(phase):
        if phase=='source_archive_captured' and not fired[0]:
            fired[0]=True
            if mutation=='contents':(root/'driver.c').write_text('racing writer\n')
            elif mutation=='mode':(root/'bin/helper').chmod(0o600)
            elif mutation=='deletion':(root/'driver.c').unlink()
            elif mutation=='head':git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','changed base')
            elif mutation=='index':(root/'new-index.c').write_text('new');git(root,'add','new-index.c')
            else:root.rename(root.with_name('moved'));root.mkdir()
    with pytest.raises((Conflict,ContractError)):capture(repository,fault_hook=fault)
    # Failed captures may leave unreferenced CAS bytes, never a usable receipt.
    assert not list((state/'workers').iterdir())


@pytest.mark.parametrize('name',['.env','.ssh/key','credentials.json','../escape','/absolute','x/../driver.c'])
def test_unapproved_sensitive_or_escaping_paths_rejected(repository,name):
    with pytest.raises(ContractError):capture(repository,allowed=[name])


def test_capture_requires_explicit_writer_handoff_and_full_oid(repository):
    with pytest.raises(Conflict):capture(repository,quiesced=False)
    root,base,state=repository
    with pytest.raises(ContractError):capture((root,'main',state))
    with pytest.raises(Conflict):capture((root,'f'*40,state))


def test_streamed_large_file_is_bound_to_manifest_digest(repository):
    root,base,state=repository;(root/'new.c').write_bytes(b'abcdefgh'*300000)
    result,store=capture(repository)
    with tarfile.open(store.path(result['archive_sha256'])) as archive:
        entry=archive.getmember('source/new.c');assert entry.size==2400000
    manifest=[json.loads(line) for line in store.get(result['manifest_sha256']).splitlines()]
    entry=next(e for e in manifest if e['path']=='new.c')
    assert entry['sha256']==__import__('hashlib').sha256((root/'new.c').read_bytes()).hexdigest()


@pytest.mark.parametrize('kind',['escape','chain-escape','special'])
def test_unsafe_source_nodes_rejected(repository,kind):
    root,base,state=repository
    if kind=='escape':(root/'new.c').symlink_to('../outside')
    elif kind=='chain-escape':(root/'new.c').symlink_to('other');(root/'other').symlink_to('../outside')
    elif kind=='special':os.mkfifo(root/'new.c')
    else:os.link(root/'driver.c',root/'new.c')
    with pytest.raises(ContractError):capture(repository)


def test_internal_relative_link_modes_and_distribution_provenance(repository):
    root,base,state=repository;(root/'new.c').symlink_to('driver.c')
    store=ArtifactStore(state/'artifacts',reserve_bytes=0)
    provenance={'source_package_sha256':store.put(b'pinned source package').sha256,'distribution_patches_sha256':store.put(b'distribution patch manifest').sha256,'upstream_base_oid':'c'*40}
    result,store=capture(repository,provenance=provenance)
    assert result['provenance']==provenance
    with tarfile.open(store.path(result['archive_sha256'])) as archive:
        assert archive.getmember('source/new.c').issym() and archive.getmember('source/new.c').linkname=='driver.c'


def test_operation_claim_loss_during_stream_rejects_source(repository):
    root,base,state=repository;(root/'new.c').write_bytes(b'x'*3000000);alive=[True]
    def fault(phase):
        if phase=='source_file_captured':alive[0]=False
    def verify():
        if not alive[0]:raise Conflict('operation claim ended')
    with pytest.raises(Conflict):capture(repository,verify=verify,fault_hook=fault)
    assert not tuple((state/'artifacts/objects').iterdir())


@pytest.mark.parametrize('point',['source_verified','last-claim-check'])
def test_last_callbacks_cannot_change_bytes_behind_capture_receipt(repository,point):
    root,base,state=repository;late=[False]
    def fault(phase):
        if phase=='source_verified':
            late[0]=True
            if point=='source_verified':(root/'driver.c').write_text('changed after CAS publication')
    def verify():
        if late[0] and point=='last-claim-check':(root/'driver.c').write_text('changed in last owner callback')
    with pytest.raises(Conflict):capture(repository,verify=verify,fault_hook=fault)


def test_complete_source_schema_and_strict_runtime_reader(repository):
    from jsonschema import Draft202012Validator
    result,_=capture(repository)
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/source-capture.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(result);assert source.validate_capture(result)==result
    for patch in ({'schema_version':True},{'complete':False},{'base_oid':'a'*64+'x'},{'extra':'unsafe'},{'file_count':False}):
        with pytest.raises(ContractError):source.validate_capture(result|patch)


@pytest.mark.parametrize('target',['archive','manifest','stage','temporary','archive-alias'])
@pytest.mark.parametrize('phase',['source_archive_captured','source_verified'])
def test_serialized_source_or_stage_replacement_never_yields_complete_receipt(repository,target,phase):
    root,base,state=repository
    def fault(actual):
        if actual!=phase:return
        stage=state/'workers';temporary=next(stage.iterdir())
        if target in ('archive','manifest'):
            path=temporary/('source.tar' if target=='archive' else 'tree.jsonl')
            path.write_bytes(b'replacement bytes')
        elif target=='archive-alias':os.link(temporary/'source.tar',temporary/'alias')
        else:
            path=stage if target=='stage' else temporary
            path.rename(path.with_name(path.name+'-old'));path.mkdir(mode=0o700)
    with pytest.raises((Conflict,ContractError,OSError)):capture(repository,fault_hook=fault)


@pytest.mark.parametrize('stream',['stdout','stderr'])
def test_native_git_output_limits_terminate_and_reap_process(repository,stream,monkeypatch):
    import sys
    from quirkbench import process_ownership as retention
    processes=[]
    def launch(argv,**kw):
        assert 'core.fsmonitor=false' in argv and 'core.hooksPath=/dev/null' in argv
        assert kw['env']['GIT_NO_LAZY_FETCH']=='1' and kw['env']['GIT_ALLOW_PROTOCOL']=='' and kw['env']['GIT_NO_REPLACE_OBJECTS']=='1'
        process=subprocess.Popen([sys.executable,'-c','import sys; sys.'+stream+'.buffer.write(b"x"*100000); sys.'+stream+'.flush()'],**kw)
        processes.append(process);return process
    monkeypatch.setattr(retention,'launch',launch);monkeypatch.setattr(source,'MAX_LIST',1024)
    with pytest.raises(ContractError,match='bounded'):source._git(repository[0],['rev-parse','HEAD'],lambda:None)
    assert processes and processes[0].poll() is not None


def test_native_git_claim_loss_terminates_and_reaps_process(repository,monkeypatch):
    import sys
    from quirkbench import process_ownership as retention
    processes=[];checks=[0]
    def launch(argv,**kw):
        process=subprocess.Popen([sys.executable,'-c','import threading; threading.Event().wait(3)'],**kw)
        processes.append(process);return process
    def guard():
        checks[0]+=1
        if checks[0]>1:raise Conflict('claim lost during Git metadata')
    monkeypatch.setattr(retention,'launch',launch)
    with pytest.raises(Conflict):source._git(repository[0],['rev-parse','HEAD'],guard)
    assert processes and processes[0].poll() is not None


def test_source_capture_rejects_stage_symlink_without_erasing_boundary(repository):
    root,base,state=repository;real=state/'real';real.mkdir(parents=True,mode=0o700)
    alias=state/'stage';alias.symlink_to(real)
    with pytest.raises(ContractError):
        source.capture(root,base,[],alias,ArtifactStore(state/'artifacts',reserve_bytes=0),writer_quiesced=True,verify=lambda:None)
    assert not tuple(real.iterdir())


@pytest.mark.parametrize('stage',['relative-staging','/var/tmp/quirkbench-capture/../noncanonical'])
def test_raw_noncanonical_stage_is_rejected_before_path_helpers(repository,stage):
    root,base,state=repository
    with pytest.raises(ContractError,match='canonical'):
        source.capture(root,base,[],stage,ArtifactStore(state/'artifacts',reserve_bytes=0),writer_quiesced=True,verify=lambda:None)


def test_source_capture_reads_hardlinked_tracked_bytes(repository):
    root,base,state=repository
    os.link(root/'driver.c',root.parent/'extra-source-name')
    value=capture(repository)
    assert value
