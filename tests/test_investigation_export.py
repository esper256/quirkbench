"""Atomic public export software, real local Git and injected native services."""
import json
import os
from pathlib import Path
import tarfile
import pytest
from quirkbench import investigation_export as export,source_capture,cli
from quirkbench.contracts import ContractError,Conflict
from quirkbench.store import ArtifactStore
from test_source_capture import repository,git
from test_investigation_context import lab,setup,observations
from test_investigation_report import populate
from test_external_proposals import captured
from test_distribution_source import prepared
from test_attended_baseline import published,joined,bounded_build,bounded_compose,candidate_setup,assembly_setup
from test_investigations import start


@pytest.fixture(autouse=True)
def tiny_export_policy(monkeypatch):
    # Tiny software fixtures don't assume the host has the production 20GiB
    # reserve. Reserve rejection is exercised separately with explicit evidence.
    monkeypatch.setattr(export,'reserve_bytes',lambda root:0)


class Objects:
    def __init__(self,store):self.store=store
    def path(self,identity):return self.store.path(identity)
    def verify(self,identity):return self.store.verify(identity)


def engine(repository,allowed=()):
    source,base,state=repository
    store=ArtifactStore(state/'artifacts',reserve_bytes=0)
    capture=source_capture.capture(source,base,list(allowed),state/'capture',store,writer_quiesced=True,verify=lambda:None)
    stage=state/'export';stage.mkdir(mode=0o700)
    bundle=stage/'bundle';bundle.mkdir(mode=0o700)
    for name in ('patches','reproduce'): (bundle/name).mkdir(mode=0o700)
    value=export.patch_series(source,capture,Objects(store),stage,bundle,'Actual Author <author@example.invalid>',lambda:None,reserve=0)
    return value,stage,bundle


def test_patch_reconstructs_additions_deletions_permissions_links_without_original_writes(repository):
    source,base,_=repository
    (source/'driver.c').write_bytes(b'edited\r\nbytes\n');(source/'driver.c').chmod(0o640)
    (source/'removed.c').unlink();(source/'bin/helper').chmod(0o644)
    (source/'new.c').write_bytes(b'new\n');(source/'link').symlink_to('driver.c')
    (source/'.gitattributes').write_text('*.c text eol=lf\n')
    before=(git(source,'status','--porcelain'),git(source,'rev-parse','HEAD'),(source/'.git/index').read_bytes())
    value,stage,bundle=engine(repository,['new.c','link','.gitattributes'])
    assert value['reconstruction_verified'] and value['base_oid']==base
    rebuilt=stage/'reconstructed'
    assert (rebuilt/'driver.c').read_bytes()==b'edited\r\nbytes\n'
    assert (rebuilt/'driver.c').stat().st_mode&0o777==0o640
    assert not (rebuilt/'removed.c').exists() and (rebuilt/'bin/helper').stat().st_mode&0o111==0
    assert os.readlink(rebuilt/'link')=='driver.c' and (rebuilt/'new.c').is_file()
    assert before==(git(source,'status','--porcelain'),git(source,'rev-parse','HEAD'),(source/'.git/index').read_bytes())
    patch=(bundle/'patches/0001-captured-changes.patch').read_text()
    assert 'From: Actual Author <author@example.invalid>' in patch and 'Signed-off-by' not in patch
    assert (bundle/'reproduce/base.bundle').is_file()


@pytest.mark.parametrize('author',[None,'invented','Name <secret\n@example.invalid>'])
def test_patch_requires_explicit_bounded_authorship(repository,author):
    with pytest.raises(ContractError):export.author_identity(author)


def manifest(path):
    with tarfile.open(path,'r:') as archive:
        return json.loads(archive.extractfile('manifest.json').read()),archive.getnames()


def test_report_only_inconclusive_atomic_public_output_excludes_private_control(lab,tmp_path):
    c,_=lab;identity=populate(c,attempts=2)
    checkout=tmp_path/'checkout';checkout.mkdir();(checkout/'.git').mkdir()
    alias=tmp_path/'alias';alias.symlink_to(checkout,target_is_directory=True)
    output=alias/'public.tar'
    receipt=export.export(c.root,'investigation',output)
    value,names=manifest(output)
    assert receipt['conclusion']=='inconclusive' and not receipt['source_reconstructed']
    assert value['complete'] and value['source'] is None and value['validation_status']=='unvalidated'
    assert {'README.md','report.md','manifest.json','experiments/report.json','evidence/'+identity}<=set(names)
    with tarfile.open(output) as archive:
        raw=b''.join(archive.extractfile(n).read() for n in names if archive.getmember(n).isfile())
    assert b'PRIVATE-TOKEN' not in raw and b'untrusted_secret' not in raw and b'controller.sqlite' not in names
    assert not value['native_qualification'] and not value['execution_authorized']
    with pytest.raises(Conflict):export.export(c.root,'investigation',output)


