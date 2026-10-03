"""Activate a complete authenticated enrollment through existing private generations."""
from __future__ import annotations

from pathlib import Path
import ssl
import subprocess
import tempfile
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest
from .controller_setup import _durable_directory,_private_path
from .controller_tls import _read,_openssl
from .enrollment import _document,_now
from .enrollment_client import endpoint
from .enrollment_result import validate_result
from .enrollment_target import _storage,_saved,_media
from .maintenance import private_lock
from .provisioning import activate_bundle,validate_signing_key
from .store import atomic_write


def _validate_native(pending,intent,result,private,approved_certificate_pem,verify_target,run, *,temporary_parent=None):
    if not isinstance(approved_certificate_pem,str) or len(approved_certificate_pem)>65536:
        raise ContractError('approved inspection certificate is missing or exceeds limit')
    try:der=ssl.PEM_cert_to_DER_cert(approved_certificate_pem)
    except (ValueError,UnicodeError) as exc:raise ContractError('invalid approved inspection certificate') from exc
    if digest(der)!=intent['certificate_sha256']:
        raise Conflict('inspection certificate differs from retained explicit approval')
    # All secret/native temporary bytes remain on verified evidence/control.
    with tempfile.TemporaryDirectory(prefix='.validate-',dir=pending if temporary_parent is None else temporary_parent) as temporary:
        stage=Path(temporary)
        files={'ca.pem':result['controller_ca_pem'].encode(),'controller.pem':approved_certificate_pem.encode(),
               'repository.crt':result['repository_certificate_pem'].encode(),'repository.key':private}
        for name,raw in files.items():verify_target();atomic_write(stage/name,raw)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            context.load_verify_locations(cafile=stage/'ca.pem')
            context.load_cert_chain(stage/'repository.crt',stage/'repository.key')
        except (OSError,ssl.SSLError) as exc:raise ContractError('enrollment CA/client certificate/private key invalid') from exc
        host,_=endpoint(intent['controller_url'])
        import ipaddress
        try:ipaddress.ip_address(host)
        except ValueError as exc:
            raise ContractError('initial enrollment activation requires the supported literal-IP controller TLS identity') from exc
        _openssl(['verify','-no-CApath','-no-CAstore','-CAfile',str(stage/'ca.pem'),
            '-purpose','sslserver','-verify_ip',host,str(stage/'controller.pem')],run=run)
        _openssl(['verify','-no-CApath','-no-CAstore','-CAfile',str(stage/'ca.pem'),
            '-purpose','sslclient',str(stage/'repository.crt')],run=run)
        for alias,remote in result['repository_remotes'].items():
            path=stage/(alias+'.public.asc');atomic_write(path,remote['public_key'].encode())
            validate_signing_key(path,runner=run,temporary_parent=stage)


def _bundle(result,request,private):
    runtime={'schema_version':1,'device_id':result['device_id'],'controller_url':result['controller_url'],
        'ca':'ca.pem','token_file':'device.token','target_binding':request['target_binding'],'remotes':{}}
    bundle={'ca.pem':result['controller_ca_pem'].encode(),'device.token':result['device_token'].encode(),
            'repository.crt':result['repository_certificate_pem'].encode(),'repository.key':private}
    for alias,remote in result['repository_remotes'].items():
        name=alias+'.public.asc';bundle[name]=remote['public_key'].encode()
        runtime['remotes'][alias]={'url':remote['url'],'ca':'ca.pem','public_key':name,
                                  'client_cert':'repository.crt','client_key':'repository.key'}
    bundle['runtime.json']=canonical(runtime)
    return bundle


def _retain_bundle(directory,bundle,verify_target):
    if set(p.name for p in directory.iterdir())-set(bundle):
        raise Conflict('enrollment activation bundle contains unexpected files')
    for name,data in bundle.items():
        path=directory/name;verify_target()
        if path.exists() or path.is_symlink():
            if _read(directory,name)!=data:raise Conflict('retained enrollment activation bundle changed')
        else:atomic_write(path,data)


def activate_enrollment(control,result,approved_certificate_pem, *, verify_target,
                        binding_reader=read_system_uuid,clock=time.time,run=subprocess.run,
                        activator=activate_bundle,fault_hook=None):
    """Verify the authenticated CA, request key and complete result before activation.

    The public inspection certificate must match the explicit approved fingerprint
    retained before exchange. This initial path cannot retarget/change active trust;
    the existing activation primitive serializes and publishes runtime.json last.
    """
    if isinstance(result,dict) and result.get('schema_version')==2:
        raise Conflict('retarget result requires explicit stopped local retarget activation')
    control,verify_target=_storage(control,verify_target);fault_hook=fault_hook or (lambda _:None)
    with private_lock(control/'runtime-config.lock'):
        from .shutdown_local import require_available
        require_available(control)
        verify_target();pending=_private_path(control/'enrollment/pending')
        intent=_document(_read(pending,'intent.json'));request,private=_saved(pending,intent,run=run)
        _media(control,request['media_instance_id']);verify_binding(request['target_binding'],reader=binding_reader)
        result=validate_result(result,request)
        if result['controller_url']!=intent['controller_url']:
            raise Conflict('enrollment reply controller differs from approved endpoint')
        if not _now(clock)<result['credential_generation']['expires_at']:
            raise Conflict('enrollment credentials expired; explicit lifecycle maintenance required')
        directory=_private_path(pending/'activation-bundle');_durable_directory(directory)
        if directory.stat().st_dev!=control.stat().st_dev:
            raise ContractError('enrollment bundle is on another storage device')
        _validate_native(pending,intent,result,private,approved_certificate_pem,verify_target,run)
        saved=pending/'result.json';raw=canonical(result)
        if saved.exists() or saved.is_symlink():
            if _read(pending,saved.name)!=raw:raise Conflict('target enrollment already retained another complete result')
        else:verify_target();atomic_write(saved,raw)
        fault_hook('enrollment_result_retained')
        bundle=_bundle(result,request,private)
        _retain_bundle(directory,bundle,verify_target)
        fault_hook('enrollment_bundle_retained')
        _media(control,request['media_instance_id']);verify_binding(request['target_binding'],reader=binding_reader)
        if not _now(clock)<result['credential_generation']['expires_at']:
            raise Conflict('enrollment credentials expired during activation preparation')
    def verified_activation():
        verify_target();_media(control,request['media_instance_id']);verify_binding(request['target_binding'],reader=binding_reader)
        if not _now(clock)<result['credential_generation']['expires_at']:
            raise Conflict('enrollment credentials expired during activation')
    activated=activator(directory,control,verify_target=verified_activation,fault=fault_hook,expected_files=bundle)
    return {**activated,'device_id':result['device_id'],'media_instance_id':request['media_instance_id'],
            'credential_generation':result['credential_generation']['generation'],'enrolled':True,'boot_authorized':False}
