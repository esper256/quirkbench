"""Initial target request/key persistence on verified private evidence control.

No enrollment success, credential activation or retargeting is implied. A required
boot/evidence verifier and actual SMBIOS binding precede all persistent writes.
"""
from __future__ import annotations

import base64
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _durable_directory,_managed_path
from .controller_tls import _openssl,_read
from .enrollment import _document,_now
from .enrollment_client import endpoint
from .enrollment_proof import _bytes,validate_request,validate_challenge,verify_signature
from .maintenance import private_lock,nested_mounts
from .store import atomic_write


def _intent(value):
    fields={'schema_version','controller_url','certificate_sha256','code_id','media_instance_id','target_binding','request_id'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1):
        raise ContractError('invalid private enrollment intent')
    endpoint(value['controller_url']);sha256(value['certificate_sha256'])
    for name in ('code_id','media_instance_id','request_id'):identifier(value[name])
    binding=value['target_binding']
    verify_binding(binding,reader=lambda:binding.get('system_uuid') if isinstance(binding,dict) else None)
    return value


def _storage(control,verify_target):
    verify_target();control=_managed_path(control)
    if not control.is_dir():raise ContractError('verified evidence control directory is missing')
    original=verify_target
    def verify():
        original();device=control.stat().st_dev
        from .provisioning import require_control_access
        require_control_access(control)
        if any(Path(path)!=control for path in nested_mounts(control)):
            raise ContractError('private enrollment control contains nested mounts')
        for path in (control/'enrollment',control/'enrollment/pending'):
            _managed_path(path)
            if path.exists() and path.stat().st_dev!=device:
                raise ContractError('private enrollment destination is on another storage device')
        for path in (control/'media-instance.json',control/'runtime-config.lock',control/'runtime.json',
                     *(control/'enrollment/pending'/name for name in ('intent.json','key.pem','request.json'))):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or path.stat().st_dev!=device:
                    raise ContractError('private enrollment file is on another storage device or linked')
    verify()
    return control,verify


def _public(directory,private, *, run):
    with tempfile.TemporaryDirectory(prefix='.key-check-',dir=directory) as temporary:
        key=Path(temporary)/'key.pem';atomic_write(key,private)
        return _openssl(['pkey','-in',str(key),'-pubout','-outform','DER'],run=run)


def _saved(directory,intent, *, run):
    intent=_intent(intent)
    saved=validate_request(_document(_read(directory,'request.json')))
    private=_read(directory,'key.pem')
    if (saved['request_id']!=intent['request_id'] or saved['code_id']!=intent['code_id']
            or saved['media_instance_id']!=intent['media_instance_id']
            or saved['target_binding']!=intent['target_binding']
            or _bytes(saved['public_key'],44)!=_public(directory,private,run=run)):
        raise Conflict('private enrollment request/key differs from retained intent')
    return saved,private


def _media(control,expected):
    value=_document(_read(control,'media-instance.json'))
    if value!={'schema_version':1,'media_instance_id':expected} or type(value.get('schema_version')) is not int:
        raise Conflict('private media identity differs from retained enrollment request')


def prepare_request(control,controller_url,approved_fingerprint,code_id, *, verify_target,
                    binding_reader=read_system_uuid,run=subprocess.run,fault_hook=None):
    """Retain key and request before exchange; identical retry never replaces them."""
    endpoint(controller_url);sha256(approved_fingerprint);identifier(code_id)
    control,verify_target=_storage(control,verify_target)
    fault_hook=fault_hook or (lambda _:None)
    with private_lock(control/'runtime-config.lock'):
        from .shutdown_local import require_available
        require_available(control)
        # A selected maintenance operation must finish before any new key can
        # appear at the public pending location, including after the first rename.
        from .enrollment_maintenance import reconcile_selection
        reconcile_selection(control,controller_url,approved_fingerprint,code_id,
                            verify_target=verify_target,binding_reader=binding_reader,run=run)
        return _prepare_locked(control,control/'enrollment/pending',controller_url,approved_fingerprint,
                               code_id,verify_target=verify_target,binding_reader=binding_reader,
                               run=run,fault_hook=fault_hook)


def _prepare_locked(control,directory,controller_url,approved_fingerprint,code_id, *,
                    verify_target,binding_reader,run,fault_hook):
    """Internal preparation under the existing configuration lock."""
    verify_target()
    if (control/'runtime.json').exists() or (control/'runtime.json').is_symlink():
        raise Conflict('existing target configuration requires explicit lifecycle maintenance')
    return _prepare_at(control,directory,controller_url,approved_fingerprint,code_id,
        verify_target=verify_target,binding_reader=binding_reader,run=run,fault_hook=fault_hook)


