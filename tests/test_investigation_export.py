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
from test_investigations import start


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
    value=export.patch_series(source,capture,Objects(store),stage,bundle,'Actual Author <author@example.invalid>',lambda:None)
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
    output=tmp_path/'public.tar'
    receipt=export.export(c.root,'investigation',output)
    value,names=manifest(output)
    assert receipt['conclusion']=='inconclusive' and not receipt['source_reconstructed']
    assert value['complete'] and value['source'] is None and value['validation_status']=='unvalidated'
    assert {'README.md','report.md','manifest.json','experiments/report.json','evidence/'+identity}<=set(names)
    with tarfile.open(output) as archive:
        raw=b''.join(archive.extractfile(n).read() for n in names)
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
    output=tmp_path/'cli.tar'
    assert cli.main(['--state',str(c.root),'investigation','export','investigation','--output',str(output),'--json'])==0
    assert json.loads(capsys.readouterr().out)['data']['conclusion']=='inconclusive'
