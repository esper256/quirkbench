"""Private preparation uses real local Git, without modifying the approved source."""
import json
import os
from pathlib import Path
import stat
import pytest
from quirkbench import source_preparation as preparation
from quirkbench.contracts import Conflict,ContractError
from quirkbench.store import ArtifactStore
from test_source_capture import repository,git


def change_mode(path):
    # The starting mode depends on the host umask; always make a real mutation.
    before=stat.S_IMODE(path.stat().st_mode)
    path.chmod(before ^ stat.S_IXUSR)
    assert stat.S_IMODE(path.stat().st_mode)!=before


def prepare(repository,**kwargs):
    root,base,state=repository
    store=ArtifactStore(state/'artifacts',reserve_bytes=0)
    result=preparation.prepare(root,base,kwargs.pop('allowed',[]),state/kwargs.pop('stage','workers'),store,'workspace-one',
        writer_quiesced=kwargs.pop('quiesced',True),verify=kwargs.pop('verify',lambda:None),**kwargs)
    return result,state/'workers/output/workspace',store


def original_identity(root):
    return git(root,'status','--porcelain'),git(root,'rev-parse','HEAD'),(root/'.git/index').read_bytes(),(root/'.git/config').read_bytes()


def test_dirty_workspace_preserves_original_base_modes_deletions_and_index(repository):
    root,base,state=repository
    (root/'driver.c').write_text('edited contents\n');(root/'driver.c').chmod(0o640)
    (root/'removed.c').unlink();(root/'bin/helper').chmod(0o644)
    (root/'new-index.c').write_text('staged original addition\n');git(root,'add','new-index.c')
    (root/'approved.c').write_text('approved untracked\n');(root/'ignored.o').write_text('excluded output\n')
    git(root,'config','remote.private.url','https://credential.invalid/private')
    before=original_identity(root)
    result,workspace,store=prepare(repository,allowed=['approved.c'])
    assert result['base_oid']==git(workspace,'rev-parse','HEAD')==base
    assert (workspace/'driver.c').read_bytes()==(root/'driver.c').read_bytes()
    assert (workspace/'driver.c').stat().st_mode&0o777==0o640
    assert not (workspace/'removed.c').exists() and not (workspace/'ignored.o').exists()
    assert (workspace/'new-index.c').read_bytes()==(root/'new-index.c').read_bytes()
    assert (workspace/'approved.c').is_file() and original_identity(root)==before
    assert 'credential.invalid' not in (workspace/'.git/config').read_text()
    assert json.loads(store.get(result['capture_sha256']))['complete']
    assert git(workspace,'status','--porcelain')


def test_relative_symlink_and_retained_distribution_provenance(repository):
    root,base,state=repository;(root/'approved.c').symlink_to('driver.c')
    store=ArtifactStore(state/'artifacts',reserve_bytes=0)
    provenance={'source_package_sha256':store.put(b'srpm').sha256,'distribution_patches_sha256':store.put(b'patch-list').sha256,'upstream_base_oid':'a'*40}
    result,workspace,_=prepare(repository,allowed=['approved.c'],provenance=provenance)
    assert os.readlink(workspace/'approved.c')=='driver.c' and result['provenance']==provenance


@pytest.mark.parametrize('phase',['source_workspace_base_checked_out','source_workspace_dirty_applied','source_workspace_verified'])
def test_interruption_preserves_original_and_fresh_worker_retry(repository,phase):
    root,base,state=repository;(root/'driver.c').write_text('retained dirty\n');before=original_identity(root)
    def fault(value):
        if value==phase:raise Conflict('interrupted worker')
    with pytest.raises(Conflict,match='interrupted'):prepare(repository,fault_hook=fault)
    assert original_identity(root)==before
    with pytest.raises(Conflict,match='already contains'):prepare(repository)
    result,_,_=prepare(repository,stage='retry-worker')
    assert (state/'retry-worker/output/workspace/driver.c').read_text()=='retained dirty\n'
    assert result['base_oid']==base and original_identity(root)==before