def _prepare_at(control,directory,controller_url,approved_fingerprint,code_id, *,
                verify_target,binding_reader,run,fault_hook):
    """Private request primitive; callers own initial/retarget policy and locks."""
    verify_target()
    binding={'schema_version':1,'system_uuid':binding_reader()};verify_binding(binding,reader=binding_reader)
    directory=_managed_path(directory);_durable_directory(directory)
    media=control/'media-instance.json'
    if media.exists() or media.is_symlink():
        value=_document(_read(control,media.name))
        if (not isinstance(value,dict) or set(value)!={'schema_version','media_instance_id'}
                or type(value['schema_version']) is not int or value['schema_version']!=1):
            raise ContractError('invalid private media identity')
        identifier(value['media_instance_id'])
    else:
        value={'schema_version':1,'media_instance_id':'media-'+secrets.token_hex(16)}
        verify_target();atomic_write(media,canonical(value))
    fault_hook('media_retained')
    expected={'schema_version':1,'controller_url':controller_url,'certificate_sha256':approved_fingerprint,
              'code_id':code_id,'media_instance_id':value['media_instance_id'],'target_binding':binding}
    marker=directory/'intent.json'
    if marker.exists() or marker.is_symlink():
        intent=_intent(_document(_read(directory,marker.name)))
        if (not isinstance(intent,dict) or set(intent)!=set(expected)|{'request_id'}
                or any(intent.get(k)!=v for k,v in expected.items())):
            raise Conflict('retained enrollment intent requires explicit maintenance')
        identifier(intent['request_id'])
    else:
        if any(directory.iterdir()):raise Conflict('unidentified enrollment key/request state')
        intent={**expected,'request_id':'enroll-'+secrets.token_hex(16)}
        verify_target();atomic_write(marker,canonical(intent))
    fault_hook('intent_retained')
    request_path=directory/'request.json';key_path=directory/'key.pem'
    if request_path.exists() or request_path.is_symlink():
        saved,_=_saved(directory,intent,run=run)
    else:
        if key_path.exists() or key_path.is_symlink():private=_read(directory,key_path.name)
        else:
            private=_openssl(['genpkey','-algorithm','ED25519'],run=run)
            if not private:raise ContractError('native enrollment key is empty')
            verify_target();atomic_write(key_path,private)
        fault_hook('key_retained')
        public=_public(directory,private,run=run)
        saved=validate_request({'schema_version':1,'request_id':intent['request_id'],'code_id':code_id,
            'media_instance_id':value['media_instance_id'],'target_binding':binding,
            'public_key':base64.b64encode(public).decode()})
        verify_target();verify_binding(binding,reader=binding_reader)
        atomic_write(request_path,canonical(saved))
    fault_hook('request_retained')
    verify_target();verify_binding(binding,reader=binding_reader);_media(control,saved['media_instance_id'])
    return saved


def sign_challenge(control,challenge, *, verify_target,binding_reader=read_system_uuid,
                   clock=time.time,run=subprocess.run):
    """Sign only the retained request and approved certificate domain, no secret logs."""
    challenge=validate_challenge(challenge);now=_now(clock)
    control,verify_target=_storage(control,verify_target)
    with private_lock(control/'runtime-config.lock'):
        from .shutdown_local import require_available
        require_available(control)
        return _sign_at(control,control/'enrollment/pending',challenge,now,
            verify_target=verify_target,binding_reader=binding_reader,clock=clock,run=run)


def _sign_at(control,directory,challenge,now, *,verify_target,binding_reader,clock,run):
    """Private proof primitive; callers fence the exact namespace and authority."""
    verify_target();directory=_managed_path(directory)
    intent=_document(_read(directory,'intent.json'));request,private=_saved(directory,intent,run=run)
    _media(control,request['media_instance_id'])
    verify_binding(request['target_binding'],reader=binding_reader)
    if (challenge['request_id']!=request['request_id'] or challenge['request_digest']!=digest(canonical(request))
            or challenge['certificate_sha256']!=intent['certificate_sha256']
            or not now<challenge['expires_at']<=now+60):
        raise Conflict('challenge differs from retained request/trust or is expired')
    with tempfile.TemporaryDirectory(prefix='.proof-',dir=directory) as temporary:
        stage=Path(temporary)
        atomic_write(stage/'key.pem',private);atomic_write(stage/'message',canonical(challenge))
        signature=_openssl(['pkeyutl','-sign','-inkey',str(stage/'key.pem'),'-rawin','-in',str(stage/'message')],run=run)
    if len(signature)!=64:raise ContractError('invalid native enrollment signature')
    verify_signature(_bytes(request['public_key'],44),canonical(challenge),signature,run=run,temporary_parent=directory)
    if not now<=_now(clock)<challenge['expires_at']:
        raise Conflict('enrollment challenge expired or target clock moved backwards during proof')
    verify_target();verify_binding(request['target_binding'],reader=binding_reader);_media(control,request['media_instance_id'])
    return base64.b64encode(signature).decode()
