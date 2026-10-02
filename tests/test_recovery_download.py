"""Small streaming tests and synthetic factory records; no image construction."""
from email.message import Message
import io
import json
from pathlib import Path
import time

import pytest

from quirkbench import recovery_download as acquisition,controller_release,job_worker
from quirkbench.contracts import ContractError,Conflict,canonical,digest
from quirkbench.http_bounds import BoundedHTTPError
from quirkbench.controller import Controller
from quirkbench.release_install import acquire_install
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.job_operations import resume
from quirkbench.worker_claim import read_active_worker_claim
from test_release_compatibility import asset_set
from test_controller_release import inputs,fake_gpg
from test_release_install import fixture
from test_builder_setup import Workers
from test_worker_service import BOOT

URL='https://releases.example.invalid/0.1.0/factory.img'


class Response:
    def __init__(self,raw, *,url=URL,status=200,size=None):
        self.raw=raw;self.url=url;self.status=status;self.headers=Message()
        self.headers['Content-Length']=str(len(raw) if size is None else size)
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def geturl(self):return self.url
    def read1(self,amount):
        raw=self.raw[:amount];self.raw=self.raw[amount:];return raw


class Opener:
    def __init__(self,response):self.response=response
    def open(self,url,timeout):assert url==URL and timeout==15;return self.response


def stream(tmp_path,raw,**patch):
    path=tmp_path/'factory.img';response=patch.pop('response',Response(raw));progress=[]
    acquisition.download_to(URL,path,digest(raw),len(raw),verify=patch.pop('verify',lambda:None),report=lambda *a,**kw:progress.append(kw),
        reserve=0,deadline=time.time()+60,opener=Opener(response),**patch)
    return path,progress


def test_stream_has_exact_digest_size_private_atomic_publication_and_progress(tmp_path):
    raw=b'factory-byte-fixture'*100000;path,progress=stream(tmp_path,raw)
    assert path.read_bytes()==raw and path.stat().st_mode&0o777==0o600
    assert progress and all(item['total']==len(raw) for item in progress)
    assert sorted(p.name for p in tmp_path.iterdir())==['factory.img']


@pytest.mark.parametrize('change',['truncated','oversized','digest','length','redirect','status','encoding','duplicate-length','expired','claim'])
def test_failed_stream_never_publishes_image(tmp_path,change):
    expected=b'exact image fixture';response=Response(expected);options={}
    if change=='truncated':response.raw=expected[:-1]
    elif change=='oversized':response.raw+=b'extra'
    elif change=='digest':response.raw=b'X'+expected[1:]
    elif change=='length':response.headers.replace_header('Content-Length','999')
    elif change=='redirect':response.url='https://other.invalid/image'
    elif change=='status':response.status=206
    elif change=='encoding':response.headers['Content-Encoding']='gzip'
    elif change=='duplicate-length':response.headers['Content-Length']=str(len(expected))
    elif change=='expired':
        clock=[0]
        def exceeded():clock[0]+=3601;return clock[0]
        options['clock']=exceeded
    elif change=='claim':
        calls=[0]
        def lost():
            calls[0]+=1
            if calls[0]>2:raise Conflict('owner changed')
        options['verify']=lost
    with pytest.raises((Conflict,ContractError)):stream(tmp_path,expected,response=response,**options)
    assert not list(tmp_path.iterdir())


def test_existing_output_or_reserve_refusal_does_not_replace_bytes(tmp_path,monkeypatch):
    path=tmp_path/'factory.img';path.write_bytes(b'operator file')
    with pytest.raises(Conflict):stream(tmp_path,b'fixture')
    assert path.read_bytes()==b'operator file'
    path.unlink()
    from quirkbench.store import StoragePressure
    import quirkbench.builder_setup as space
    monkeypatch.setattr(space,'check_space',lambda *a:(_ for _ in ()).throw(StoragePressure('reserve')))
    with pytest.raises(StoragePressure):stream(tmp_path,b'fixture')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('wire',[b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx',
    b'HTTP/1.1 200 OK\r\nX-Drip: '+b'a'*100+b'\r\nContent-Length: 1\r\n\r\nx'])
