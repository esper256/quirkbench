"""One joined prepared-USB software journey; host devices/services are disposable adapters."""
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from quirkbench.contracts import canonical,digest
from quirkbench import binding,prepared_media,prepared_enrollment,recovery_reports,provisioning
from quirkbench.preparation_payload import stage_enrollment
from quirkbench.recovery_actions import RecoveryActions
from quirkbench.recovery_status import read_status,actions
from quirkbench.recovery_dashboard import Screen
from quirkbench.recovery_report_service import export,show
from quirkbench.state_reader import StateReader
from quirkbench.transport import make_server
from quirkbench.credential_registry import CredentialRegistry
from quirkbench.enrollment import create_code
from quirkbench.enrollment_service import EnrollmentService
from test_prepared_enrollment import initialized,publication
from test_enrollment import issuer
from test_enrollment_certificate import bound
from test_enrollment_credentials import Commands
from test_preparation_plan import selected
from test_prepared_media import factory
from test_boot import CONFIG

UUID='864fad97-1557-41e0-9f9a-ed27c4f17256'


class Attended:
    def __init__(self,*answers):self.answers=iter(answers);self.text=''
    def write(self,s):self.text+=s;return len(s)
    def flush(self):pass
    def readline(self):return next(self.answers,'')+'\n'


