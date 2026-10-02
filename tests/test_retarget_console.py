"""Explicit attended retarget confirmation, pinned pairing and native owner reuse."""
from io import StringIO
import json
from pathlib import Path
import subprocess
import pytest
from quirkbench import retarget_console as console,retarget_activation,retarget_local
from quirkbench.console import run_console
from quirkbench.contracts import Conflict,ContractError
from quirkbench.enrollment_service import EnrollmentService
from quirkbench.transport import TransportError
from test_retarget_enrollment import paused,key_path
from test_retarget_local import NEW,CONFIG
from test_evidence_drain_target import spool,received,publication,bound,args,issuer,initialized,UUID
from test_enrollment_credentials import Commands
from test_evidence_drain_target import local as evidence


def setup(paused,*,failure=None,read_secret=None):
    spool,code,server=paused;c,control,old,*_=spool
    conf=json.loads((c.root/'private/controller-service.json').read_bytes());pem=Path(conf['cert']).read_text()
    app=EnrollmentService(c,run=Commands(),tls_inspector=server['tls_inspector']);posts=[];fail=[failure]
    class Client:
        def __init__(self,url,certificate,pin,**kwargs):
            assert certificate==pem and pin==code['record']['certificate_sha256']
        def post(self,path,document):
            posts.append((path,document));answer=app.handle(path,document,'127.0.0.1')
            if path.endswith('redeem') and fail[0]:fail[0]=None;raise TransportError('lost reply')
            return answer
    def activation(*a,**kw):
        return retarget_activation.activate(*a,**kw,validator=lambda path:json.loads(path.read_bytes()))
    kwargs={'verify_target':lambda:True,'binding_reader':lambda:NEW,'run':Commands(),'clearer':lambda _:None,
        'recovery_verifier':lambda _:True,'certificate_inspector':lambda *a,**kw:{'certificate_sha256':code['record']['certificate_sha256'],'certificate_pem':pem},
        'read_secret':read_secret or (lambda:code['code']),'client_factory':Client,'activator':activation}
    return kwargs,posts


def entered(paused,*,confirmation=None,pin=None):
    old=paused[0][2];code=paused[1]['record']
    return StringIO('retarget-1\n'+(confirmation if confirmation is not None else 'retarget '+old['device_id']+' '+NEW)+'\n'+
        code['controller_url']+'\n'+(pin if pin is not None else code['certificate_sha256'])+'\n'+code['code_id']+'\n')


@pytest.mark.parametrize('failure',['cancel-confirmation','wrong-confirmation','cancel-pin','wrong-pin'])
def test_no_code_or_new_key_before_exact_local_and_fingerprint_confirmation(paused,failure):
    reads=[];kw,posts=setup(paused,read_secret=lambda:reads.append('secret') or paused[1]['code']);control=paused[0][1]
    stream=entered(paused,confirmation='' if failure=='cancel-confirmation' else 'yes' if failure=='wrong-confirmation' else None,
                   pin='' if failure=='cancel-pin' else 'f'*64 if failure=='wrong-pin' else None)
    if failure.startswith('cancel'):
        assert console.run_retarget(control,CONFIG,input_stream=stream,output_stream=StringIO(),**kw) is None
    else:
        with pytest.raises((Conflict,ContractError)):console.run_retarget(control,CONFIG,input_stream=stream,output_stream=StringIO(),**kw)
    assert not reads and not posts and not key_path(paused).exists()


def test_attended_lost_reply_resumes_original_new_key_then_completed_ack_sends_no_code(paused):
    control=paused[0][1];kw,posts=setup(paused,failure='lost');out=StringIO()
    with pytest.raises(TransportError):console.run_retarget(control,CONFIG,input_stream=entered(paused),output_stream=out,**kw)
    assert retarget_local.pending_intent(control) is not None
    key=key_path(paused).read_bytes();request=key_path(paused).with_name('request.json').read_bytes()
    result=console.run_retarget(control,CONFIG,input_stream=entered(paused),output_stream=out,**kw)
    assert result['activated'] and key_path(paused).read_bytes()==key and key_path(paused).with_name('request.json').read_bytes()==request
    calls=len(posts);reads=[]
    assert console.run_retarget(control,CONFIG,input_stream=entered(paused),output_stream=out,
        **(kw|{'read_secret':lambda:reads.append('must not read'),'clearer':lambda _:(_ for _ in ()).throw(AssertionError('completed ACK must not clear'))}))==result
    assert len(posts)==calls and not reads
    assert paused[1]['code'] not in out.getvalue() and key.decode() not in out.getvalue()


def test_initial_unpaused_media_runs_native_preparation_after_confirmation(paused):
    import shutil
    control=paused[0][1];shutil.rmtree(control/'retarget');kw,posts=setup(paused);clears=[]
    result=console.run_retarget(control,CONFIG,input_stream=entered(paused),output_stream=StringIO(),
        **(kw|{'clearer':lambda _:clears.append(True)}))
    assert result['activated'] and clears and posts