def test_captured_source_export_preserves_private_working_tree_and_produces_unvalidated_patch(captured,tmp_path):
    c,proposal,private,original=captured
    before=(git(private,'status','--porcelain'),git(original,'status','--porcelain'),(private/'.git/index').read_bytes())
    output=tmp_path/'captured.tar'
    receipt=export.export(c.root,'investigation',output,author='Author <author@example.invalid>')
    value,names=manifest(output)
    assert receipt['source_reconstructed'] and value['source']['reconstruction_verified']
    assert value['capture_sha256']==proposal['source']['capture_sha256'] and value['tested_source_attempts']==[]
    assert value['validation_status']=='unvalidated' and 'patches/0001-captured-changes.patch' in names
    assert before==(git(private,'status','--porcelain'),git(original,'status','--porcelain'),(private/'.git/index').read_bytes())


@pytest.mark.parametrize('stage',['verified','before_publish'])
def test_interrupted_output_never_appears_complete(lab,tmp_path,stage):
    c,_=lab;populate(c)
    output=tmp_path/'interrupted.tar'
    def fault(name):
        if name==stage:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):export.export(c.root,'investigation',output,fault=fault)
    assert not output.exists()


def test_missing_evidence_remains_original_attributed_missing(lab,tmp_path):
    c,_=lab;identity=populate(c);c.store.path(identity).unlink()
    output=tmp_path/'missing.tar';receipt=export.export(c.root,'investigation',output)
    value,names=manifest(output)
    assert receipt['missing_count']==1 and value['evidence'][0]['sha256']==identity
    assert not value['evidence'][0]['bytes_exported'] and 'evidence/'+identity not in names
    assert value['validation_status']=='unvalidated'


def test_cli_exports_existing_state_without_controller_initialization(lab,tmp_path,monkeypatch,capsys):
    c,_=lab;populate(c)
    def forbidden(*a,**kw):raise AssertionError('export initialized controller')
    monkeypatch.setattr('quirkbench.controller.Controller.__init__',forbidden)
    monkeypatch.setattr('quirkbench.maintenance.prune',forbidden)
    with c.transaction() as db:before=list(db.execute('SELECT * FROM refs ORDER BY owner,digest'))
    output=tmp_path/'cli.tar'
    assert cli.main(['investigation', 'results', 'export', 'investigation', '--output', str(output), '--json'], state_root=str(c.root))==0
    assert json.loads(capsys.readouterr().out)['data']['conclusion']=='inconclusive'
    with c.transaction() as db:assert before==list(db.execute('SELECT * FROM refs ORDER BY owner,digest'))


def test_empty_capture_reconstructs_exact_base_without_authored_changes(repository):
    value,stage,bundle=engine(repository)
    assert value['empty_changes'] and value['reconstruction_verified']
    assert (bundle/'patches/0001-captured-changes.patch').read_bytes()==b''


@pytest.mark.parametrize('mutation',['delete','replace','archive'])
def test_declared_or_sealed_output_tamper_cannot_publish(lab,tmp_path,mutation):
    c,_=lab;populate(c);output=tmp_path/'tampered.tar'
    def fault(phase):
        stage=next(tmp_path.glob('.quirkbench-export-*'))
        if phase=='verified' and mutation=='delete':(stage/'bundle/report.md').unlink()
        if phase=='verified' and mutation=='replace':(stage/'bundle/report.md').write_text('different')
        if phase=='before_publish' and mutation=='archive':(stage/'public.tar').write_bytes(b'truncated')
    with pytest.raises((Conflict,FileNotFoundError)):export.export(c.root,'investigation',output,fault=fault)
    assert not output.exists()


