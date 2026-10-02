"""Attended fingerprint approval, durable same-key retry and quiesced activation."""
from io import StringIO
import json
from pathlib import Path
import subprocess

import pytest

from quirkbench import enrollment_console as console
from quirkbench.contracts import Conflict,ContractError
from quirkbench.controller_tls import inspect_identity
from quirkbench.enrollment import create_code
from quirkbench.enrollment_service import EnrollmentService
from quirkbench.enrollment_activation import activate_enrollment
from quirkbench.provisioning import activate_bundle
from quirkbench.transport import TransportError
from test_enrollment_credentials import publication,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import request
from test_enrollment import issuer
from test_setup_service import initialized

UUID='12345678-1234-1234-1234-123456789abc'


@pytest.fixture
def pairing(publication,tmp_path):
    c,_,_,kwargs=publication;code=create_code(c,'console','console-code',**kwargs)
    config=json.loads((c.root/'private/controller-service.json').read_bytes())
    identity=inspect_identity(Path(config['cert']).parent,run=Commands())
    observation={'certificate_sha256':identity['certificate_sha256'],'certificate_pem':Path(identity['certificate']).read_text()}
    control=tmp_path/'console-control';control.mkdir(mode=0o700)
    app=EnrollmentService(c,run=Commands(),tls_inspector=kwargs['tls_inspector']);posts=[];fail=[False]
    class Client:
        def __init__(self,url,pem,pin,**kw):
            assert pin==observation['certificate_sha256'] and pem==observation['certificate_pem']
        def post(self,path,data):
            posts.append((path,data));result=app.handle(path,data,'127.0.0.1')
            if path.endswith('redeem') and fail[0]:fail[0]=False;raise TransportError('lost reply fixture')
            return result
    def activation(*a,**kw):
        def activate(bundle,control,**opts):
            return activate_bundle(bundle,control,validator=lambda path:json.loads(path.read_bytes()),**opts)
        return activate_enrollment(*a,**kw,activator=activate)
    options={'verify_target':lambda:True,'binding_reader':lambda:UUID,'run':Commands(),
        'certificate_inspector':lambda *a,**kw:observation,'client_factory':Client,
        'read_secret':lambda:code['code'],'activator':activation}
    return c,control,code,observation,options,posts,fail


def input_for(code,observed,approval=None):
    return StringIO(code['record']['controller_url']+'\n'+(approval if approval is not None else observed['certificate_sha256'])+'\n'+code['record']['code_id']+'\n')


@pytest.mark.parametrize('approval',['','yes','f'*64])
def test_no_secret_input_or_request_before_explicit_matching_fingerprint(pairing,approval):
    c,control,code,observation,options,posts,_=pairing;read=[];out=StringIO()
    options=options|{'read_secret':lambda:read.append('secret') or code['code']}
    if approval=='':
        assert console.run_initial_enrollment(control,input_stream=input_for(code,observation,approval),output_stream=out,**options) is None
    else:
        with pytest.raises((Conflict,ContractError)):
            console.run_initial_enrollment(control,input_stream=input_for(code,observation,approval),output_stream=out,**options)
    assert read==posts==[] and not (control/'enrollment').exists()
    assert code['code'] not in out.getvalue()


def test_lost_redemption_reply_resumes_same_key_request_and_activates_one_generation(pairing):
    c,control,code,observation,options,posts,fail=pairing;fail[0]=True;out=StringIO()
    with pytest.raises(TransportError):console.run_initial_enrollment(control,input_stream=input_for(code,observation),output_stream=out,**options)
    key=(control/'enrollment/pending/key.pem').read_bytes();request=(control/'enrollment/pending/request.json').read_bytes()
    assert not (control/'runtime.json').exists()
    result=console.run_initial_enrollment(control,input_stream=input_for(code,observation),output_stream=out,**options)
    assert result['enrolled'] and not result['boot_authorized']
    assert (control/'runtime.json').is_file() and (control/'enrollment/pending/key.pem').read_bytes()==key
    assert (control/'enrollment/pending/request.json').read_bytes()==request
    assert posts[0][1]['request']==posts[2][1]['request']
    assert code['code'] not in out.getvalue() and key.decode() not in out.getvalue()
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1
        for table in ('devices','campaigns','jobs','attempts'):assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    with pytest.raises(Conflict):console.run_initial_enrollment(control,input_stream=input_for(code,observation),**options)