def test_supervisor_stop_failure_uncertainty_cancel_and_prerequisites(paused):
    control=paused[0][1];calls=[];actions=[]
    def run(argv,**kw):calls.append(argv[1]);return subprocess.CompletedProcess(argv,0)
    common={'control':control,'config':CONFIG,'verify_target':lambda:True,'run':run,'prerequisites':lambda:None,
            'retarget':lambda *a,**kw:actions.append(True)}
    console.connect_retarget(**common);assert calls==['stop','start'] and actions==[True]
    calls.clear();actions.clear()
    def blocked(argv,**kw):calls.append(argv[1]);return subprocess.CompletedProcess(argv,1)
    with pytest.raises(Conflict):console.connect_retarget(**(common|{'run':blocked}))
    assert calls==['stop'] and not actions
    calls.clear()
    def uncertain(argv,**kw):
        calls.append(argv[1])
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,45)
        return subprocess.CompletedProcess(argv,0)
    with pytest.raises(subprocess.TimeoutExpired):console.connect_retarget(**(common|{'run':uncertain}))
    assert calls==['stop','start'] and not actions
    calls.clear()
    with pytest.raises(RuntimeError):console.connect_retarget(**(common|{'prerequisites':lambda:(_ for _ in ()).throw(RuntimeError('native unavailable'))}))
    assert not calls


def test_console_archived_actions_are_explicit_and_use_same_owner(paused):
    calls=[];actions=[];control=paused[0][1]
    def run(argv,**kw):calls.append(argv[1]);return subprocess.CompletedProcess(argv,0)
    def exported(*a,**kw):actions.append(a);return {'plan_file':'/private/plan.json'}
    result=evidence.attended_drain(control=control,config=CONFIG,verify_target=lambda:True,run=run,
        input_stream=StringIO('archived-plan retarget-1 plan-1\n'),output_stream=StringIO(),archived_exporter=exported)
    assert result['plan_file']=='/private/plan.json' and calls==['stop','start']
    assert actions==[(control,CONFIG,'retarget-1','plan-1')]


def test_recovery_menu_gates_retarget_and_returns_after_interruption(tmp_path):
    from test_console import boot_record
    record=tmp_path/'boot.json';actions=[];output=StringIO()
    def action(**kwargs):actions.append(kwargs);raise KeyboardInterrupt()
    run_console(boot_record=record,input_stream=StringIO('8\n'),output_stream=output,
                run_retarget_setup=action,system_uuid_reader=lambda:NEW)
    assert not actions and 'Retarget requires verified recovery' in output.getvalue()
    boot_record(record)
    run_console(boot_record=record,input_stream=StringIO('8\n'),output_stream=output,
                run_retarget_setup=action,system_uuid_reader=lambda:NEW)
    assert len(actions)==1 and 'Partial maintenance stays paused' in output.getvalue()


def test_terminal_echo_failure_keeps_new_request_key_and_sends_no_code(paused,monkeypatch):
    import warnings
    from quirkbench import enrollment_console as entry
    kw,posts=setup(paused);kw.pop('read_secret')
    class TTY(StringIO):
        def isatty(self):return True
    source=TTY(entered(paused).getvalue());monkeypatch.setattr(entry.sys,'stdin',source)
    def noecho(*a,**kw):warnings.warn('no echo control',entry.getpass.GetPassWarning)
    monkeypatch.setattr(entry.getpass,'getpass',noecho)
    with pytest.raises(ContractError,match='cannot disable secret echo'):
        console.run_retarget(paused[0][1],CONFIG,input_stream=source,output_stream=StringIO(),**kw)
    assert key_path(paused).exists() and not posts and retarget_local.pending_intent(paused[0][1]) is not None


@pytest.mark.parametrize('confirmation',['','yes','replace'])
def test_pending_retarget_invitation_requires_exact_replace_confirmation(paused,confirmation):
    from quirkbench import retarget_invitation
    from test_retarget_enrollment import request
    original=request(paused);oldkey=key_path(paused).read_bytes();c=paused[0][0]
    replacement=retarget_invitation.create_invitation(c,paused[0][2]['device_id'],paused[0][2]['credential_generation']['generation'],
        'console-replacement',NEW,'console-replacement-invitation',ready=lambda _:True,tls_inspector=paused[2]['tls_inspector'])
    updated=(paused[0],replacement,paused[2]);reads=[];kw,posts=setup(updated,read_secret=lambda:reads.append(True) or replacement['code'])
    stream=entered(updated);stream=StringIO(stream.getvalue()+('replace '+original['request_id'] if confirmation=='replace' else confirmation)+'\n')
    if confirmation=='yes':
        with pytest.raises(Conflict,match='confirmation'):console.run_retarget(paused[0][1],CONFIG,input_stream=stream,output_stream=StringIO(),**kw)
    else:
        result=console.run_retarget(paused[0][1],CONFIG,input_stream=stream,output_stream=StringIO(),**kw)
        if confirmation=='':assert result is None
        else:assert result['activated'] and reads and posts
    if confirmation!='replace':assert not reads and not posts and key_path(paused).read_bytes()==oldkey