@pytest.mark.parametrize('kind',['unacknowledged','unretained','retired'])
def test_incidental_cas_owner_does_not_export_original_attempt_evidence(lab,tmp_path,kind):
    c,_=lab;identity=populate(c)
    with c.transaction() as db:
        db.execute('INSERT INTO refs VALUES(?,?)',('unrelated-private-owner',identity))
        if kind=='unacknowledged':db.execute('DELETE FROM evidence')
        elif kind=='unretained':db.execute("DELETE FROM refs WHERE owner='attempt:attempt-0-0'")
        else:db.execute("INSERT INTO storage_retired VALUES('attempt:attempt-0-0',0)")
    output=tmp_path/'original-owner.tar';export.export(c.root,'investigation',output)
    value,names=manifest(output)
    assert not value['evidence'][0]['bytes_exported'] and 'evidence/'+identity not in names
    assert value['missing'] and value['conclusion']=='inconclusive'


def test_retired_capture_cannot_borrow_unrelated_retained_source(captured,tmp_path):
    c,proposal,private,original=captured;selected=proposal['source']['capture_operation_id']
    with c.transaction() as db:
        db.execute('INSERT INTO refs SELECT ?,digest FROM operation_refs WHERE operation=?',('unrelated-source-owner',selected))
        db.execute('INSERT INTO storage_retired VALUES(?,0)',(selected,))
    output=tmp_path/'retired.tar'
    with pytest.raises(Conflict,match='no longer retained'):export.export(c.root,'investigation',output,author='Author <a@example.invalid>')
    assert not output.exists()


@pytest.mark.parametrize('mutation',['alternates','config','staged-source'])
def test_source_consumption_rejects_changed_git_policy_or_staged_bytes(captured,tmp_path,monkeypatch,mutation):
    c,proposal,private,original=captured;real=export.patch_series
    def changed(base,capture,objects,stage,bundle,author,verify,**kw):
        if mutation=='alternates':(base/'.git/objects/info/alternates').write_text('/unapproved/objects\n')
        elif mutation=='config':(base/'.git/config').write_text('[core]\nrepositoryformatversion=0\n[include]\npath=/unapproved\n')
        else:objects.path(capture['archive_sha256']).write_bytes(b'changed staged source')
        return real(base,capture,objects,stage,bundle,author,verify,**kw)
    monkeypatch.setattr(export,'patch_series',changed)
    output=tmp_path/'guarded.tar';receipt=export.export(c.root,'investigation',output,author='Author <a@example.invalid>')
    value,names=manifest(output)
    assert not receipt['source_reconstructed'] and value['source'] is None
    assert not any(n.endswith('.patch') for n in names) and value['validation_status']=='unvalidated'


def test_config_change_between_policy_parse_and_snapshot_is_rejected(captured,tmp_path,monkeypatch):
    from quirkbench import source_prepare_operation
    c,proposal,private,_=captured;real=source_prepare_operation.git_tree
    def race(path,base,verify):
        rows=real(path,base,verify)
        (path/'.git/config').write_text('[core]\nrepositoryformatversion=0\n[include]\npath=/unapproved\n')
        return rows
    monkeypatch.setattr(source_prepare_operation,'git_tree',race)
    receipt=export.export(c.root,'investigation',tmp_path/'config-race.tar',author='Author <a@example.invalid>')
    assert not receipt['source_reconstructed']


def test_export_honors_configured_reserve_before_any_staging(lab,tmp_path,monkeypatch,capsys):
    from quirkbench.store import StoragePressure
    c,_=lab;populate(c)
    monkeypatch.setattr(export,'reserve_bytes',lambda _:10**20)
    output=tmp_path/'reserve.tar'
    with pytest.raises(StoragePressure):export.export(c.root,'investigation',output)
    assert not output.exists() and not list(tmp_path.glob('.quirkbench-export-*'))
    assert cli.main(['investigation', 'results', 'export', 'investigation', '--output', str(output), '--json'], state_root=str(c.root))==4
    assert json.loads(capsys.readouterr().out)['error']['code']=='BLOCKED'


def test_export_work_budget_remains_failure_inclusive(lab,tmp_path,monkeypatch):
    c,_=lab;populate(c);monkeypatch.setattr(export,'MAX_WORK',1)
    output=tmp_path/'budget.tar'
    with pytest.raises(ContractError,match='work budget'):export.export(c.root,'investigation',output)
    assert not output.exists()


def test_immutable_capture_does_not_follow_live_cleanup_edits(captured,tmp_path):
    c,proposal,private,_=captured
    (private/'driver.c').write_text('different untested cleanup\n')
    output=tmp_path/'immutable.tar'
    receipt=export.export(c.root,'investigation',output,author='Author <a@example.invalid>')
    value,names=manifest(output)
    assert receipt['source_reconstructed'] and value['validation_status']=='unvalidated'
    with tarfile.open(output) as archive:
        patch=archive.extractfile('patches/0001-captured-changes.patch').read()
    assert b'external agent edit' in patch and b'untested cleanup' not in patch
    assert (private/'driver.c').read_text()=='different untested cleanup\n'