def test_status_and_header_drips_are_bounded_on_every_raw_read(wire):
    now=[0.0];closed=[];timeouts=[]
    class Raw(io.RawIOBase):
        def readable(self):return True
        def readinto(self,buffer):
            now[0]+=.1
            if not socket.raw:return 0
            buffer[0]=socket.raw[0];socket.raw=socket.raw[1:];return 1
    class Socket:
        raw=wire
        def makefile(self,*a,**kw):assert kw['buffering']==0;return Raw()
        def settimeout(self,value):timeouts.append(value)
    socket=Socket()
    class Connection:
        def __init__(self,host,port,context,timeout):assert host=='releases.example.invalid' and context.check_hostname;assert timeout<=.5
        def connect(self):pass
        def request(self,*a,**kw):pass
        def getresponse(self):
            response=self.response_class(socket);response.begin();return response
        def close(self):closed.append(True)
    with pytest.raises(BoundedHTTPError):
        with acquisition._response(URL,.5,lambda:now[0],connection_factory=Connection):
            raise AssertionError('dripping framing must not produce a response')
    assert now[0]<=.6 and timeouts and max(timeouts)<=.5 and closed


def test_chunked_framing_is_rejected_before_body_reads(tmp_path):
    response=Response(b'payload');response.headers['Transfer-Encoding']='chunked'
    with pytest.raises(ContractError,match='unambiguous'):stream(tmp_path,b'payload',response=response)
    assert response.raw==b'payload' and not list(tmp_path.iterdir())


def test_wall_clock_rollback_cannot_extend_stream_total_deadline(tmp_path):
    now=[0];wall=[time.time()];response=Response(b'payload')
    def clock():now[0]+=1;return now[0]
    calls=[0]
    def rollback():
        calls[0]+=1
        if calls[0]>1:wall[0]-=86400
    with pytest.raises(Conflict,match='deadline'):
        acquisition.download_to(URL,tmp_path/'factory.img',digest(b'payload'),7,verify=rollback,report=lambda *a,**kw:None,
            reserve=0,deadline=wall[0]+2,clock=clock,wall_clock=lambda:wall[0],opener=Opener(response))
    assert not list(tmp_path.iterdir())


@pytest.fixture
def signed_factory(fixture,asset_set,monkeypatch,tmp_path):
    arguments,bundle,payloads,calls=fixture;statement,assets,candidate,manifest=asset_set
    # Existing compatibility fixtures represent a 4GiB factory using small bytes
    # and injected measurement. Real stream size/digest checks are separate above.
    image=assets['recovery_image'].read_bytes();image_sha=digest(image)
    candidate['image_sha256']=image_sha;manifest['image_sha256']=image_sha
    candidate['image_manifest_sha256']=digest(canonical(manifest))
    payloads['factory.img.json']=canonical(manifest);payloads['factory.img.release-candidate.json']=canonical(candidate)
    statement.update(controller_archive_sha256=digest(payloads['controller.tar.gz']),recovery_image_sha256=image_sha,
        recovery_manifest_sha256=digest(payloads['factory.img.json']),recovery_candidate_sha256=digest(payloads['factory.img.release-candidate.json']))
    payloads['release.json']=canonical(statement)+b'\n';payloads['release.sig']=digest(payloads['release.json']).encode()
    record=acquire_install('0.1.0','installed',**arguments)
    original=controller_release._asset_digest
    def measured(path,**kw):
        if Path(path).name=='factory.img':return {'path':str(Path(path)),'sha256':digest(Path(path).read_bytes()),'size_bytes':candidate['image_size_bytes']}
        return original(path,**kw)
    monkeypatch.setattr(controller_release,'_asset_digest',measured)
    native_verify=acquisition.verify_recovery_assets
    def small_receipt(value,paths):
        verified=native_verify(value,paths)
        verified['recovery_image']['size_bytes']=Path(paths['recovery_image']).stat().st_size
        return verified
    monkeypatch.setattr(acquisition,'verify_recovery_assets',small_receipt)
    def streamed(url,path,sha,size,**opts):
        assert url==bundle['release_base_url']+'0.1.0/factory.img' and sha==image_sha and size==candidate['image_size_bytes']
        opts['verify']();path.write_bytes(image);path.chmod(0o600)
    native_capture=acquisition.capture
    def captured(intent,stage,verify,report,**opts):
        return native_capture(intent,stage,verify,report,run=fake_gpg,fetch=arguments['fetch'],stream=streamed,**opts)
    return arguments,record,payloads,calls,captured


