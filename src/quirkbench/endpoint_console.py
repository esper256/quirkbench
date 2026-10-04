"""Attended same-binding recovery endpoint maintenance over existing ownership."""
from pathlib import Path
import subprocess
import sys
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,digest,identifier,sha256
from .filesystem import _strict_read
from .filesystem import _managed_path
from .enrollment_records import _document
from .enrollment_client import endpoint,inspect_certificate
from .enrollment_console import _answer
from .enrollment_target import _storage
from .endpoint_local import location,validate_intent,prepare
from .endpoint_preflight import preflight
from .endpoint_activation import activate
from .endpoint_rollback import rollback_stopped


def run_endpoint(control,config, *,verify_target,input_stream=None,output_stream=None,
        binding_reader=read_system_uuid,certificate_inspector=inspect_certificate,run=subprocess.run,
        clock=time.time,clearer=None,recovery_verifier=None,preparer=prepare,checker=preflight,
        activator=activate,restorer=rollback_stopped):
    source=input_stream or sys.stdin;output=output_stream or sys.stdout
    control,verify=_storage(control,verify_target)
    runtime_raw=_strict_read(control,'runtime.json');runtime=_document(runtime_raw)
    if not isinstance(runtime,dict):raise ContractError('existing enrolled runtime required')
    verify_binding(runtime.get('target_binding'),reader=binding_reader)
    device=identifier(runtime.get('device_id'));verify()
    request_id=_answer(source,output,'Endpoint request ID (reuse after interruption; empty cancels): ',128)
    if not request_id:return None
    identifier(request_id);directory=location(control,request_id);intent=None
    if (directory/'intent.json').exists() or (directory/'intent.json').is_symlink():
        intent=validate_intent(_document(_strict_read(directory,'intent.json')))
        verify_binding(intent['target_binding'],reader=binding_reader)
        if intent['request_id']!=request_id or intent['device_id']!=device:raise Conflict('endpoint request belongs to another enrolled target')
    print('Target: '+device,file=output)
    print('Current configuration SHA-256: '+digest(runtime_raw),file=output)
    print('Apply or check an approved controller address, or restore this request\'s exact preceding configuration.',file=output)
    action=_answer(source,output,'Action: apply, check, rollback (empty cancels): ',16)
    if not action:return None
    if action not in ('apply','check','rollback'):raise ContractError('choose apply, check or rollback')
    common={'verify_target':verify,'binding_reader':binding_reader,'clearer':clearer,'recovery_verifier':recovery_verifier}
    if action=='rollback':
        source_sha=digest(_strict_read(directory,'source.json'))
        print('Retained source SHA-256: '+source_sha,file=output)
        expected='rollback '+request_id+' '+source_sha
        answer=_answer(source,output,'Type "'+expected+'" (empty cancels): ',210)
        if not answer:return None
        if answer!=expected:raise Conflict('confirm the exact retained source before rollback')
        result=restorer(control,config,request_id,source_sha,**common)
        print('Previous configuration restored. Reachability is unchecked; no boot or attempt is authorized.',file=output,flush=True)
        return result
    source_sha=intent['runtime_sha256'] if intent is not None else digest(runtime_raw)
    print('Selected source configuration SHA-256: '+source_sha,file=output)
    expected='endpoint '+device+' '+source_sha
    answer=_answer(source,output,'Type "'+expected+'" to confirm (empty cancels): ',210)
    if not answer:return None
    if answer!=expected:raise Conflict('confirm the exact selected target configuration')
    captured=(directory/'capture-completion.json').exists() or (directory/'capture-completion.json').is_symlink()
    if not captured:
        url=intent['controller_url'] if intent is not None else _answer(source,output,'New controller HTTPS endpoint: ',4096)
        if not url:return None
        endpoint(url)
        print('Controller: '+url,file=output)
        staged=_answer(source,output,'Staged public certificate basename in setup (empty observes endpoint): ',128)
        if staged is None:return None
        if staged:
            identifier(staged);pem=_strict_read(_managed_path(control/'setup'),staged).decode('ascii')
            import ssl
            observation={'certificate_pem':pem,'certificate_sha256':digest(ssl.PEM_cert_to_DER_cert(pem))}
        elif (directory/'approved-controller.pem').exists():
            import ssl
            pem=_strict_read(directory,'approved-controller.pem').decode('ascii')
            observation={'certificate_pem':pem,'certificate_sha256':digest(ssl.PEM_cert_to_DER_cert(pem))}
        else:
            verify();observation=certificate_inspector(url,clock=clock)
        print('Certificate SHA-256: '+observation['certificate_sha256'],file=output)
        print('Compare with the controller maintenance output before approving.',file=output)
        approval=_answer(source,output,'Type the full controller fingerprint (empty cancels): ',64)
        if not approval:return None
        sha256(approval)
        if approval!=observation['certificate_sha256'] or intent is not None and approval!=intent['approved_certificate_sha256']:
            raise Conflict('endpoint fingerprint differs from explicit approved identity')
        if intent is not None:urls=intent['remote_urls']
        else:
            aliases=runtime.get('remotes')
            if not isinstance(aliases,dict) or not 1<=len(aliases)<=8:raise ContractError('existing bounded repository aliases required')
            urls={}
            for alias in sorted(aliases):
                identifier(alias);url_value=_answer(source,output,'New repository HTTPS URL for '+alias+': ',4096)
                if not url_value:return None
                urls[alias]=url_value
        preparer(control,config,request_id,device,source_sha,url,urls,observation['certificate_pem'],approval,**common)
    print('Prepared source SHA-256: '+digest(_strict_read(directory,'source.json')),file=output)
    result=(activator if action=='apply' else checker)(control,config,request_id,run=run,clock=clock,**common)
    print('Endpoint activated. Existing evidence and enrollment remain intact; no boot or attempt is authorized.' if action=='apply'
        else 'Trust and reachability checked. Endpoint remains paused until apply or rollback.',file=output,flush=True)
    return result


def connect_endpoint(*,endpoint=None,**kwargs):
    # Reuse the existing verified recovery/supervisor stop-and-restart facade.
    from .retarget_console import connect_retarget
    return connect_retarget(retarget=endpoint or run_endpoint,**kwargs)