def test_expired_unredeemed_invitation_is_explicitly_replaced_without_losing_key(pairing):
    import time
    from quirkbench.enrollment_target import prepare_request
    c,control,code,observation,options,posts,_=pairing
    original=prepare_request(control,code['record']['controller_url'],observation['certificate_sha256'],
        code['record']['code_id'],**{k:options[k] for k in ('verify_target','binding_reader','run')})
    key=(control/'enrollment/pending/key.pem').read_bytes()
    now=int(time.time())+301
    app=EnrollmentService(c,clock=lambda:now,run=Commands(),tls_inspector=lambda directory,**kw:inspect_identity(directory,run=Commands(),**kw))
    with pytest.raises(Conflict,match='expired'):app.handle('/v1/enrollment/challenge',{'schema_version':1,'request':original},'127.0.0.1')
    new=create_code(c,'renewed-console','renewed-console-code',clock=lambda:now,ready=lambda root:None,
        tls_inspector=lambda directory,**kw:inspect_identity(directory,run=Commands(),**kw))
    class Client:
        def __init__(self,*a,**kw):pass
        def post(self,path,data):return app.handle(path,data,'127.0.0.1')
    options=options|{'clock':lambda:now,'client_factory':Client,'read_secret':lambda:new['code']}
    # Selecting another invitation without confirming the retained request cancels.
    assert console.run_initial_enrollment(control,input_stream=StringIO(input_for(new,observation).getvalue()+'\n'),
        output_stream=StringIO(),**options) is None
    assert (control/'enrollment/pending/key.pem').read_bytes()==key
    out=StringIO()
    answer=console.run_initial_enrollment(control,input_stream=StringIO(input_for(new,observation).getvalue()+'replace '+original['request_id']+'\n'),
        output_stream=out,**options)
    assert answer['enrolled'] and not answer['boot_authorized']
    assert (control/'enrollment/archives'/original['request_id']/'key.pem').read_bytes()==key
    assert code['code'] not in out.getvalue() and new['code'] not in out.getvalue() and key.decode() not in out.getvalue()


def test_completed_lost_reply_blocks_new_identity_then_archived_original_can_resume(pairing):
    from quirkbench.enrollment_target import prepare_request
    c,control,code,observation,options,posts,fail=pairing;fail[0]=True
    with pytest.raises(TransportError):
        console.run_initial_enrollment(control,input_stream=input_for(code,observation),output_stream=StringIO(),**options)
    original=json.loads((control/'enrollment/pending/request.json').read_bytes())
    key=(control/'enrollment/pending/key.pem').read_bytes()
    new=create_code(c,'new-console','new-console-code',ready=lambda root:None,
        tls_inspector=lambda directory,**kw:inspect_identity(directory,run=Commands(),**kw))
    replacement=options|{'read_secret':lambda:new['code']}
    with pytest.raises(Conflict,match='identity already bound'):
        console.run_initial_enrollment(control,input_stream=StringIO(input_for(new,observation).getvalue()+'replace '+original['request_id']+'\n'),
            output_stream=StringIO(),**replacement)
    assert not (control/'runtime.json').exists()
    current=json.loads((control/'enrollment/pending/request.json').read_bytes())
    result=console.run_initial_enrollment(control,input_stream=StringIO(input_for(code,observation).getvalue()+'resume '+current['request_id']+'\n'),
        output_stream=StringIO(),**options)
    assert result['enrolled'] and not result['boot_authorized']
    assert (control/'enrollment/pending/key.pem').read_bytes()==key
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1