@pytest.mark.parametrize('directory',['original','workers','output','workspace'])
def test_replaced_named_directory_never_returns_prepared_workspace(repository,directory):
    root,base,state=repository
    def fault(phase):
        if phase=='source_workspace_verified':
            path={'original':root,'workers':state/'workers','output':state/'workers/output','workspace':state/'workers/output/workspace'}[directory]
            path.rename(path.with_name(path.name+'-retained'));path.mkdir(mode=0o700)
    with pytest.raises(Conflict,match='ownership changed'):prepare(repository,fault_hook=fault)


def test_unquiesced_writer_is_not_copied(repository):
    with pytest.raises(Conflict):prepare(repository,quiesced=False)
    assert not (repository[2]/'workers/output').exists()


def test_no_transport_or_templates_inherited_from_environment(repository,monkeypatch):
    root,base,state=repository
    template=state/'evil-template';template.mkdir(parents=True);(template/'config').write_text('[core]\n hooksPath=/evil\n')
    monkeypatch.setenv('GIT_TEMPLATE_DIR',str(template));monkeypatch.setenv('GIT_CONFIG_COUNT','1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0','core.sshCommand');monkeypatch.setenv('GIT_CONFIG_VALUE_0','false')
    result,workspace,_=prepare(repository)
    assert '/evil' not in (workspace/'.git/config').read_text() and result['base_oid']==base


@pytest.mark.parametrize('mutation',['extra','bad_base','bad_path','bad_version'])
def test_preparation_record_reader_is_strict(repository,mutation):
    value,_,_=prepare(repository)
    if mutation=='extra':value['untrusted']='x'
    elif mutation=='bad_base':value['base_oid']='main'
    elif mutation=='bad_path':value['workspace_path']='../original'
    else:value['schema_version']=True
    with pytest.raises(ContractError):preparation.validate(value)


def test_local_transport_requires_exact_fetch_form(repository):
    from quirkbench.source_capture import _git
    with pytest.raises(ContractError):_git(repository[0],['status'],lambda:None,local_fetch=True)


def test_all_base_files_deleted_are_reproduced(repository):
    root,base,state=repository
    (root/'driver.c').unlink();(root/'removed.c').unlink();(root/'bin/helper').unlink()
    result,workspace,_=prepare(repository)
    assert sorted(p.name for p in workspace.iterdir())==['.git'] and result['base_oid']==base