def test_distribution_base_keeps_recorded_patch_ancestry_unknown(prepared):
    from test_distribution_source import run
    entry,record,source_stage,stage,store=prepared
    result=run(prepared);source=stage/'output/workspace'
    capture=json.loads(store.get(result['capture_sha256']))
    (source/'driver.c').write_text('new captured distribution edit')
    fresh=source_capture.capture(source,result['base_oid'],[],stage/'fresh',store,writer_quiesced=True,verify=lambda:None)
    scratch=stage/'export';scratch.mkdir();bundle=scratch/'bundle';bundle.mkdir()
    for n in ('patches','reproduce'):(bundle/n).mkdir()
    value=export.patch_series(source,fresh,Objects(store),scratch,bundle,'Author <a@example.invalid>',lambda:None,reserve=0)
    assert value['base_oid']==result['base_oid'] and value['reconstruction_verified']
    provenance=json.loads(store.get(capture['provenance']['distribution_patches_sha256']))
    assert provenance['upstream_relationship']['upstream_base_oid'] is None
    assert 'unknown' in value['upstream_ancestry']


def test_tested_source_match_and_later_cleanup_remain_separate(published,joined,monkeypatch,tmp_path):
    from test_proposal_dispatch import capture_proposal,dispatch_and_build,bounded_attempt
    from test_attended_baseline import attended_lab
    from quirkbench import attended_baseline,source_workspace
    c,composition=published
    with c.lifecycle() as owner:
        attended_baseline.admit(c,'investigation',composition,'export-baseline',ready=lambda _:None)
        hardware,client,step,backend,boot=attended_lab(c,tmp_path)
        bounded_attempt(c,step,hardware.boot_id,'candidate-export-base','recovery-export-base')
        op,proposal,workspace=capture_proposal(c,owner,monkeypatch,'export-patch','tested export change\n')
        _,experiment=dispatch_and_build(c,owner,monkeypatch,op,joined[5],'export-dispatch')
        attempt=bounded_attempt(c,step,'recovery-export-base','candidate-export-patch','recovery-export-patch')
        # Export after the lifecycle owner has stopped; no owner/service starts.
    output=tmp_path/'tested.tar';receipt=export.export(c.root,'investigation',output,author='Author <a@example.invalid>')
    value,_=manifest(output)
    assert value['tested_source_attempts']==[attempt] and value['validation_status']=='tested-source-match'
    assert value['conclusion']=='inconclusive' and not value['native_qualification']
    # Validated same-investigation experiment ownership is a legitimate retained
    # successor; unrelated owners tested above are not.
    selected=proposal['source']['capture_operation_id']
    with c.transaction() as db:
        db.execute('DELETE FROM operation_refs WHERE operation=?',(selected,))
        db.execute('DELETE FROM refs WHERE owner=?',(selected,))
        db.execute('INSERT INTO storage_retired VALUES(?,0)',(selected,))
    successor=tmp_path/'successor.tar'
    receipt=export.export(c.root,'investigation',successor,capture_id=selected,author='Author <a@example.invalid>')
    assert receipt['source_reconstructed'] and manifest(successor)[0]['tested_source_attempts']==[attempt]
    with c.lifecycle() as owner:
        c.resume('investigation')
        capture_proposal(c,owner,monkeypatch,'export-cleanup','different final cleanup\n')
    newer=tmp_path/'cleanup.tar';export.export(c.root,'investigation',newer,author='Author <a@example.invalid>')
    value,_=manifest(newer)
    assert value['validation_status']=='unvalidated' and value['tested_source_attempts']==[]


