"""Attended recovery retarget: exact local confirmation and fingerprint approval."""
from pathlib import Path
import subprocess
import os
import sys
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,identifier,sha256
from .filesystem import _read
from .enrollment_records import _document
from .enrollment_client import endpoint,inspect_certificate,PinnedEnrollmentClient
from .enrollment_console import _answer,_secret,require_native_tools
from .enrollment_target import _storage
from .retarget_local import pending_intent,_location,prepare_retarget,validate_intent
from .retarget_enrollment import prepare_request
from .retarget_activation import activate,completed
from .filesystem import private_lock


def run_retarget(control,config, *,verify_target,input_stream=None,output_stream=None,read_secret=None,
        binding_reader=read_system_uuid,certificate_inspector=inspect_certificate,client_factory=PinnedEnrollmentClient,
        run=subprocess.run,clock=time.time,clearer=None,recovery_verifier=None,activator=activate):
    source=input_stream or sys.stdin;output=output_stream or sys.stdout
    control,verify=_storage(control,verify_target)
    actual=binding_reader();verify_binding({'schema_version':1,'system_uuid':actual},reader=binding_reader)
    request_id=_answer(source,output,'Local retarget request ID (reuse it after interruption; empty cancels): ',128)
    if request_id is None or not request_id:return None
    identifier(request_id);verify()
    directory=_location(control,request_id)
    if (directory/'intent.json').exists() or (directory/'intent.json').is_symlink():
        intent=validate_intent(_document(_read(directory,'intent.json')))
        if intent['request_id']!=request_id or intent['new_target_binding']['system_uuid']!=actual:
            raise Conflict('retarget request belongs to another actual hardware binding')
        old=intent['old_device_id']
    else:
        runtime=_document(_read(control,'runtime.json'));old=identifier(runtime.get('device_id'))
    print('Original target: '+old,file=output)
    print('Actual new system UUID: '+actual,file=output)
    print('Original evidence keeps its original attribution. New binding requires new reset qualification and exact candidate/attempt approval.',file=output)
    expected='retarget '+old+' '+actual
    confirmation=_answer(source,output,'Type "'+expected+'" to confirm (empty cancels): ',300)
    if confirmation is None or not confirmation:return None
    if confirmation!=expected:raise Conflict('explicit original-target and actual-new-hardware confirmation required')
    verify()
    pointer=_document(_read(control/'retarget','active.json')) if (control/'retarget/active.json').exists() else None
    if pointer is not None and pointer.get('schema_version')==2 and pointer.get('request_id')==request_id:
        from .boot import _verify_state_identity
        recover=recovery_verifier or (lambda config:_verify_state_identity(config,Path('/boot/quirkbench-state')))
        recover(config)
        with private_lock(control/'runtime-config.lock') as config_fd,private_lock(control/'agent/agent.lock') as agent_fd:
            from .shutdown_local import require_available
            require_available(control)
            verify();recover(config)
            for path,fd in ((control/'runtime-config.lock',config_fd),(control/'agent/agent.lock',agent_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('completed retarget console ownership changed')
            if pending_intent(control,binding_reader=binding_reader) is not None:raise Conflict('retarget completion unavailable')
            receipt=completed(control,request_id,binding_reader=binding_reader)
        print('Retarget already activated for '+receipt['device_id']+'. Original evidence remains retained.',file=output,flush=True)
        return receipt
    if not directory.exists() or not (directory/'source.json').exists():
        prepare_retarget(control,config,request_id,old,actual,verify_target=verify,binding_reader=binding_reader,
                         clearer=clearer,recovery_verifier=recovery_verifier)
    print('Retarget is paused. Finish with the same local request and retained controller invitation after interruption.',file=output)
    url=_answer(source,output,'Controller HTTPS endpoint from target reassign: ',4096)
    if url is None or not url:return None
    endpoint(url);verify();observation=certificate_inspector(url,clock=clock)
    print('Controller: '+url,file=output)
    print('Observed certificate SHA-256: '+observation['certificate_sha256'],file=output)
    print('Compare the full fingerprint with the controller retarget-code output.',file=output)
    approval=_answer(source,output,'Type that full fingerprint to approve (empty cancels): ',64)
    if approval is None or not approval:return None
    sha256(approval)
    if approval!=observation['certificate_sha256']:raise Conflict('retarget fingerprint mismatch; no code transmitted')
    code_id=_answer(source,output,'Code ID from target reassign: ',128)
    if code_id is None or not code_id:return None
    identifier(code_id)
    from .retarget_maintenance import pending_choice,select_invitation
    choice=pending_choice(control,config,request_id,url,approval,code_id,verify_target=verify,binding_reader=binding_reader,
        run=run,clearer=clearer,recovery_verifier=recovery_verifier)
    if choice is not None:
        wording='resume' if choice['action']=='resume' else 'replace'
        print('Pending request: '+choice['source_request_id']+'. Its original key remains private and recoverable.',file=output)
        print('Local replacement cannot cancel remote redemption. A completed remote generation requires explicit revocation and reconciliation before another invitation.',file=output)
        expected=wording+' '+choice['source_request_id']
        confirmation=_answer(source,output,'Type "'+expected+'" to select this invitation (empty cancels): ',160)
        if confirmation is None or not confirmation:return None
        if confirmation!=expected:raise Conflict('explicit pending retarget invitation confirmation required')
        import secrets
        select_invitation(control,config,request_id,url,approval,code_id,choice.get('maintenance_request_id') or 'selection-'+secrets.token_hex(16),
            action=choice['action'],confirmed_request_id=choice['source_request_id'],verify_target=verify,binding_reader=binding_reader,
            run=run,clearer=clearer,recovery_verifier=recovery_verifier)
    prepare_request(control,config,request_id,url,approval,code_id,verify_target=verify,binding_reader=binding_reader,
                    run=run,clearer=clearer,recovery_verifier=recovery_verifier)
    code=_secret(source,output,read_secret)
    if code is None or not code:return None
    result=activator(control,config,request_id,url,observation['certificate_pem'],approval,code_id,code,
        verify_target=verify,binding_reader=binding_reader,run=run,clock=clock,clearer=clearer,
        recovery_verifier=recovery_verifier,client_factory=client_factory)
    print('Retarget activated for '+result['device_id']+'. Original evidence is archived; saved network profiles are not inherited.',file=output,flush=True)
    return result


def connect_retarget(*,input_stream=None,output_stream=None,run=subprocess.run,control=None,config=None,
                     verify_target=None,retarget=None,prerequisites=None):
    from .runtime import CONTROL,boot_context
    control=control or CONTROL
    if verify_target is None:
        config,boot,verify_target=boot_context()
        if boot['quirkbench.mode']!='recovery':raise ContractError('retarget requires verified recovery')
    if config is None:raise ContractError('retarget requires exact verified recovery configuration')
    control,verify_target=_storage(control,verify_target);(prerequisites or require_native_tools)()
    stopped=None
    try:
        stopped=run(['systemctl','stop','quirkbench-supervisor.service'],check=False,capture_output=True,timeout=45)
        if stopped.returncode!=0:raise Conflict('target supervisor could not stop; retarget blocked')
        verify_target()
        return (retarget or run_retarget)(control,config,verify_target=verify_target,input_stream=input_stream,output_stream=output_stream)
    finally:
        if stopped is None or stopped.returncode==0:
            restarted=run(['systemctl','start','quirkbench-supervisor.service'],check=False,capture_output=True,timeout=120)
            if restarted.returncode!=0:raise Conflict('target supervisor restart failed; retarget selection remains retained')