def admitted(c,signed_factory,request='download'):
    arguments,record,*_=signed_factory;trust=acquisition.load_bundle(arguments['trust_bundle'])
    args={'schema_version':1,'version':'0.1.0','controller_archive_sha256':record['archive_sha256'],'trust_bundle_sha256':trust['bundle_sha256']}
    return c.admit_operation(request,'recovery_download',args,local_paths={
        'runtime':record['runtime_root'],'trust_bundle':str(arguments['trust_bundle']),'config_home':str(arguments['config_home'])})


def execute(c,claim,captured,monkeypatch):
    def verify(*args,**kwargs):
        return read_active_worker_claim(*args,**kwargs,boot_id_reader=lambda:BOOT,
            cgroup_reader=lambda:'0::/user.slice/'+claim['worker_unit']+'/runtime\n')
    import quirkbench.builder_setup as builder
    monkeypatch.setattr(builder,'reserve_bytes',lambda root:0)
    monkeypatch.setattr(job_worker,'read_active_worker_claim',verify)
    monkeypatch.setattr(acquisition,'capture',captured)
    return job_worker.run_worker(c.root,claim['id'],claim['worker_epoch'],claim['worker_generation'],claim['stage_dir'])


def test_fixed_worker_then_whole_stop_publishes_verified_factory_without_authority(signed_factory,tmp_path,monkeypatch):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT);captured=signed_factory[-1]
    actual_consume=acquisition.consume
    monkeypatch.setattr(acquisition,'consume',lambda coordinator,claim,intent,data:actual_consume(coordinator,claim,intent,data,run=fake_gpg))
    with c.lifecycle() as owner:
        row=admitted(c,signed_factory);services=Workers();coordinator=JobCoordinator(owner,services)
        claim=coordinator.tick();assert claim['stage']=='recovery_download'
        assert 0<claim['deadline']-c.clock()<=3599
        assert execute(c,claim,captured,monkeypatch)==0
        with pytest.raises(Conflict,match='shutdown required'):coordinator.consume(claim)
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        with c.transaction() as db:
            final=db.execute('SELECT final_output_digest FROM operations WHERE id=?',(row['id'],)).fetchone()[0]
            assert db.execute('SELECT kind FROM storage_groups WHERE owner=?',(row['id'],)).fetchone()[0]=='input'
            for table in ('devices','campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
        receipt=json.loads(c.store.get(final))
        from jsonschema import Draft202012Validator
        schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/released-recovery-acquisition.v1.schema.json').read_bytes())
        Draft202012Validator(schema).validate(receipt)
        assert not any(receipt[field] for field in ('qualified','flash_authorized','builder_ready','baseline_ready'))
        assert c.store.get(receipt['assets']['recovery_image']['sha256'])==b'synthetic image placeholder'


def test_restart_requires_stop_and_explicit_resume_of_same_signed_intent(signed_factory,tmp_path):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    with c.lifecycle() as owner:
        row=admitted(c,signed_factory);claim=owner.claim(row['id'],stage='recovery_download',deadline=c.clock()+60)
    with c.lifecycle() as successor:
        with pytest.raises(Conflict):resume(successor,row['id'])
        successor.reconcile_units(Workers());resume(successor,row['id'])
        next_claim=successor.claim(row['id'],stage='recovery_download',deadline=c.clock()+60)
        assert next_claim['worker_generation']==2 and next_claim['input_digest']==claim['input_digest']


@pytest.mark.parametrize('change',['signature','manifest','candidate','different-statement'])
def test_authentication_or_metadata_failure_publishes_no_outputs(signed_factory,tmp_path,monkeypatch,change):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    arguments,record,payloads,calls,captured=signed_factory
    if change=='signature':payloads['release.sig']=b'untrusted signature'
    elif change in ('manifest','candidate'):
        name='factory.img.json' if change=='manifest' else 'factory.img.release-candidate.json';payloads[name]+=b'changed'
    else:
        statement=json.loads(payloads['release.json']);statement['recovery_image_sha256']='f'*64
        payloads['release.json']=canonical(statement)+b'\n';payloads['release.sig']=digest(payloads['release.json']).encode()
    with c.lifecycle() as owner:
        row=admitted(c,signed_factory);services=Workers();coordinator=JobCoordinator(owner,services)
        claim=coordinator.tick();assert execute(c,claim,captured,monkeypatch)==1
        services.done=True;assert coordinator.tick()['state']=='FAILED'
        assert c.operation_status(row['id'])['data']['references']['output']==[]


def test_trust_change_after_private_worker_completion_blocks_owner_publication(signed_factory,tmp_path,monkeypatch):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    arguments,record,payloads,calls,captured=signed_factory;native_consume=acquisition.consume
    monkeypatch.setattr(acquisition,'consume',lambda coordinator,claim,intent,data:native_consume(coordinator,claim,intent,data,run=fake_gpg))
    with c.lifecycle() as owner:
        row=admitted(c,signed_factory);services=Workers();coordinator=JobCoordinator(owner,services)
        claim=coordinator.tick();assert execute(c,claim,captured,monkeypatch)==0
        path=arguments['trust_bundle'];trust=json.loads(path.read_bytes());trust['expires_at']-=1;path.write_bytes(canonical(trust))
        services.done=True
        with pytest.raises(Conflict):coordinator.tick()
        assert c.operation_status(row['id'])['data']['references']['output']==[]


def test_terminal_copy_requires_current_claim_and_exact_receipt(signed_factory,tmp_path,monkeypatch):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    native_consume=acquisition.consume
    monkeypatch.setattr(acquisition,'consume',lambda coordinator,claim,intent,data:native_consume(coordinator,claim,intent,data,run=fake_gpg))
    with c.lifecycle() as owner:
        row=admitted(c,signed_factory);services=Workers();coordinator=JobCoordinator(owner,services)
        claim=coordinator.tick();assert execute(c,claim,signed_factory[-1],monkeypatch)==0
        path=Path(claim['stage_dir'])/'diagnostics/stage-result.json';value=json.loads(path.read_bytes())
        value['result']['assets']['recovery_image']['path']='../../different-image';path.write_bytes(canonical(value))
        services.done=True;assert coordinator.tick()['state']=='FAILED'
        assert c.operation_status(row['id'])['data']['references']['output']==[]


def test_cli_missing_production_trust_is_unavailable_before_any_state(tmp_path,capsys):
    from quirkbench.cli import main,parser
    assert parser().parse_args(['recovery','download','--request-id','fixture','--json']).action=='download'
    assert main(['--state',str(tmp_path/'absent'),'recovery','download','--request-id','first','--json'])==4
    assert json.loads(capsys.readouterr().out)['error']['code']=='UNAVAILABLE'
    assert not list(tmp_path.iterdir())


def test_admission_is_prompt_local_only_and_exactly_replayable(signed_factory,tmp_path,monkeypatch):
    from quirkbench.store import atomic_write
    arguments,record,*_=signed_factory
    root=tmp_path/'state';c=Controller(root,reserve_bytes=0)
    runtime=Path(record['runtime_root'])/'bin'
    cert=tmp_path/'cert';cert.write_bytes(b'fixture certificate')
    key=tmp_path/'key';key.write_bytes(b'fixture key')
    atomic_write(root/'private/controller-service.json',canonical({'runtime':str(runtime/'quirkbench'),
        'job_worker':str(runtime/'quirkbench-job-worker'),'cert':str(cert),'key':str(key),'credential_registry':True,'reserve_gib':0}))
    monkeypatch.setattr(acquisition,'_trusted',lambda *a,**kw:pytest.fail('authentication belongs in worker'))
    def submit(request=None):
        return acquisition.submit(root,request,trust_bundle=arguments['trust_bundle'],config_home=arguments['config_home'],ready=lambda path:None)
    first=submit();second=submit()
    assert first==second and first['data']['state']=='QUEUED'
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1
        for table in ('devices','campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    installation=Path(record['runtime_root'])/'installation.json'
    installation.write_bytes(b'{}')
    with pytest.raises(ContractError,match='record fields'):submit('malformed')


def test_released_recovery_schema_and_cli_frozen_fixtures():
    from jsonschema import Draft202012Validator,ValidationError
    from quirkbench.cli import parser
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/released-recovery-acquisition.v1.schema.json').read_bytes())
    value=json.loads((root/'examples/released-recovery-acquisition.json').read_bytes())
    Draft202012Validator.check_schema(schema);validator=Draft202012Validator(schema);validator.validate(value)
    for field in ('qualified','flash_authorized','builder_ready','baseline_ready'):
        with pytest.raises(ValidationError):validator.validate({**value,field:True})
    for case in json.loads((root/'examples/recovery-cli.v1.json').read_bytes())['cases']:
        actual=vars(parser().parse_args(case['argv']))
        assert {key:str(actual[key]) if isinstance(actual[key],Path) else actual[key] for key in case['expected']}==case['expected']