def test_installed_cli_and_resources_export_without_checkout(lab,tmp_path):
    import subprocess
    from test_release_plan import archive
    from quirkbench.controller_install import install
    c,_=lab;populate(c)
    assets=tmp_path/'assets';assets.mkdir()
    catalog=json.loads((Path(__file__).parents[1]/'src/quirkbench/baselines/catalog.v1.json').read_bytes())
    installed=install(archive(assets,catalog),data_home=tmp_path/'data')
    runtime=Path(installed['runtime_root'])
    # Actual supported service configuration, fixture-only zero reserve. No
    # process/service starts; existing lab database is the read-only input.
    private=c.root/'private';private.mkdir(exist_ok=True)
    placeholder=private/'fixture';placeholder.write_text('injected native input');placeholder.chmod(0o600)
    from quirkbench.controller_tls import create_identity
    from tls_command_fixture import TLSCommands
    create_identity(c.root,'127.0.0.1','export-fixture',run=TLSCommands())
    config={'software':{key:installed[key] for key in ('version','archive_sha256')},
            'tls_identity':{'kind':'setup','request_id':'export-fixture'},
            'tokens_file':str(placeholder),'reserve_gib':0}
    config_path=private/'controller-service.json';config_path.write_text(json.dumps(config));config_path.chmod(0o600)
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
    help_run=subprocess.run([str(runtime / 'bin/quirkbench'), 'investigation', 'results', 'export', '--help'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert help_run.returncode==0 and '--author' in help_run.stdout
    config_home=tmp_path/'export-config';(config_home/'quirkbench').mkdir(parents=True)
    (config_home/'quirkbench/controller.json').write_text(json.dumps({'schema_version':1,'state_root':str(c.root)}))
    env['XDG_CONFIG_HOME']=str(config_home)
    output=tmp_path/'installed.tar'
    run=subprocess.run([str(runtime / 'bin/quirkbench'), 'investigation', 'results', 'export', 'investigation', '--output', str(output), '--json'],
                       cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert run.returncode==0,run.stderr+run.stdout
    assert json.loads(run.stdout)['data']['conclusion']=='inconclusive'
    assert (runtime/'lib/quirkbench/guide/designs/experiment-loop.md').is_file()
    assert (runtime/'lib/quirkbench/schemas/investigation-export.v1.schema.json').is_file()
    from jsonschema import Draft202012Validator
    schema=json.loads((runtime/'lib/quirkbench/schemas/investigation-export.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(manifest(output)[0])


def test_base_bundle_mutation_after_consumption_invalidates_reconstruction(captured,tmp_path,monkeypatch):
    c,proposal,private,_=captured;real=export._git;changed=[]
    def mutate(root,args,verify,**kw):
        answer=real(root,args,verify,**kw)
        if kw.get('local_bundle'):
            Path(args[5]).write_bytes(b'no longer the successfully consumed bundle');changed.append(True)
        return answer
    monkeypatch.setattr(export,'_git',mutate)
    receipt=export.export(c.root,'investigation',tmp_path/'after-fetch.tar',author='Author <a@example.invalid>')
    value,names=manifest(tmp_path/'after-fetch.tar')
    assert changed and not receipt['source_reconstructed'] and value['source'] is None
    assert 'reproduce/base.bundle' not in names


@pytest.mark.parametrize('artifact',['reproduce/base.bundle','patches/0001-captured-changes.patch','evidence'])
def test_final_inventory_cannot_bless_changed_semantically_pinned_public_bytes(captured,tmp_path,monkeypatch,artifact):
    c,proposal,private,_=captured;identity=populate(c);real=export.patch_series
    def mutate(*args,**kw):
        proof=real(*args,**kw);bundle=args[4]
        target=bundle/'evidence'/identity if artifact=='evidence' else bundle/artifact
        target.write_bytes(b'changed after proof or verified copy')
        return proof
    monkeypatch.setattr(export,'patch_series',mutate)
    output=tmp_path/'pre-inventory.tar'
    with pytest.raises(Conflict,match='creation/proof pin'):export.export(c.root,'investigation',output,author='Author <a@example.invalid>')
    assert not output.exists()


def test_manifest_cardinality_refuses_too_many_original_evidence_rows(lab,tmp_path):
    c,_=lab;populate(c,attempts=129)
    identities=[c.store.put(('small public evidence '+str(n)).encode()).sha256 for n in range(128)]
    with c.transaction() as db:
        attempts=list(db.execute('SELECT id,result FROM attempts'))
        db.execute('DELETE FROM evidence')
        for attempt,raw in attempts:
            result=json.loads(raw);result['evidence']=identities
            db.execute('UPDATE attempts SET result=? WHERE id=?',(json.dumps(result),attempt))
            db.executemany('INSERT OR IGNORE INTO refs VALUES(?,?)',[('attempt:'+attempt,v) for v in identities])
            db.executemany('INSERT INTO evidence VALUES(?,?,?,?,?)',[(attempt,'public',n,v,20) for n,v in enumerate(identities)])
    output=tmp_path/'too-many-evidence.tar'
    with pytest.raises(ContractError,match='cardinality'):export.export(c.root,'investigation',output)
    assert not output.exists()
