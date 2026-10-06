"""Real bounded collector, immutable RAM/export files and paired HTTPS receipts."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from quirkbench import recovery_reports as reports,recovery_report_service as service
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.recovery_report_records import report_id,validate_manifest,upload_id,MAX_PAYLOAD
from quirkbench.recovery_report_client import send
from quirkbench.store import atomic_write,StoragePressure
from quirkbench.state_reader import StateReader
from quirkbench.credential_registry import CredentialRegistry,revoke_generation
from quirkbench.transport import make_server,TransportError,HTTPSDeviceClient
from test_shutdown import received,publication,bound,args,issuer,initialized,paired,UUID


@pytest.fixture
def snapshot(tmp_path):
    root=tmp_path/'ram';source=tmp_path/'sources';source.mkdir()
    paths={'run/quirkbench-boot.json':canonical({'boot':{'quirkbench.mode':'recovery','root':'PARTUUID=public','token':'PRIVATE_CANARY'},'config':{'private_key':'PRIVATE_CANARY'}}),
        'proc/cmdline':b'root=PARTUUID=public console=tty1 password=PRIVATE_CANARY\n',
        'proc/sys/kernel/random/boot_id':b'12345678-1234-1234-1234-123456789abc\n',
        'usr/lib/quirkbench/recovery-rootfs-lock.json':canonical({'schema_version':2,'recipe_sha256':'a'*64,'token':'PRIVATE_CANARY'}),
        'run/initramfs/rdsosreport.txt':b'early failure\npsk=PRIVATE_CANARY\n'}
    for name,raw in paths.items():
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    def runner(argv,deadline,limit):
        # Real child processes; only host tool endpoints are replaced.
        return reports.command_tail([sys.executable,'-c','print("public observation\\nAuthorization: Bearer PRIVATE_CANARY\\npsk=PRIVATE_CANARY\\n\\x1b[31mconsole")'],deadline,limit)
    answer=reports.collect(root=root,ready=lambda path:True,source_root=source,runner=runner)
    manifest,files=reports.load(answer['report_id'],root=root,ready=lambda path:True)
    return root,source,answer,manifest,files


def test_real_collector_excludes_secrets_unknown_fields_and_escapes_preview(snapshot,tmp_path):
    root,source,answer,manifest,files=snapshot
    assert manifest['boot_id']=='12345678-1234-1234-1234-123456789abc'
    from jsonschema import Draft202012Validator
    Draft202012Validator(json.loads((Path(__file__).parents[1]/'schemas/recovery-debug-report.v1.schema.json').read_bytes())).validate(manifest)
    assert b'PRIVATE_CANARY' not in b''.join(files.values())+canonical(manifest)
    assert b'\\u001b' in files['kernel.txt'] and b'\x1b' not in files['kernel.txt']
    assert 'reported' in reports.preview(manifest,files).lower()
    destination=tmp_path/'export'
    exported=reports.export(manifest,files,destination)
    assert exported['report_id']==answer['report_id']
    assert json.loads((destination/'manifest.json').read_bytes())==manifest
    for name,raw in files.items():assert (destination/name).read_bytes()==raw
    with pytest.raises(FileExistsError):reports.export(manifest,files,destination)
    # UI restart uses current immutable files; changed bytes cannot be sent/exported.
    atomic_write(root/answer['report_id']/'kernel.txt',b'changed')
    with pytest.raises(Conflict):reports.load(answer['report_id'],root=root,ready=lambda path:True)
    with pytest.raises(Conflict):reports.export(manifest,files|{'kernel.txt':b'changed'},tmp_path/'bad-export')
    assert not (tmp_path/'bad-export').exists()


def test_real_tail_noisy_descendants_and_absolute_deadline():
    start=time.monotonic()
    raw,state=reports.command_tail([sys.executable,'-c','import os; os.write(1,(b"a"*100+b"\\n")*1000+b"RECENT");'],start+1,128)
    assert raw.endswith(b'RECENT\n') and len(raw)==128 and state=='truncated'
    raw,state=reports.command_tail([sys.executable,'-c','import time; print("started",flush=True); time.sleep(60)'],time.monotonic()+.1,128)
    assert state=='deadline'


def test_offline_no_boot_no_identity_missing_tools_collection_and_local_export(tmp_path):
    root=tmp_path/'ram';source=tmp_path/'empty';source.mkdir()
    def missing(*args):raise FileNotFoundError()
    answer=reports.collect(root=root,ready=lambda _:True,source_root=source,runner=missing)
    manifest,files=reports.load(answer['report_id'],root=root,ready=lambda _:True)
    assert manifest['boot_id'] is None and 'Early boot logs were not retained' in reports.preview(manifest,files)
    output=io.StringIO()
    assert reports.attended('export',root=root,ready=lambda _:True,input_stream=io.StringIO(answer['report_id']+'\n'+str(tmp_path/'offline-export')+'\n'),output_stream=output)['exported']
    assert not (source/'control').exists()
    with pytest.raises((Conflict,OSError,ValueError)):send(manifest,files,control=tmp_path/'unpaired',verify_target=lambda:True,binding_reader=lambda:UUID)
    assert (root/answer['report_id']/'manifest.json').is_file()


def test_manifest_bounds_unknown_fields_and_ram_aggregate(snapshot):
    root,_,_,manifest,files=snapshot
    for bad in (manifest|{'schema_version':True},manifest|{'private':'unknown'},manifest|{'files':{'../escape':{'sha256':'a'*64,'size':1}}},
        manifest|{'files':{'kernel.txt':{'sha256':'a'*64,'size':MAX_PAYLOAD+1}}}):
        with pytest.raises(ContractError):validate_manifest(bad)
    for i in range(3):(root/str(i)).mkdir()
    with pytest.raises(Conflict,match='Four RAM reports'):reports.collect(root=root,ready=lambda _:True)


@pytest.fixture
def tls(paired):
    c,control,result,report=paired
    config=json.loads((c.root/'private/controller-service.json').read_bytes())
    server=make_server(c,certfile=config['cert'],keyfile=config['key'],credential_registry=CredentialRegistry(c.root,clock=c.clock))
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.05),daemon=True);thread.start()
    runtime=json.loads((control/'runtime.json').read_bytes())
    runtime['controller_url']='https://127.0.0.1:'+str(server.server_address[1])
    atomic_write(control/'runtime.json',canonical(runtime))
    try:yield c,control,result,report,server
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_actual_paired_https_round_trip_replay_receipt_export_no_execution(snapshot,tls,tmp_path):
    _,_,answer,manifest,files=snapshot;c,control,result,report,_=tls
    receipt=send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    assert receipt['report_id']==answer['report_id']
    assert send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)==receipt
    from quirkbench.controller import Controller
    reopened=Controller(c.root,clock=c.clock,reserve_bytes=0)
    reader=StateReader(c.root)
    assert service.listing(reader)['reports'][0]['report_id']==receipt['report_id']
    assert service.show(reader,receipt['report_id'])['manifest']==manifest
    service.export(reader,receipt['report_id'],tmp_path/'received')
    for name,raw in files.items():assert (tmp_path/'received'/name).read_bytes()==raw
    with reopened.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
    from quirkbench.upload_retention import recover_legacy
    with reopened.transaction() as db:assert not recover_legacy(c.root,db)['unknown']


def test_receipt_loss_restart_and_changed_request_rejected(snapshot,tls,monkeypatch):
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    import socket
    server=tls[-1];real=server.RequestHandlerClass._send;lost=[]
    def lose(handler,status,data):
        value=data.get('value',{})
        if status==200 and value.get('reported_diagnostics') is True and not lost:
            lost.append(value);handler.close_connection=True
            handler.connection.shutdown(socket.SHUT_RDWR);handler.connection.close();return
        return real(handler,status,data)
    monkeypatch.setattr(server.RequestHandlerClass,'_send',lose)
    with pytest.raises(TransportError):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    monkeypatch.setattr(server.RequestHandlerClass,'_send',real)
    assert send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)==lost[0]
    altered=manifest|{'collected_at':manifest['collected_at']+1}
    with pytest.raises(TransportError):send(altered,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)


@pytest.mark.parametrize('phase',['begin','chunk','finish'])
def test_revocation_at_each_authenticated_mutation_has_no_receipt(snapshot,tls,phase,monkeypatch):
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    real=service.handle
    def revoke(*args,**kw):
        if args[4]==phase:revoke_generation(c,result['credential_generation']['generation'])
        return real(*args,**kw)
    monkeypatch.setattr(service,'handle',revoke)
    with pytest.raises(TransportError,match='HTTP 403'):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    with c.transaction() as db:
        assert db.execute('SELECT revoked FROM credential_generations WHERE generation=?',(result['credential_generation']['generation'],)).fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM diagnostic_reports WHERE received IS NOT NULL').fetchone()[0]==0


def test_controller_full_retains_target_report_and_never_receipts(snapshot,tls,monkeypatch):
    root,_,answer,manifest,files=snapshot;c,control,result,report,_=tls
    monkeypatch.setattr(c.store,'check_space',lambda n=0:(_ for _ in ()).throw(StoragePressure('full')))
    with pytest.raises(TransportError):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    assert reports.load(answer['report_id'],root=root,ready=lambda _:True)==(manifest,files)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM diagnostic_reports WHERE received IS NOT NULL').fetchone()[0]==0


def test_tombstoned_delete_retries_partial_cleanup_and_keeps_shared_cas(snapshot,tls):
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    receipt=send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    protected=next(iter(manifest['files'].values()))['sha256']
    with c.transaction() as db:db.execute('INSERT INTO refs VALUES(?,?)',('unrelated-owner',protected))
    def interrupt(stage):
        if stage=='report_tombstoned':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):service.delete(c,receipt['report_id'],fault_hook=interrupt)
    with pytest.raises(TransportError):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    assert service.delete(c,receipt['report_id'])['deleted']
    assert c.store.path(protected).is_file()
    with pytest.raises(ContractError):service.show(StateReader(c.root),receipt['report_id'])
    assert not [p for p in c.store.uploads.iterdir() if p.suffix in ('.json','.part')]


def test_wrong_controller_certificate_and_expired_normal_credentials_never_receipt(snapshot,tls,monkeypatch):
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    runtime=json.loads((control/'runtime.json').read_bytes());ca=control/runtime['ca'];original=ca.read_bytes()
    ca.write_bytes(b'not a certificate')
    with pytest.raises((TransportError,ValueError,OSError)):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    ca.write_bytes(original)
    with c.transaction() as db:db.execute('UPDATE credential_generations SET expires_at=? WHERE generation=?',(int(c.clock())-1,result['credential_generation']['generation']))
    with pytest.raises(TransportError):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    with c.transaction() as db:assert not db.execute('SELECT * FROM diagnostic_reports').fetchall()


def test_opaque_report_retention_does_not_parse_large_json_or_delete_shared_bytes(snapshot,tls):
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    from quirkbench.retention import collect
    from quirkbench.filesystem import private_lock
    raw=b'{'+b' '*9*1024**2
    artifact=c.store.put(raw)
    manifest=manifest|{'request_id':'large-opaque-report','files':{'kernel.txt':{'sha256':artifact.sha256,'size':len(raw)}}}
    with c.transaction() as db:
        db.execute('INSERT INTO diagnostic_reports(device,request,report_sha256,manifest,received) VALUES(?,?,?,?,?)',
            (report.device_id,manifest['request_id'],report_id(manifest),canonical(manifest).decode(),c.clock()))
        db.execute('INSERT INTO storage_garbage VALUES(?,?)',(artifact.sha256,0))
    with private_lock(c.root/'command.lock'),private_lock(c.root/'coordinator.lock'),private_lock(c.root/'build.lock'):
        collect(c.root)
    assert c.store.path(artifact.sha256).is_file()
    assert service.export(StateReader(c.root),report_id(manifest),c.root.parent/'opaque-export')['exported']
    service.delete(c,report_id(manifest))
    assert not c.store.path(artifact.sha256).exists()


def test_admin_diagnostics_commands_use_real_readers_and_explicit_delete(snapshot,tls,tmp_path,capsys):
    from quirkbench import cli
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    receipt=send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    base=['--state',str(c.root),'--json','admin','diagnostics']
    for argv in (['list'],['show',receipt['report_id']],['export',receipt['report_id'],'--output',str(tmp_path/'admin-export')]):
        assert cli.main(base+argv)==0
        assert json.loads(capsys.readouterr().out)['error'] is None
    assert (tmp_path/'admin-export'/'manifest.json').is_file()
    assert cli.main(base+['delete',receipt['report_id']])==0
    assert json.loads(capsys.readouterr().out)['error'] is None


def test_collection_deadline_cannot_publish_complete_manifest(tmp_path,monkeypatch):
    root=tmp_path/'ram';source=tmp_path/'source';source.mkdir();now=[0.0]
    def run(*a):return b'public\n','included'
    original=reports.atomic_write
    def write(path,raw):
        original(path,raw)
        now[0]=11
    monkeypatch.setattr(reports,'atomic_write',write)
    with pytest.raises(TimeoutError):reports.collect(root=root,ready=lambda _:True,source_root=source,runner=run,clock=lambda:now[0])
    assert list(root.rglob('*.txt')) and not list(root.rglob('manifest.json'))


def test_descendant_retaining_stdout_is_stopped_at_collection_deadline(tmp_path):
    pidfile=tmp_path/'child'
    program='import os,time; pid=os.fork();\nif pid==0:\n open('+repr(str(pidfile))+',"w").write(str(os.getpid())); time.sleep(60)\nelse:\n time.sleep(60)'
    raw,state=reports.command_tail([sys.executable,'-c',program],time.monotonic()+.2,128)
    assert state=='deadline'
    pid=int(pidfile.read_text())
    path=Path('/proc')/str(pid)/'stat'
    if path.exists():assert path.read_text().split(') ',1)[1].split()[0] in ('Z','X')


def test_private_key_redaction_precedes_tail_truncation():
    canary='A'*64
    program='print("-----BEGIN PRIVATE KEY-----"); print(('+repr(canary)+'+"\\n")*10000,end=""); print("-----END PRIVATE KEY-----"); print("public ending")'
    raw,state=reports.command_tail([sys.executable,'-c',program],time.monotonic()+2,256)
    assert state=='truncated' and b'public ending' in raw and canary.encode() not in raw
    assert canary.encode() not in reports.sanitize(canary+'\n-----END PRIVATE KEY-----\npublic ending')


@pytest.mark.parametrize('owner',['configured_builder','legacy_upload','unknown_upload'])
def test_report_deletion_respects_existing_configured_and_legacy_roots(snapshot,tls,owner):
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    receipt=send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    file=manifest['files']['kernel.txt'];value=file['sha256']
    if owner=='configured_builder':
        path=c.root/'private/controller-service.json';config=json.loads(path.read_bytes())
        config['builder_archive_sha256']=value;atomic_write(path,canonical(config))
    elif owner=='legacy_upload':
        atomic_write(c.store.uploads/('f'*64+'.json'),canonical({'sha256':value,'size':file['size']}))
    else:atomic_write(c.store.uploads/'unknown-fragment.part',b'unidentified')
    service.delete(c,receipt['report_id'])
    assert c.store.get(value)==files['kernel.txt']


def test_complete_diagnostics_survive_existing_backup_restore(snapshot,tls,tmp_path):
    from quirkbench.controller import Controller
    _,_,_,manifest,files=snapshot;c,control,result,report,_=tls
    receipt=send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    backup=tmp_path/'backup';c.backup(backup)
    restored=Controller.restore(backup,tmp_path/'restored')
    assert service.show(StateReader(restored.root),receipt['report_id'])['manifest']==manifest
    service.export(StateReader(restored.root),receipt['report_id'],tmp_path/'restored-export')
    for name,raw in files.items():assert (tmp_path/'restored-export'/name).read_bytes()==raw


def test_real_server_restart_replays_durable_receipt_without_duplicate(snapshot,tls,tmp_path):
    _,_,_,manifest,files=snapshot;c,control,_,_,old=tls
    receipt=send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    old.shutdown();old.server_close()
    from quirkbench.controller import Controller
    reopened=Controller(c.root,clock=c.clock,reserve_bytes=0)
    config=json.loads((c.root/'private/controller-service.json').read_bytes())
    server=make_server(reopened,certfile=config['cert'],keyfile=config['key'],credential_registry=CredentialRegistry(c.root,clock=c.clock))
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.05),daemon=True);thread.start()
    runtime=json.loads((control/'runtime.json').read_bytes());runtime['controller_url']='https://127.0.0.1:'+str(server.server_address[1]);atomic_write(control/'runtime.json',canonical(runtime))
    try:
        assert send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)==receipt
        with reopened.transaction() as db:assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==1
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_valid_wrong_controller_trust_and_chunk_space_failure_never_receipt(snapshot,tls,monkeypatch):
    _,_,_,manifest,files=snapshot;c,control,result,report,server=tls
    runtime=json.loads((control/'runtime.json').read_bytes());ca=control/runtime['ca'];original=ca.read_bytes()
    ca.write_text(result['repository_certificate_pem'])
    with pytest.raises(TransportError):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==0
    ca.write_bytes(original)
    from quirkbench import store
    write=store.write_all
    def full(stream,raw):
        if str(getattr(stream,'name','')).endswith('.part'):raise OSError(28,'injected ENOSPC')
        return write(stream,raw)
    monkeypatch.setattr(store,'write_all',full)
    with pytest.raises(TransportError):send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)
    with c.transaction() as db:
        rows=db.execute('SELECT received FROM diagnostic_reports').fetchall()
        assert len(rows)==1 and rows[0][0] is None
    assert reports.load(report_id(manifest),root=snapshot[0],ready=lambda _:True)[1]==files


def test_actual_https_rejects_malformed_oversized_changed_chunks_and_static_auth(snapshot,tls):
    import http.client
    from urllib.parse import urlsplit
    from quirkbench.transport import MAX_BODY,MAX_CHUNK
    _,_,_,manifest,files=snapshot;c,control,_,_,_=tls
    runtime=json.loads((control/'runtime.json').read_bytes())
    token=(control/runtime['token_file']).read_text().strip()
    client=HTTPSDeviceClient(runtime['controller_url'],runtime['device_id'],token,str(control/runtime['ca']))
    url=urlsplit(runtime['controller_url'])
    def post(action,raw,*,length=None,credential=token):
        conn=http.client.HTTPSConnection(url.hostname,url.port,context=client.context,timeout=2)
        try:
            conn.request('POST','/v1/recovery-reports/'+action,raw,{
                'Content-Type':'application/json','Content-Length':str(len(raw) if length is None else length),
                'X-Device-ID':runtime['device_id'],'Authorization':'Bearer '+credential})
            response=conn.getresponse();status=response.status;response.read();return status
        finally:conn.close()
    assert post('begin',b'{malformed')==400
    assert post('begin',canonical({'schema_version':1,'manifest':manifest|{'unknown':'PRIVATE_CANARY'}}))==400
    assert post('begin',b'',length=MAX_BODY+1)==413
    assert post('begin',canonical({'schema_version':1,'manifest':manifest}),credential='s'*64)==403
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==0
    client._request('/v1/recovery-reports/begin',{'manifest':manifest})
    name=next(name for name,raw in files.items() if raw);raw=files[name]
    payload={'request_id':manifest['request_id'],'file':name,'offset':0,'data_b64':base64.b64encode(raw).decode()}
    client._request('/v1/recovery-reports/chunk',payload)
    with pytest.raises(TransportError,match='HTTP 409'):
        client._request('/v1/recovery-reports/chunk',payload|{'data_b64':base64.b64encode(b'!'+raw[1:]).decode()})
    oversized=payload|{'data_b64':'A'*(((MAX_CHUNK+2)//3)*4+4)}
    with pytest.raises(TransportError,match='HTTP 400'):client._request('/v1/recovery-reports/chunk',oversized)
    with pytest.raises(TransportError):client._request('/v1/recovery-reports/finish',{'request_id':manifest['request_id']})
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM diagnostic_reports WHERE received IS NOT NULL').fetchone()[0]==0
    # Existing valid chunk and RAM snapshot survive rejected requests; ordinary
    # sender resumes the same identity and obtains exactly one durable receipt.
    assert send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)['report_id']==report_id(manifest)


def test_report_sender_absolute_deadline_retains_ram_and_same_request(snapshot,tls,monkeypatch):
    from types import SimpleNamespace
    from quirkbench import recovery_report_client
    root,_,answer,manifest,files=snapshot;c,control,_,_,_=tls
    clock={'elapsed':0};real=time.monotonic
    monkeypatch.setattr(recovery_report_client,'time',SimpleNamespace(monotonic=lambda:real()+clock['elapsed']))
    def progress(_):clock['elapsed']=46
    with pytest.raises((TransportError,TimeoutError)) as failure:
        send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID,progress=progress)
    assert 'recovery report upload' in str(failure.value) or 'recovery report upload' in str(failure.value.__cause__)
    assert reports.load(answer['report_id'],root=root,ready=lambda _:True)==(manifest,files)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM diagnostic_reports WHERE received IS NOT NULL').fetchone()[0]==0
    clock['elapsed']=0
    assert send(manifest,files,control=control,verify_target=lambda:True,binding_reader=lambda:UUID)['report_id']==answer['report_id']
