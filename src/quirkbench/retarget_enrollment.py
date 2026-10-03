"""Retained new request/key proof inside explicitly paused retarget preparation.

No old target secret is authenticated, no new runtime is activated and original
spool attribution remains in place. The ordinary pinned TLS exchange is separate.
"""
from contextlib import contextmanager,ExitStack
from pathlib import Path
import os
import subprocess
import time
from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,canonical,digest,identifier,sha256
from .controller_setup import _managed_path
from .controller_tls import _read
from .enrollment import _document,_now
from .enrollment_client import endpoint
from .enrollment_proof import validate_challenge
from .enrollment_target import _storage,_media,_prepare_at,_sign_at,_intent,_saved
from .maintenance import private_lock
from .retarget_local import pending_intent,_location,_capture_source,validate_source
from .release_http import _remaining


class _FinalChecks(list):
    def __init__(self,deadline,clock):super().__init__();self.locks=ExitStack();self.deadline=deadline;self.clock=clock
    def hold(self,context):return self.locks.enter_context(context)
    def check_deadline(self):_remaining(self.deadline,self.clock)


@contextmanager
def _paused_source(control,config,request_id, *,verify_target,binding_reader,clearer,recovery_verifier,monotonic):
    from .boot import clear_once,_verify_state_identity
    identifier(request_id);deadline=monotonic()+120
    recover=recovery_verifier or (lambda config:_verify_state_identity(config,Path('/boot/quirkbench-state')))
    recover(config);control,storage=_storage(control,verify_target);directory=_location(control,request_id)
    with private_lock(control/'runtime-config.lock') as config_fd,ExitStack() as ownership:
        from .shutdown_local import require_available
        require_available(control)
        archived=directory/'archive/agent'
        agent=_managed_path(archived if (directory/'activation.json').exists() and archived.exists() else control/'agent')
        if not agent.is_dir():raise Conflict('original retarget spool unavailable; no new initialization permitted')
        original_fd=ownership.enter_context(private_lock(agent/'agent.lock'))
        intent=pending_intent(control)
        if intent is None or intent['request_id']!=request_id:raise Conflict('exact paused local retarget intent required')
        if digest(canonical(config.to_dict()))!=intent['boot_config_sha256']:raise Conflict('retarget boot identity differs from retained preparation')
        directory=_location(control,request_id);source_raw=_read(directory,'source.json');source=validate_source(_document(source_raw))
        if source['intent_sha256']!=digest(canonical(intent)):raise Conflict('retarget source differs from exact prepared intent')
        def basic():
            if monotonic()>=deadline:raise TimeoutError('retarget proof preparation deadline exceeded; maintenance remains paused')
            storage();recover(config);verify_binding(intent['new_target_binding'],reader=binding_reader)
            current_agent=directory/'archive/agent' if (directory/'activation.json').exists() and (directory/'archive/agent').exists() else control/'agent'
            for path,fd in ((control/'runtime-config.lock',config_fd),(current_agent/'agent.lock',original_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('original retarget lock ownership changed')
            _media(control,intent['media_instance_id'])
            if pending_intent(control)!=intent or _read(directory,'source.json')!=source_raw:
                raise Conflict('retarget paused authority or original source map changed')
            if intent['schema_version']==3:
                from .retarget_endpoint import public
                public(control,intent)
            if monotonic()>=deadline:raise TimeoutError('retarget proof preparation deadline exceeded; maintenance remains paused')
        basic();(clearer or clear_once)(config);basic()
        def locations():
            from .retarget_activation import original_locations
            return original_locations(control,directory,intent)
        def guard():
            basic()
            if _capture_source(control,intent,basic,locations=locations())!=source:raise Conflict('original retarget source changed before new-key proof')
            _remaining(deadline,monotonic)
        final_checks=_FinalChecks(deadline,monotonic)
        with final_checks.locks:
            guard();yield control,directory,intent,guard,final_checks
            guard()
            for check in final_checks:check()
            _remaining(deadline,monotonic)


def _retained_new(directory):
    return {name:_read(_managed_path(directory),name) for name in ('intent.json','request.json','key.pem')}


def _exact_new(directory,expected):
    if _retained_new(directory)!=expected:raise Conflict('new retarget request/key/intent changed during native proof')


def prepare_request(control,config,request_id,controller_url,approved_fingerprint,code_id, *,
                    verify_target,binding_reader=read_system_uuid,run=subprocess.run,
                    clearer=None,recovery_verifier=None,fault_hook=None,monotonic=time.monotonic):
    """New private namespace, same immutable initial request/key primitive."""
    endpoint(controller_url);sha256(approved_fingerprint);identifier(code_id)
    with _paused_source(control,config,request_id,verify_target=verify_target,binding_reader=binding_reader,
            clearer=clearer,recovery_verifier=recovery_verifier,monotonic=monotonic) as (control,directory,intent,guard,final_checks):
        original=_document(_read(control,'runtime.json'))
        if controller_url!=original['controller_url']:raise Conflict('retarget proof cannot also migrate the original controller endpoint')
        from .retarget_maintenance import reconcile_selection
        selection_exact=reconcile_selection(control,directory,intent,controller_url,approved_fingerprint,code_id,guard,binding_reader,run)
        pending=directory/'enrollment/pending'
        prepared=_prepare_at(control,pending,controller_url,approved_fingerprint,code_id,
            verify_target=guard,binding_reader=binding_reader,run=run,fault_hook=fault_hook or (lambda _:None))
        selection_exact()
        retained=_retained_new(pending);private_intent=_intent(_document(retained['intent.json']))
        if (canonical(prepared)!=retained['request.json'] or any(private_intent[key]!=value for key,value in {
                'controller_url':controller_url,'certificate_sha256':approved_fingerprint,'code_id':code_id,
                'request_id':prepared['request_id'],'media_instance_id':intent['media_instance_id'],
                'target_binding':intent['new_target_binding']}.items())):
            raise Conflict('new retarget request differs from exact prepared trust/identity')
        guard();saved,private=_saved(pending,private_intent,run=run)
        if saved!=prepared or private!=retained['key.pem']:raise Conflict('new retarget request/key differs from retained preparation')
        final_checks.append(lambda:_exact_new(pending,retained))
        final_checks.append(selection_exact)
        return prepared


def sign_challenge(control,config,request_id,challenge, *,verify_target,binding_reader=read_system_uuid,
                   run=subprocess.run,clock=time.time,clearer=None,recovery_verifier=None,monotonic=time.monotonic):
    """Fresh proof uses only the retained new key; original key is never signed."""
    challenge=validate_challenge(challenge);now=_now(clock)
    with _paused_source(control,config,request_id,verify_target=verify_target,binding_reader=binding_reader,
            clearer=clearer,recovery_verifier=recovery_verifier,monotonic=monotonic) as (control,directory,intent,guard,final_checks):
        from .retarget_maintenance import reject_unfinished
        selection_exact=reject_unfinished(control,directory,intent,guard)
        pending=directory/'enrollment/pending';retained=_retained_new(pending)
        final_checks.append(lambda:_exact_new(pending,retained))
        def fresh():
            if not now<=_now(clock)<challenge['expires_at']:raise Conflict('retarget challenge expired during final source fence')
        final_checks.append(selection_exact)
        final_checks.append(fresh)
        return _sign_at(control,pending,challenge,now,
            verify_target=guard,binding_reader=binding_reader,clock=clock,run=run)


def _same_source_scope(result,original,intent):
    """Authenticated v2 alone can assert the original controller fence."""
    if result['schema_version']!=2:
        raise Conflict('paused retarget requires an authenticated versioned retarget result')
    scope=result['retarget_invitation']['scope']
    expected={'schema_version':1,'old_device_id':intent['old_device_id'],
        'old_generation':original['credential_generation']['generation'],
        'old_generation_sha256':digest(canonical(original['credential_generation'])),
        'media_instance_id':intent['media_instance_id'],'old_target_binding':intent['old_target_binding'],
        'new_target_binding':intent['new_target_binding']}
    if scope!=expected:raise Conflict('authenticated retarget scope differs from frozen original enrollment')
    if any(result[key]!=original[key] for key in ('controller_url','controller_ca_pem','repository_remotes')):
        raise Conflict('retarget cannot also replace original controller or repository trust; explicit endpoint maintenance required')


def _verify_original_ca(pending,original,pem,private_intent,guard,run):
    """Validate the public approved server leaf under the retained original CA."""
    import ipaddress
    import ssl
    import tempfile
    from .controller_tls import _openssl
    from .contracts import ContractError
    from .store import atomic_write
    host,_=endpoint(private_intent['controller_url'])
    try:ipaddress.ip_address(host)
    except ValueError as exc:raise ContractError('retarget requires the supported literal-IP controller TLS identity') from exc
    if not isinstance(pem,str) or len(pem)>65536:raise ContractError('invalid approved retarget certificate')
    try:der=ssl.PEM_cert_to_DER_cert(pem)
    except (ValueError,UnicodeError) as exc:raise ContractError('invalid approved retarget certificate') from exc
    if digest(der)!=private_intent['certificate_sha256']:raise Conflict('retarget certificate differs from retained explicit approval')
    guard()
    with tempfile.TemporaryDirectory(prefix='.retarget-ca-',dir=pending.parent) as temporary:
        stage=Path(temporary)
        guard();atomic_write(stage/'ca.pem',original['controller_ca_pem'].encode())
        guard();atomic_write(stage/'server.pem',pem.encode())
        _openssl(['verify','-no-CApath','-no-CAstore','-CAfile',str(stage/'ca.pem'),
            '-purpose','sslserver','-verify_ip',host,str(stage/'server.pem')],run=run)
    guard()


def exchange(control,config,request_id,controller_url,approved_certificate_pem,approved_fingerprint,code_id,code, *,
             verify_target,binding_reader=read_system_uuid,run=subprocess.run,clock=time.time,
             clearer=None,recovery_verifier=None,fault_hook=None,monotonic=time.monotonic,client_factory=None):
    """Fresh authenticated exchange retains a complete bundle, still paused.

    Every retry obtains a fresh challenge, including an ACK lost after controller
    completion. A local staged result never substitutes for the controller fence.
    """
    import re
    from .contracts import ContractError
    from .controller_setup import _durable_directory
    from .enrollment_activation import _validate_native,_bundle,_retain_bundle
    from .enrollment_client import PinnedEnrollmentClient
    from .enrollment_result import validate_result
    from .store import atomic_write
    endpoint(controller_url);sha256(approved_fingerprint);identifier(code_id)
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',code):raise ContractError('invalid one-use retarget code')
    fault=fault_hook or (lambda _:None);_now(clock)
    with _paused_source(control,config,request_id,verify_target=verify_target,binding_reader=binding_reader,
            clearer=clearer,recovery_verifier=recovery_verifier,monotonic=monotonic) as (control,directory,intent,guard,final_checks):
        from .retarget_activation import original_locations
        from .retarget_endpoint import original as original_endpoint
        original=original_endpoint(control,intent,locations=original_locations(control,directory,intent))
        if controller_url!=original['controller_url']:raise Conflict('retarget cannot also migrate original controller endpoint')
        from .retarget_maintenance import reconcile_selection
        selection_exact=reconcile_selection(control,directory,intent,controller_url,approved_fingerprint,code_id,guard,binding_reader,run)
        pending=directory/'enrollment/pending'
        request=_prepare_at(control,pending,controller_url,approved_fingerprint,code_id,
            verify_target=guard,binding_reader=binding_reader,run=run,fault_hook=fault)
        selection_exact()
        retained=_retained_new(pending);private_intent=_intent(_document(retained['intent.json']))
        saved,private=_saved(pending,private_intent,run=run)
        if (saved!=request or retained['request.json']!=canonical(request) or retained['key.pem']!=private
                or any(private_intent[key]!=value for key,value in {'controller_url':controller_url,
                    'certificate_sha256':approved_fingerprint,'code_id':code_id,'request_id':request['request_id'],
                    'media_instance_id':intent['media_instance_id'],'target_binding':intent['new_target_binding']}.items())):
            raise Conflict('retarget request/key differs from explicit approved identity')
        def exact():_exact_new(pending,retained);selection_exact()
        def verified():guard();exact()
        _verify_original_ca(pending,original,approved_certificate_pem,private_intent,verified,run)
        client=(client_factory or PinnedEnrollmentClient)(controller_url,approved_certificate_pem,approved_fingerprint,
                                                        clock=clock,monotonic=monotonic)
        verified();challenge=validate_challenge(client.post('/v1/enrollment/challenge',{'schema_version':1,'request':request}))
        verified();signature=_sign_at(control,pending,challenge,_now(clock),verify_target=verified,
            binding_reader=binding_reader,clock=clock,run=run)
        verified();result=client.post('/v1/enrollment/redeem',{'schema_version':1,'request':request,
            'challenge_id':challenge['challenge_id'],'code':code,'signature':signature})
        verified();result=validate_result(result,request);_same_source_scope(result,original,intent)
        invitation_code=result['retarget_invitation']['code']
        if (invitation_code['code_id']!=code_id or invitation_code['controller_url']!=controller_url
                or invitation_code['certificate_sha256']!=approved_fingerprint):
            raise Conflict('authenticated retarget invitation differs from explicitly approved code/trust')
        def fresh():
            if not _now(clock)<result['credential_generation']['expires_at']:raise Conflict('new retarget credentials expired; maintenance remains paused')
        fresh();_validate_native(pending,private_intent,result,private,approved_certificate_pem,verified,run,temporary_parent=directory)
        verified();fresh();raw=canonical(result)
        path=pending/'result.json'
        if path.exists() or path.is_symlink():
            if _read(pending,'result.json')!=raw:raise Conflict('retarget already retained a different complete result')
        else:atomic_write(path,raw)
        fault('retarget_result_retained');verified();fresh()
        bundle=_bundle(result,request,private);bundle_dir=_managed_path(pending/'activation-bundle')
        _durable_directory(bundle_dir);verified();_retain_bundle(bundle_dir,bundle,verified)
        fault('retarget_bundle_retained');verified();fresh()
        def complete():
            exact()
            if _read(pending,'result.json')!=raw:raise Conflict('authenticated retarget result changed before receipt')
            if (set(p.name for p in bundle_dir.iterdir())!=set(bundle)
                    or any(_read(bundle_dir,name)!=data for name,data in bundle.items())):
                raise Conflict('authenticated retarget bundle changed before receipt')
            fresh()
        final_checks.append(complete)
        return {'request_id':request_id,'enrollment_request_id':request['request_id'],'device_id':result['device_id'],
            'credential_generation':result['credential_generation']['generation'],'result_file':str(path),
            'new_files':{**{name:digest(data) for name,data in retained.items()},'result.json':digest(raw)},
            'bundle_files':{name:digest(data) for name,data in bundle.items()},
            'prepared':True,'activated':False,'boot_authorized':False}