def test_prepared_handoff_network_pair_collect_consent_https_receipt_and_export(publication,selected,tmp_path,monkeypatch):
    c,_,_,options=publication;plan,_=selected
    path=c.root/'private/controller-service.json';config=json.loads(path.read_bytes())
    from quirkbench import enrollment_console
    require_tools=enrollment_console.require_native_tools
    monkeypatch.setattr(enrollment_console,'require_native_tools',lambda:require_tools(which=lambda name:'/fixture/native/'+name))
    commands=Commands();app=EnrollmentService(c,run=commands,tls_inspector=options['tls_inspector'])
    server=make_server(c,certfile=config['cert'],keyfile=config['key'],credential_registry=CredentialRegistry(c.root),enrollment_service=app)
    config['port']=server.server_address[1];path.write_bytes(canonical(config))
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.05),daemon=True);thread.start()
    # Translate the host-facing LAN socket only; retain real TLS hostname/SAN,
    # routing, challenge signatures, authorization, controller storage and receipt.
    connect=socket.socket.connect
    def local_socket(sock,address,*a,**kw):
        if address[0]=='192.0.2.44':address=('127.0.0.1',server.server_address[1])
        return connect(sock,address,*a,**kw)
    monkeypatch.setattr(socket.socket,'connect',local_socket)
    monkeypatch.setattr(binding,'read_system_uuid',lambda:UUID)
    # Native GPG adapter validates the exact fixture public key. All validators
    # and private-generation publication remain production implementations.
    signing=provisioning.validate_signing_key
    monkeypatch.setattr(provisioning,'validate_signing_key',lambda p,**kw:signing(p,runner=commands,temporary_parent=kw.get('temporary_parent')))
    native=[];owner={"active":True,"fault":None}
    def host(argv,**kw):
        if argv[0].endswith('systemctl'):
            native.append(argv)
            if argv[1]=='show':
                active=owner['active']
                return subprocess.CompletedProcess(argv,0,stdout=f'LoadState=loaded\nActiveState={"active" if active else "inactive"}\nMainPID={"123" if active else "0"}\nControlGroup=\nKillMode=control-group\nJob=0\n')
            if argv[1]=='stop':
                owner['active']=False
                if owner['fault']=='stop':return subprocess.CompletedProcess(argv,1)
            if argv[1]=='start':owner['active']=True
            return subprocess.CompletedProcess(argv,0,b'' if not kw.get('text') else 'active',b'')
        if argv[0].endswith('nmtui'):
            native.append(argv);return subprocess.CompletedProcess(argv,0)
        return commands(argv,**kw)
    invitation=create_code(c,plan['target'],plan['preparation_id'],**options)
    plan['controller']={k:invitation['record'][k] for k in ('controller_url','certificate_sha256')}
    work=tmp_path/'controller-payload';work.mkdir()
    metadata,_,control,_=stage_enrollment(plan,work,invitation,Path(config['cert']).read_text())
    complete=prepared_media.completed(plan['prepared_media'])
    assert metadata['prepared_media_sha256']==digest(canonical(complete))
    assert metadata['invitation']['expires_at'] is None
    assert not any(p.name in ('key.pem','controller.key') for p in control.iterdir())
    from types import SimpleNamespace
    from quirkbench.boot import RecoveryConfig,prepared_capacity
    from quirkbench.prepared_factory import validate
    boot_config=RecoveryConfig(plan['factory']['disk_guid'],*plan['factory']['partition_uuids'])
    state_mount=tmp_path/'state-mount';(state_mount/'quirkbench').mkdir(parents=True)
    (state_mount/'quirkbench/prepared-media.json').write_bytes(canonical(complete))
    layout=SimpleNamespace(logical_sector_size=512,backup_needs_relocation=False,
        disk_sectors=complete['device_bytes']//512,
        partitions=[SimpleNamespace(start=a,end=b) for a,b in complete['geometry']])
    capacity=prepared_capacity(validate(plan['factory']),layout,state_mount)
    assert capacity['experiment_mib']>10000 and capacity['evidence_mib']>10000
    boot_path=tmp_path/'boot.json';boot={'quirkbench.mode':'recovery','quirkbench.capacity':capacity}
    state={'network':'disconnected'}
    def command(argv):
        if 'quirkbench-recovery.service' in argv:return 'ActiveState=active\nResult=success\n'
        if argv[1]=='is-active':return 'active'
        if 'device' in argv:return 'wifi:'+state['network']
        return 'enabled:enabled'
    def context():
        from quirkbench.boot import validate_capacity
        actual=json.loads(boot_path.read_bytes());validate_capacity(actual['boot']['quirkbench.capacity'])
        def verify():
            if not owner['active'] and owner['fault']=='binding':
                from quirkbench.contracts import Conflict
                raise Conflict('current media changed after native stop')
            return True
        return boot_config,actual['boot'],verify
    def observe():return read_status(boot_record=boot_path,control=control,experiments=tmp_path,
        command=command,verify_boot=lambda _:context(),binding_reader=lambda:UUID,profiles_ready=lambda:True)
    sources=tmp_path/'sources';sources.mkdir();(sources/'proc').mkdir()
    (sources/'proc/cmdline').write_text('console=tty1 password=PRIVATE_CANARY')
    for name in ('run/initramfs','proc/sys/kernel/random'):(sources/name).mkdir(parents=True,exist_ok=True)
    (sources/'proc/sys/kernel/random/boot_id').write_text('different-test-boot')
    (sources/'run/initramfs/rdsosreport.txt').write_text('early observation\npsk=PRIVATE_CANARY')
    monkeypatch.setattr(recovery_reports,'COMMANDS',{name:[sys.executable,'-c','print("public boot observation\\npassword=PRIVATE_CANARY")'] for name in recovery_reports.COMMANDS})
    services=RecoveryActions(control=control,context=context,run=host,profiles_ready=lambda:True,
        report_root=tmp_path/'ram-reports',report_ready=lambda _:True,report_sources=sources)
    try:
        # Missing checks do not gate temporary local networking, collection/export.
        first=observe();assert first.boot=='missing' and next(a for a in actions(first) if a.id=='network').enabled
        services.network();assert any(a[0].endswith('nmtui') for a in native)
        offline=services.dispatch('collect',Attended())
        output=tmp_path/'offline-export';services.dispatch('export',Attended(offline['report_id'],str(output)))
        assert (output/'manifest.json').is_file()
        with pytest.raises((OSError,ValueError,RuntimeError)):services.dispatch('upload',Attended(offline['report_id'],'SEND '+offline['report_id']))
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==0
        # Recovery arrival changes real observations without moving selection.
        screen=Screen(facts=first,loading=False);screen.focus['home']='diagnostics'
        boot_path.write_bytes(canonical({'config':boot_config.to_dict(),'boot':boot}))
        state['network']='connected';screen.facts=observe()
        assert screen.focus['home']=='diagnostics' and screen.facts.usb=='prepared'
        stream=Attended();paired=services.dispatch('pair',stream)
        assert paired['enrolled'] and not paired['boot_authorized']
        assert not (control/prepared_enrollment.SECRET).exists()
        current=observe();assert current.paired and current.controller=='connected' and current.binding=='current'
        # Real immutable-generation activation through the dashboard adapter,
        # with cancellation, uncertain stop and post-stop binding loss.
        import shutil
        runtime=json.loads((control/'runtime.json').read_bytes())
        generation=control/Path(runtime['ca']).parent
        shutil.copytree(generation,control/'setup');(control/'setup/generation.json').unlink()
        from quirkbench import enrollment_target
        # Nested directory and bind-file mounts are refused before any setup
        # secret read or service stop; held locks do not freeze mount state.
        real_read=provisioning._read
        def reject_setup_read(path):
            assert not Path(path).is_relative_to(control/'setup')
            return real_read(path)
        for mount in (control/'setup',control/'setup/token'):
            count=len(native)
            with monkeypatch.context() as guarded:
                guarded.setattr(enrollment_target,'nested_mounts',lambda _: [str(mount)])
                guarded.setattr(provisioning,'_read',reject_setup_read)
                with pytest.raises(ValueError,match='nested mounts'):services.dispatch('setup_file',Attended('APPLY SETUP'))
            assert len(native)==count
        before=(control/'runtime.json').read_bytes();start=len(native)
        assert services.dispatch('setup_file',Attended())['cancelled'] and len(native)==start
        from quirkbench.contracts import Conflict
        for fault in ('stop','binding'):
            owner['fault']=fault
            with pytest.raises(Conflict):services.dispatch('setup_file',Attended('APPLY SETUP'))
            assert owner['active'] and (control/'runtime.json').read_bytes()==before
        owner['fault']=None
        with monkeypatch.context() as guarded:
            guarded.setattr(enrollment_target,'nested_mounts',lambda _: [] if owner['active'] else [str(control/'setup')])
            with pytest.raises(ValueError,match='nested mounts'):services.dispatch('setup_file',Attended('APPLY SETUP'))
            assert owner['active'] and (control/'runtime.json').read_bytes()==before
        assert services.dispatch('setup_file',Attended('APPLY SETUP'))['activated']
        assert owner['active'] and (control/'runtime.json').read_bytes()==before
        collected=services.dispatch('collect',Attended())
        sha=collected['report_id'];cancel=Attended(sha,'');assert services.dispatch('upload',cancel)['cancelled']
        with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==0
        reviewed=Attended(sha,'SEND '+sha);receipt=services.dispatch('upload',reviewed)
        assert receipt['report_id']==sha and 'Received: '+sha in reviewed.text
        assert 'PRIVATE_CANARY' not in reviewed.text
        assert services.dispatch('upload',Attended(sha,'SEND '+sha))==receipt
        reader=StateReader(c.root);export(reader,sha,tmp_path/'controller-export')
        manifest,files=recovery_reports.load(sha,root=services.report_root,ready=lambda _:True)
        assert show(reader,sha)['manifest']==manifest
        for name,raw in files.items():assert (tmp_path/'controller-export'/name).read_bytes()==raw
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM diagnostic_reports').fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