@pytest.mark.parametrize('mutation',['contents','mode','deletion','index','head'])
def test_late_workspace_mutation_cannot_match_original_capture(repository,mutation):
    root,base,state=repository
    def fault(phase):
        if phase!='source_workspace_verified':return
        path=state/'workers/output/workspace'
        if mutation=='contents':(path/'driver.c').write_text('late mutation')
        elif mutation=='mode':change_mode(path/'driver.c')
        elif mutation=='deletion':(path/'driver.c').unlink()
        elif mutation=='index':(path/'injected.c').write_text('new');git(path,'add','injected.c')
        else:git(path,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','late head')
    with pytest.raises((Conflict,ContractError)):prepare(repository,fault_hook=fault)
    assert git(root,'rev-parse','HEAD')==base


@pytest.mark.parametrize('mutation',['contents','mode','deletion','index','head'])
def test_last_claim_callback_mutation_is_caught_without_later_callbacks(repository,mutation,monkeypatch):
    root,base,state=repository
    import quirkbench.source_capture as capture
    original=capture._scope;calls=[0];armed=[False]
    def scope(path,*args):
        value=original(path,*args)
        if path==state/'workers/output/workspace':
            calls[0]+=1
            if calls[0]==3:armed[0]=True
        return value
    monkeypatch.setattr(capture,'_scope',scope)
    fired=[False]
    def verify():
        path=state/'workers/output/workspace'
        if fired[0] or not armed[0]:return
        fired[0]=True
        if mutation=='contents':(path/'driver.c').write_text('late callback mutation')
        elif mutation=='mode':change_mode(path/'driver.c')
        elif mutation=='deletion':(path/'driver.c').unlink()
        elif mutation=='index':(path/'injected.c').write_text('new');git(path,'add','injected.c')
        else:git(path,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','late callback head')
    with pytest.raises((Conflict,ContractError)):prepare(repository,verify=verify)
    assert fired[0]


@pytest.mark.parametrize('arguments',[
    ['fetch','--quiet','--no-tags','--no-recurse-submodules','--upload-pack=false'],
    ['fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=2','--','/tmp','a'*40],
    ['fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=1','--','/tmp','main'],
])
def test_local_fetch_rejects_extra_execution_options_and_mutable_refs(repository,arguments):
    from quirkbench.source_capture import _git
    with pytest.raises(ContractError):_git(repository[0],arguments,lambda:None,local_fetch=True)


@pytest.mark.parametrize('loss',['claim','space','destination'])
def test_extraction_checks_each_chunk_before_further_writes(tmp_path,loss,monkeypatch):
    import io,tarfile
    from quirkbench.build_pipeline import _extract_archive
    from quirkbench.build import BuildError
    archive=tmp_path/'source.tar'
    with tarfile.open(archive,'w') as handle:
        member=tarfile.TarInfo('source/driver.c');member.size=4*1024**2
        handle.addfile(member,io.BytesIO(b'x'*member.size))
    destination=tmp_path/'extraction';fired=[False]
    def verify():
        path=destination/'source/driver.c'
        if fired[0] or not path.exists() or path.stat().st_size<1024**2:return
        fired[0]=True
        if loss=='claim':raise Conflict('owner ended during extraction')
        if loss=='space':monkeypatch.setattr('shutil.disk_usage',lambda _:type('Usage',(),{'free':0})())
        else:destination.rename(tmp_path/'retained');destination.mkdir(mode=0o700)
    with pytest.raises((Conflict,BuildError)):_extract_archive(archive,destination,reserve_bytes=0,verify=verify)
    assert fired[0]
    retained=(tmp_path/'retained' if loss=='destination' else destination)/'source/driver.c'
    assert retained.stat().st_size<=1024**2


def test_sha256_git_base_is_preserved_in_private_repository(repository):
    root,base,state=repository
    alternate=root.with_name('sha256-source');alternate.mkdir()
    git(alternate,'init','--quiet','--object-format=sha256')
    (alternate/'driver.c').write_text('sha256 base')
    git(alternate,'add','.')
    git(alternate,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','sha256 original')
    base=git(alternate,'rev-parse','HEAD');assert len(base)==64
    (alternate/'driver.c').write_text('sha256 edited')
    result,workspace,_=prepare((alternate,base,state))
    assert git(workspace,'rev-parse','--show-object-format')=='sha256'
    assert result['base_oid']==base and (workspace/'driver.c').read_text()=='sha256 edited'


def test_force_added_ignored_source_remains_captured_without_original_index_change(repository):
    root,base,state=repository
    (root/'.gitignore').write_text('generated.c\n');git(root,'add','.gitignore')
    (root/'generated.c').write_text('explicit source despite ignore')
    git(root,'add','--force','generated.c');before=original_identity(root)
    result,workspace,_=prepare(repository)
    assert (workspace/'generated.c').read_text()=='explicit source despite ignore'
    assert 'generated.c' in git(workspace,'ls-files').splitlines()
    assert original_identity(root)==before and result['base_oid']==base


def test_special_git_metadata_is_refused_before_a_blocking_read(repository):
    root,base,state=repository
    def fault(phase):
        if phase=='source_workspace_verified':
            path=state/'workers/output/workspace/.git/index';path.unlink();os.mkfifo(path)
    with pytest.raises(ContractError,match='metadata is not bounded'):prepare(repository,fault_hook=fault)


def test_extraction_late_nested_parent_link_cannot_create_outside_file(tmp_path):
    import io,tarfile
    from quirkbench.build_pipeline import _extract_archive
    from quirkbench.build import BuildError
    archive=tmp_path/'source.tar';destination=tmp_path/'extraction';outside=tmp_path/'outside';outside.mkdir()
    with tarfile.open(archive,'w') as handle:
        for name in ('source/nested/first.c','source/nested/second.c'):
            member=tarfile.TarInfo(name);member.size=1;handle.addfile(member,io.BytesIO(b'x'))
    fired=[False]
    def verify():
        nested=destination/'source/nested'
        if not fired[0] and (nested/'first.c').exists():
            fired[0]=True;nested.rename(destination/'retained');nested.symlink_to(outside,target_is_directory=True)
    with pytest.raises(BuildError,match='link'):_extract_archive(archive,destination,reserve_bytes=0,verify=verify)
    assert fired[0] and not list(outside.iterdir())


def test_extraction_replaced_regular_parent_stops_writing_moved_file(tmp_path):
    import io,tarfile
    from quirkbench.build_pipeline import _extract_archive
    from quirkbench.build import BuildError
    archive=tmp_path/'source.tar';destination=tmp_path/'extraction';outside=tmp_path/'outside';outside.mkdir()
    with tarfile.open(archive,'w') as handle:
        member=tarfile.TarInfo('source/nested/driver.c');member.size=4*1024**2
        handle.addfile(member,io.BytesIO(b'x'*member.size))
    fired=[False]
    def verify():
        nested=destination/'source/nested';path=nested/'driver.c'
        if not fired[0] and path.exists() and path.stat().st_size>=1024**2:
            fired[0]=True;nested.rename(outside/'moved');nested.mkdir()
    with pytest.raises(BuildError,match='named output'):_extract_archive(archive,destination,reserve_bytes=0,verify=verify)
    assert fired[0] and (outside/'moved/driver.c').stat().st_size==1024**2


def test_extraction_parent_change_before_open_creates_no_moved_file(tmp_path):
    import io,tarfile
    from quirkbench.build_pipeline import _extract_archive
    from quirkbench.build import BuildError
    archive=tmp_path/'source.tar';destination=tmp_path/'extraction';outside=tmp_path/'outside';outside.mkdir()
    with tarfile.open(archive,'w') as handle:
        member=tarfile.TarInfo('source/nested/driver.c');member.size=1;handle.addfile(member,io.BytesIO(b'x'))
    fired=[False]
    def verify():
        nested=destination/'source/nested'
        if not fired[0] and nested.exists() and not (nested/'driver.c').exists():
            fired[0]=True;nested.rename(outside/'moved');nested.mkdir()
    with pytest.raises(BuildError,match='parent changed'):_extract_archive(archive,destination,reserve_bytes=0,verify=verify)
    assert fired[0] and not list((outside/'moved').iterdir())


def test_extraction_late_hardlink_cannot_change_outside_mode(tmp_path):
    import io,tarfile
    from quirkbench.build_pipeline import _extract_archive
    from quirkbench.build import BuildError
    archive=tmp_path/'source.tar';destination=tmp_path/'extraction';outside=tmp_path/'outside.c'
    outside.write_bytes(b'outside');outside.chmod(0o600)
    with tarfile.open(archive,'w') as handle:
        member=tarfile.TarInfo('source/driver.c');member.size=1024**2;member.mode=0o755
        handle.addfile(member,io.BytesIO(b'x'*member.size))
    observations=[0];fired=[False]
    def verify():
        path=destination/'source/driver.c'
        if fired[0] or not path.exists() or path.stat().st_size!=1024**2:return
        observations[0]+=1
        if observations[0]==4:
            fired[0]=True;path.unlink();os.link(outside,path)
    with pytest.raises(BuildError,match='named output'):_extract_archive(archive,destination,reserve_bytes=0,verify=verify,preserve_mode=True)
    assert fired[0] and outside.stat().st_mode&0o777==0o600 and outside.read_bytes()==b'outside'


def test_existing_output_link_is_rejected_before_outside_workspace_creation(repository):
    root,base,state=repository;outside=root.with_name('outside-output');outside.mkdir(mode=0o700)
    stage=state/'workers';stage.mkdir(mode=0o700,parents=True);(stage/'output').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ContractError,match='symlinks'):prepare(repository)
    assert not list(outside.iterdir())
