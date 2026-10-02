"""Strict complete private enrollment reply; never an experiment approval."""
import re
import ssl
from urllib.parse import urlsplit

from .contracts import Conflict,ContractError,canonical,digest,identifier
from .credential_registry import validate_generation
from .enrollment_client import endpoint,MAX_BODY
from .enrollment_proof import validate_request


def validate_result(value,request):
    request=validate_request(request)
    fields={'schema_version','record_type','request_id','request_digest','device_id','media_instance_id',
            'target_binding','controller_url','credential_generation','controller_ca_pem','device_token',
            'repository_certificate_pem','repository_remotes'}
    version=value.get('schema_version') if isinstance(value,dict) else None
    if version==2:fields=fields|{'retarget_invitation'}
    if (not isinstance(value,dict) or set(value)!=fields or type(version) is not int
            or version not in (1,2) or value['record_type']!='enrollment-result'):
        raise ContractError('invalid complete enrollment result')
    identifier(value['device_id']);endpoint(value['controller_url'])
    generation=validate_generation(value['credential_generation'])
    if version==2:
        from .retarget_invitation import validate_invitation
        authority=validate_invitation(value['retarget_invitation']);scope=authority['scope'];code=authority['code']
        if (code['code_id']!=request['code_id'] or code['controller_url']!=value['controller_url']
                or scope['media_instance_id']!=request['media_instance_id']
                or canonical(scope['new_target_binding'])!=canonical(request['target_binding'])
                or scope['old_device_id']==value['device_id'] or scope['old_generation']==generation['generation']):
            raise Conflict('retarget result differs from the exact invitation/request or inherits original identity')
    if (value['request_id']!=request['request_id'] or value['request_digest']!=digest(canonical(request))
            or value['media_instance_id']!=request['media_instance_id']
            or canonical(value['target_binding'])!=canonical(request['target_binding'])
            or generation['device_id']!=value['device_id'] or generation['media_instance_id']!=value['media_instance_id']
            or generation['system_uuid']!=request['target_binding']['system_uuid']):
        raise Conflict('enrollment result differs from retained request/binding')
    token=value['device_token']
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token) or digest(token.encode())!=generation['device_token_sha256']:
        raise ContractError('enrollment result token differs from credential generation')
    for name in ('controller_ca_pem','repository_certificate_pem'):
        pem=value[name]
        if not isinstance(pem,str) or len(pem)>65536:raise ContractError('invalid enrollment certificate bytes')
        try:der=ssl.PEM_cert_to_DER_cert(pem)
        except (ValueError,UnicodeError) as exc:raise ContractError('invalid enrollment certificate PEM') from exc
        if name=='repository_certificate_pem' and digest(der)!=generation['repository_certificate_sha256']:
            raise ContractError('enrollment repository certificate differs from credential generation')
    remotes=value['repository_remotes']
    if not isinstance(remotes,dict) or not 1<=len(remotes)<=8:
        raise ContractError('complete enrollment requires configured repository remotes')
    for alias,remote in remotes.items():
        identifier(alias)
        if not isinstance(remote,dict) or set(remote)!={'url','public_key'}:
            raise ContractError('invalid enrollment repository remote')
        url=remote['url']
        if not isinstance(url,str):raise ContractError('invalid enrollment repository URL')
        parts=urlsplit(url)
        if parts.path!='/'+alias or parts.query or parts.fragment:
            raise ContractError('enrollment repository URL differs from configured alias')
        endpoint(url[:-len(parts.path)])
        key=remote['public_key']
        if (not isinstance(key,str) or len(key)>65536 or not key.isascii()
                or not key.startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----\n')):
            raise ContractError('invalid enrollment repository public key')
    if len(canonical({'schema_version':1,'data':{'value':value}}))>MAX_BODY:
        raise ContractError('complete enrollment response envelope exceeds byte limit')
    return value
