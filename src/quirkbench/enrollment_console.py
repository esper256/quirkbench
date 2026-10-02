"""Attended initial pairing; explicit fingerprint approval precedes secret input."""
import getpass
import subprocess
import sys
import time
import shutil
import warnings

from .binding import read_system_uuid
from .contracts import Conflict,ContractError,identifier,sha256
from .enrollment_client import inspect_certificate,PinnedEnrollmentClient,endpoint
from .enrollment_target import prepare_request,sign_challenge,_storage
from .enrollment_activation import activate_enrollment


def require_native_tools(*,which=shutil.which):
    from .setup_contracts import SetupUnavailable
    missing=[name for name in ('openssl','gpg') if which(name) is None]
    if missing:
        raise SetupUnavailable('recovery enrollment unavailable: stock recovery needs native '+', '.join(missing)+'; provision a compatible recovery release, then retry')


def _answer(source,output,prompt,limit):
    print(prompt,end='',file=output,flush=True)
    raw=source.readline(limit+2)
    if raw=='':return None
    if len(raw)>limit+1 or not raw.endswith('\n'):
        raise ContractError('setup input is incomplete or exceeds its limit')
    return raw[:-1].strip()


def _secret(source,output,read_secret):
    if read_secret is None:
        if source is not sys.stdin or not source.isatty():
            raise ContractError('secret entry requires the attended recovery terminal')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                code=getpass.getpass('One-use code: ',stream=output)
        except getpass.GetPassWarning as exc:
            raise ContractError('terminal cannot disable secret echo; pairing remains blocked') from exc
    else:code=read_secret()
    if code is None or code=='':return None
    return code


def run_initial_enrollment(control, *, verify_target,input_stream=None,output_stream=None,
        read_secret=None,certificate_inspector=inspect_certificate,client_factory=PinnedEnrollmentClient,
        binding_reader=read_system_uuid,run=subprocess.run,clock=time.time,activator=activate_enrollment):
    source=input_stream or sys.stdin;output=output_stream or sys.stdout
    control,verify=_storage(control,verify_target)
    if (control/'runtime.json').exists() or (control/'runtime.json').is_symlink():
        raise Conflict('initial controller configuration already exists; explicit paused maintenance required')
    url=_answer(source,output,'Controller HTTPS endpoint from target add: ',4096)
    if url is None:return None
    endpoint(url);verify()
    observation=certificate_inspector(url,clock=clock)
    print('Controller: '+url,file=output)
    print('Observed certificate SHA-256: '+observation['certificate_sha256'],file=output)
    print('Compare the full fingerprint with the controller target add output.',file=output)
    approval=_answer(source,output,'Type that full controller fingerprint to approve (empty cancels): ',64)
    if approval is None or approval=='':return None
    sha256(approval)
    if approval!=observation['certificate_sha256']:
        raise Conflict('controller certificate fingerprint does not match; no code transmitted')
    client=client_factory(url,observation['certificate_pem'],approval,clock=clock)
    code_id=_answer(source,output,'Code ID from target add: ',128)
    if code_id is None or code_id=='':return None
    identifier(code_id)
    from .enrollment_maintenance import pending_choice,select_invitation
    choice=pending_choice(control,url,approval,code_id,verify_target=verify,binding_reader=binding_reader,run=run)
    if choice is not None:
        if choice['action']=='resume':
            print('This invitation has a retained original request/key: '+choice['selected_request_id'],file=output)
            wording='resume'
        else:
            print('A different invitation is pending. Its request/key will remain private and recoverable.',file=output)
            wording='replace'
        print('This cannot cancel controller credentials. A completed original redemption must be recovered or explicitly revoked through lifecycle maintenance.',file=output)
        expected=wording+' '+choice['source_request_id']
        confirmation=_answer(source,output,'Type "'+expected+'" to select this invitation (empty cancels): ',160)
        if confirmation is None or confirmation=='':return None
        if confirmation!=expected:raise Conflict('explicit pending invitation confirmation required')
        import secrets
        select_invitation(control,url,approval,code_id,'selection-'+secrets.token_hex(16),
            action=choice['action'],confirmed_request_id=choice['source_request_id'],
            verify_target=verify,binding_reader=binding_reader,run=run)
    # Persist the same key and request even if secret entry, transport or ACK fails.
    request=prepare_request(control,url,approval,code_id,verify_target=verify,
                            binding_reader=binding_reader,run=run)
    code=_secret(source,output,read_secret)
    if code is None or code=='':return None
    import re
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',code):
        raise ContractError('invalid one-use code format')
    verify()
    nonce=client.post('/v1/enrollment/challenge',{'schema_version':1,'request':request})
    signature=sign_challenge(control,nonce,verify_target=verify,binding_reader=binding_reader,run=run,clock=clock)
    verify()
    result=client.post('/v1/enrollment/redeem',{'schema_version':1,'request':request,
        'challenge_id':nonce['challenge_id'],'code':code,'signature':signature})
    result=activator(control,result,observation['certificate_pem'],verify_target=verify,
                     binding_reader=binding_reader,run=run,clock=clock)
    print('Initial controller configuration activated. Exact candidate and attempt approval is still required.',file=output,flush=True)
    return result


def connect_initial_controller(*,input_stream=None,output_stream=None,run=subprocess.run,
                               control=None,verify_target=None,enroll=None,prerequisites=None):
    """Quiesce the existing supervisor before private initial activation, then restart."""
    from .runtime import CONTROL,boot_context
    control=control or CONTROL
    if verify_target is None:
        _,boot,verify_target=boot_context()
        if boot['quirkbench.mode']!='recovery':raise ContractError('pairing requires verified recovery')
    control,verify_target=_storage(control,verify_target)
    if (control/'runtime.json').exists() or (control/'runtime.json').is_symlink():
        raise Conflict('initial controller configuration already exists; explicit paused maintenance required')
    (prerequisites or require_native_tools)()
    stopped=None
    try:
        stopped=run(['systemctl','stop','quirkbench-supervisor.service'],check=False,capture_output=True,timeout=45)
        if stopped.returncode!=0:raise Conflict('target supervisor could not stop; pairing remains blocked')
        verify_target()
        return (enroll or run_initial_enrollment)(control,verify_target=verify_target,
                                                input_stream=input_stream,output_stream=output_stream)
    finally:
        if stopped is None or stopped.returncode==0:
            restarted=run(['systemctl','start','quirkbench-supervisor.service'],check=False,capture_output=True,timeout=120)
            if restarted.returncode!=0:raise Conflict('target supervisor restart failed; private configuration retained')