def test_native_secret_entry_cannot_fall_back_to_echoed_input(pairing,monkeypatch):
    import warnings
    c,control,code,observation,options,posts,_=pairing
    class TTY(StringIO):
        def isatty(self):return True
    source=TTY(input_for(code,observation).getvalue());monkeypatch.setattr(console.sys,'stdin',source)
    def broken_terminal(*a,**kw):
        warnings.warn('cannot control echo',console.getpass.GetPassWarning)
        raise AssertionError('echoed fallback must never run')
    monkeypatch.setattr(console.getpass,'getpass',broken_terminal)
    options=options.copy();options.pop('read_secret')
    with pytest.raises(ContractError,match='cannot disable secret echo'):
        console.run_initial_enrollment(control,output_stream=StringIO(),**options)
    assert posts==[] and (control/'enrollment/pending/key.pem').is_file()
    assert not (control/'runtime.json').exists()


def test_supervisor_stop_failure_prevents_exchange_and_uncertain_stop_restarts(tmp_path):
    control=tmp_path/'control';control.mkdir(mode=0o700)
    calls=[];exchange=[]
    def blocked(argv,**kw):calls.append(argv);return subprocess.CompletedProcess(argv,1)
    with pytest.raises(Conflict):console.connect_initial_controller(control=control,verify_target=lambda:True,
        run=blocked,enroll=lambda *a,**kw:exchange.append('exchange'),prerequisites=lambda:None)
    assert len(calls)==1 and exchange==[]
    calls.clear()
    def uncertain(argv,**kw):
        calls.append(argv)
        if 'stop' in argv:raise subprocess.TimeoutExpired(argv,45)
        return subprocess.CompletedProcess(argv,0)
    with pytest.raises(subprocess.TimeoutExpired):console.connect_initial_controller(control=control,verify_target=lambda:True,
        run=uncertain,enroll=lambda *a,**kw:exchange.append('exchange'),prerequisites=lambda:None)
    assert ['stop','start']==[argv[1] for argv in calls] and exchange==[]


def test_cancelled_exchange_still_restarts_existing_supervisor(tmp_path):
    control=tmp_path/'control';control.mkdir(mode=0o700);calls=[]
    def run(argv,**kw):calls.append(argv);return subprocess.CompletedProcess(argv,0)
    assert console.connect_initial_controller(control=control,verify_target=lambda:True,run=run,enroll=lambda *a,**kw:None,prerequisites=lambda:None) is None
    assert [argv[1] for argv in calls]==['stop','start']


def test_missing_native_crypto_is_actionable_before_secret_or_supervisor_work(tmp_path):
    from quirkbench.setup_contracts import SetupUnavailable
    control=tmp_path/'control';control.mkdir(mode=0o700);calls=[]
    def unavailable():console.require_native_tools(which=lambda name:None if name=='openssl' else '/fixture/gpg')
    with pytest.raises(SetupUnavailable,match='stock recovery needs native openssl'):
        console.connect_initial_controller(control=control,verify_target=lambda:True,
            prerequisites=unavailable,run=lambda *a,**kw:calls.append(a),enroll=lambda *a,**kw:calls.append(a))
    assert calls==[] and not (control/'enrollment').exists()


def test_console_selection_requires_verified_recovery(tmp_path):
    from quirkbench.console import run_console
    from test_console import boot_record
    record=tmp_path/'boot.json';calls=[];out=StringIO()
    run_console(boot_record=record,input_stream=StringIO('5\n'),output_stream=out,profiles_ready=lambda:True,
                run_enrollment_setup=lambda **kw:calls.append(kw))
    assert calls==[] and 'Pairing is blocked' in out.getvalue()
    boot_record(record)
    run_console(boot_record=record,input_stream=StringIO('5\n'),output_stream=out,profiles_ready=lambda:True,
                run_enrollment_setup=lambda **kw:calls.append(kw))
    assert len(calls)==1 and calls[0]['output_stream'] is out
