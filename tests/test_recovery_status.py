"""Joined readonly observations plus the complete attended eligibility table."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
from test_shutdown import received,publication,bound,args,issuer,initialized,paired
from quirkbench.recovery_status import Facts, actions, recommendation, read_status, space

BASE = Facts(boot='verified', usb='prepared', evidence='ready', experiments='ready',
             binding='current', network='connected', ram_network=True,network_service=True,
             paired=True,prepared_trust=True,controller='connected',target='different-computer')
ALWAYS={'controller','diagnostics','terminal','power','retry_checks','retry_network','reset_network','collect','export','local_power'}
CURRENT={'network','save_network','replay_network','retarget','endpoint','drain','upload','shutdown','restart'}


@pytest.mark.parametrize('changes, recommended, extra', [
    ({}, 'controller', CURRENT),
    ({'boot':'missing','evidence':'unavailable','experiments':'unavailable','paired':False,'binding':'unavailable'},'diagnostics',{'network'}),
    ({'boot':'running','evidence':'unavailable','paired':False,'binding':'unavailable'},'diagnostics',{'network'}),
    ({'boot':'failed','usb':'invalid','evidence':'unavailable'},'diagnostics',{'network'}),
    ({'usb':'incomplete','boot':'failed','evidence':'unavailable'},'diagnostics',{'network'}),
    ({'evidence':'full'},'diagnostics',{'network','replay_network','drain','upload','shutdown','restart'}),
    ({'experiments':'full'},'diagnostics',CURRENT),
    ({'evidence':'unavailable'},'diagnostics',{'network'}),
    ({'binding':'unavailable'},'diagnostics',{'network'}),
    ({'binding':'moved'},'diagnostics',{'network','retarget'}),
    ({'binding':'maintenance'},'diagnostics',{'network','retarget'}),
    ({'network':'disconnected','controller':'disconnected'},'network',CURRENT-{'upload'}),
    ({'network':'no-wifi','controller':'disconnected'},'network',CURRENT-{'upload'}),
    ({'network':'radio-blocked','controller':'disconnected'},'network',CURRENT-{'upload'}),
    ({'network':'failed','network_service':False,'controller':'disconnected'},'network',CURRENT-{'network','save_network','replay_network','upload'}),
    ({'network':'blocked','ram_network':False,'controller':'disconnected'},'network',CURRENT-{'network','save_network','replay_network','upload'}),
    ({'paired':False,'binding':'unpaired','controller':'configured'},'controller',{'network','pair','shutdown','restart'}),
    ({'controller':'disconnected'},'controller',CURRENT),
])
def test_complete_state_and_action_table(changes,recommended,extra):
    f=replace(BASE,**changes)
    offered=actions(f)
    assert len({a.id for a in offered})==len(offered)
    assert {a.id for a in offered if a.enabled} == ALWAYS|extra
    assert recommendation(f)[2] == recommended
    assert all(a.reason and a.remedy for a in offered if not a.enabled)
    assert not any('experiment' in a.id or 'approve' in a.id for a in offered)


def test_real_reader_without_boot_never_reads_persistent_private_state(tmp_path):
    control=tmp_path/'evidence/control';control.mkdir(parents=True)
    (control/'runtime.json').write_text('private malformed data must not be opened')
    seen=[]
    def command(argv):
        seen.append(argv)
        if 'quirkbench-recovery.service' in argv:return 'ActiveState=activating\n'
        if argv[1]=='is-active':return 'active\n'
        if 'device' in argv:return 'ethernet:disconnected\n'
        return 'enabled:enabled\n'
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    status=read_status(boot_record=tmp_path/'missing',control=control,command=command,
                       binding_reader=lambda:None,profiles_ready=lambda:True)
    assert status.boot=='running' and not status.paired
    assert status.network=='no-wifi' and next(a for a in actions(status) if a.id=='network').enabled
    assert {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}==before


@pytest.mark.parametrize('network', ['connected','disconnected','no-wifi','radio-blocked','failed'])
def test_actual_files_and_native_network_adapter_keep_contact_distinct(tmp_path,network):
    from quirkbench.binding import verify_binding
    from test_boot import CONFIG
    from quirkbench.runtime import CONTROL
    control=tmp_path/'evidence/control';control.mkdir(parents=True)
    record=tmp_path/'boot.json';record.write_text('{}')
    target_uuid='f7a93e8e-125c-4931-83ca-312173af46d9'
    (control/'runtime.json').write_text(json.dumps({'schema_version':1,'device_id':'workshop-other',
        'controller_url':'https://192.0.2.18:9443','target_binding':{'schema_version':1,'system_uuid':target_uuid}}))
    experiments=tmp_path/'experiments';experiments.mkdir()
    def command(argv):
        if 'quirkbench-recovery.service' in argv:return 'ActiveState=active\nSubState=exited\nResult=success\n'
        if argv[1]=='is-active':return 'failed' if network=='failed' else 'active'
        if 'device' in argv:
            return {'connected':'wifi:connected','disconnected':'wifi:disconnected',
                    'radio-blocked':'wifi:disconnected','no-wifi':'ethernet:disconnected'}[network]
        return 'disabled:disabled' if network=='radio-blocked' else 'enabled:enabled'
    contacts=[]
    status=read_status(boot_record=record,control=control,experiments=experiments,command=command,
        verify_boot=lambda path:(CONFIG,{'quirkbench.mode':'recovery','quirkbench.capacity':{'record_type':'prepared-capacity','prepared_media_sha256':'a'*64}},lambda:True),
        binding_reader=lambda:target_uuid,profiles_ready=lambda:True,contact=lambda:contacts.append('authenticated') or True)
    assert status.paired and status.binding=='current'
    assert status.network==network
    assert status.controller==('connected' if network=='connected' else 'disconnected')
    assert contacts==(['authenticated'] if network=='connected' else [])
    assert not (control/'runtime-config.lock').exists()


def test_storage_byte_and_inode_exhaustion_are_operation_specific(tmp_path,monkeypatch):
    from types import SimpleNamespace
    for bytes_free,inodes_free in ((0,1),(1,0)):
        monkeypatch.setattr('os.statvfs',lambda p:SimpleNamespace(f_bavail=bytes_free,f_favail=inodes_free))
        assert space(tmp_path)=='full'
        f=replace(BASE,evidence='full')
        assert next(a for a in actions(f) if a.id=='upload').enabled
        assert not next(a for a in actions(f) if a.id=='save_network').enabled


def test_actual_moved_binding_offers_retarget_without_reading_contact_credentials(tmp_path):
    from test_boot import CONFIG
    from quirkbench.contracts import canonical
    control=tmp_path/'evidence/control';control.mkdir(parents=True)
    (control/'runtime.json').write_bytes(canonical({'schema_version':1,'device_id':'other-computer',
        'controller_url':'https://192.0.2.15:9443','target_binding':{'schema_version':1,'system_uuid':'12345678-1234-1234-1234-123456789abc'}}))
    record=tmp_path/'boot';record.write_text('{}')
    status=read_status(boot_record=record,control=control,experiments=tmp_path,
        verify_boot=lambda p:(CONFIG,{'quirkbench.mode':'recovery'},lambda:True),
        command=lambda argv:'active' if 'is-active' in argv else '',profiles_ready=lambda:True,
        binding_reader=lambda:'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',contact=lambda:pytest.fail('moved media contact'))
    assert status.binding=='moved'
    assert next(a for a in actions(status) if a.id=='retarget').enabled
    assert not next(a for a in actions(status) if a.id=='upload').enabled


def test_real_tls_connection_checks_binding_after_handshake_before_auth(paired,monkeypatch):
    import threading,time,http.client
    from quirkbench.transport import make_server
    from quirkbench.credential_registry import CredentialRegistry
    from quirkbench.store import atomic_write
    from quirkbench.contracts import canonical,Conflict
    from quirkbench.recovery_status import paired_contact
    from test_shutdown import UUID
    c,control,result,report=paired
    config=json.loads((c.root/'private/controller-service.json').read_bytes())
    server=make_server(c,certfile=config['cert'],keyfile=config['key'],credential_registry=CredentialRegistry(c.root,clock=c.clock))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    runtime=json.loads((control/'runtime.json').read_bytes());runtime['controller_url']='https://127.0.0.1:'+str(server.server_address[1])
    atomic_write(control/'runtime.json',canonical(runtime))
    try:
        assert paired_contact(control,lambda:True,lambda:UUID,deadline=time.monotonic()+5)
        original=http.client.HTTPSConnection.connect
        current=[UUID]
        def connect(connection):
            original(connection);current[0]='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        monkeypatch.setattr(http.client.HTTPSConnection,'connect',connect)
        with pytest.raises((ValueError,Conflict)):
            paired_contact(control,lambda:True,lambda:current[0],deadline=time.monotonic()+5)
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
