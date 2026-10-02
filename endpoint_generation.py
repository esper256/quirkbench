"""Typed URL-only private generation transitions; no activation or transport trust.

These records associate explicit endpoint maintenance with immutable enrollment
bytes. They cannot approve a certificate, authorize execution or replace a CA.
The stopped activation adapter must independently verify native trust/reachability.
"""
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

from .binding import verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _private_path
from .controller_tls import _read
from .enrollment import _document
from .enrollment_client import endpoint
from .retarget_activation import _active

MAX_TRANSITIONS=32
FIELDS={'schema_version','record_type','request_id','enrollment_request_sha256','enrollment_result_sha256',
    'source_generation','destination_generation','source_runtime_sha256','destination_runtime_sha256',
    'approved_certificate_sha256','controller_url','remote_urls'}


def validate_transition(value):
    if (not isinstance(value,dict) or set(value)!=FIELDS or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='target-endpoint-transition'):
        raise ContractError('invalid target endpoint transition')
    identifier(value['request_id'])
    for name in ('enrollment_request_sha256','enrollment_result_sha256','source_generation','destination_generation',
                 'source_runtime_sha256','destination_runtime_sha256','approved_certificate_sha256'):sha256(value[name])
    host,_=endpoint(value['controller_url'])
    if not isinstance(value['remote_urls'],dict) or not 1<=len(value['remote_urls'])<=8:
        raise ContractError('endpoint maintenance requires the exact existing repository aliases')
    for alias,url in value['remote_urls'].items():
        identifier(alias);_remote_url(url,host)
    if value['source_generation']==value['destination_generation']:
        raise Conflict('endpoint transition must select a changed URL generation')
    if len(canonical(value))>65536:raise ContractError('endpoint transition exceeds byte budget')
    return value


def _remote_url(url,host):
    if not isinstance(url,str) or len(url)>4096:raise ContractError('invalid endpoint repository URL')
    parts=urlsplit(url)
    try:port=parts.port
    except ValueError as exc:raise ContractError('invalid endpoint repository port') from exc
    origin=parts.scheme+'://'+parts.netloc
    remote_host,_=endpoint(origin)
    if (remote_host!=host or parts.query or parts.fragment or not parts.path or parts.path.endswith('/')
            or '%' in parts.path or '\\' in parts.path or any(part in ('','..','.') for part in parts.path.split('/')[1:])):
        raise ContractError('repository endpoint must use the controller SAN and an exact safe repository path')
    return parts.path,port


def _bundle(files):
    if not isinstance(files,dict) or not 5<=len(files)<=16 or 'runtime.json' not in files:
        raise ContractError('endpoint maintenance requires a bounded complete private generation')
    for name,raw in files.items():
        if (not isinstance(name,str) or Path(name).name!=name or not isinstance(raw,bytes) or len(raw)>65536):
            raise ContractError('invalid bounded private generation file')
    runtime=_document(files['runtime.json'])
    if (not isinstance(runtime,dict) or set(runtime)!={'schema_version','device_id','controller_url','ca','token_file','target_binding','remotes'}
            or type(runtime['schema_version']) is not int or runtime['schema_version']!=1 or files['runtime.json']!=canonical(runtime)):
        raise Conflict('endpoint transition requires exact enrolled runtime without inherited grants')
    identifier(runtime['device_id']);endpoint(runtime['controller_url'])
    binding=runtime['target_binding'];verify_binding(binding,reader=lambda:binding.get('system_uuid') if isinstance(binding,dict) else None)
    if runtime['ca']!='ca.pem' or runtime['token_file']!='device.token':raise Conflict('endpoint generation must retain enrolled credential names')
    remotes=runtime['remotes']
    if not isinstance(remotes,dict) or not 1<=len(remotes)<=8:raise ContractError('invalid bounded enrolled repository aliases')
    names={'runtime.json','ca.pem','device.token','repository.crt','repository.key'}
    for alias,remote in remotes.items():
        identifier(alias)
        if (not isinstance(remote,dict) or set(remote)!={'url','ca','public_key','client_cert','client_key'}
                or {k:v for k,v in remote.items() if k!='url'}!={'ca':'ca.pem','public_key':alias+'.public.asc','client_cert':'repository.crt','client_key':'repository.key'}):
            raise Conflict('endpoint generation must retain exact enrolled repository credential names')
        names.add(alias+'.public.asc')
    if set(files)!=names:raise Conflict('private endpoint generation contains unknown or missing files')
    manifest={name:digest(raw) for name,raw in sorted(files.items())}
    return runtime,digest(canonical(manifest)),manifest


def transition(files,request_id,enrollment_request_sha256,enrollment_result_sha256,controller_url,remote_urls,approved_certificate_sha256):
    """Construct only a URL delta, copying every credential/trust byte unchanged."""
    runtime,source,manifest=_bundle(files);host,controller_port=endpoint(controller_url)
    if not isinstance(remote_urls,dict) or set(remote_urls)!=set(runtime['remotes']):
        raise Conflict('endpoint migration must explicitly preserve every repository alias')
    new=_document(canonical(runtime));new['controller_url']=controller_url
    for alias,url in remote_urls.items():
        path,port=_remote_url(url,host)
        previous=urlsplit(runtime['remotes'][alias]['url'])
        if path!=previous.path or port==controller_port:raise Conflict('endpoint migration cannot change repository identity or combine protocol/repository ports')
        new['remotes'][alias]['url']=url
    destination_files=files|{'runtime.json':canonical(new)}
    _,destination,_=_bundle(destination_files)
    record=validate_transition({'schema_version':1,'record_type':'target-endpoint-transition','request_id':request_id,
        'enrollment_request_sha256':enrollment_request_sha256,'enrollment_result_sha256':enrollment_result_sha256,
        'source_generation':source,'destination_generation':destination,'source_runtime_sha256':digest(_active(files,source)),
        'destination_runtime_sha256':digest(_active(destination_files,destination)),
        'approved_certificate_sha256':approved_certificate_sha256,'controller_url':controller_url,'remote_urls':dict(remote_urls)})
    return record,destination_files


def verify_transition(record,source_files,destination_files):
    """Recompute the entire permitted delta; hashes alone never broaden its scope."""
    validate_transition(record)
    expected,files=transition(source_files,record['request_id'],record['enrollment_request_sha256'],
        record['enrollment_result_sha256'],record['controller_url'],record['remote_urls'],record['approved_certificate_sha256'])
    if expected!=record or files!=destination_files:raise Conflict('endpoint transition changed credentials, trust, binding or attribution')
    return _active(destination_files,record['destination_generation'])


def read_generation(control,generation):
    """Fixed bounded private files; caller must first hold storage/binding ownership."""
    sha256(generation);directory=_private_path(Path(control)/'generations'/generation)
    manifest_raw=_read(directory,'generation.json');manifest=_document(manifest_raw)
    if (not isinstance(manifest,dict) or not 5<=len(manifest)<=16 or manifest_raw!=canonical(manifest)
            or digest(manifest_raw)!=generation or any(not isinstance(name,str) or Path(name).name!=name for name in manifest)
            or {p.name for p in islice(directory.iterdir(),18)}!=set(manifest)|{'generation.json'}):
        raise Conflict('endpoint generation is incomplete or differs from retained manifest')
    files={name:_read(directory,name) for name in manifest};_,actual,checksums=_bundle(files)
    if actual!=generation or checksums!=manifest:raise Conflict('private endpoint generation bytes changed')
    return files


def verify_chain(records,generations,original_runtime,enrollment_request_sha256,enrollment_result_sha256):
    """Validate an explicitly supplied bounded chain without selecting active state.

    No fallback to another generation, filesystem scanning, credential request or
    actual-hardware substitution occurs. This is provenance, not authorization.
    """
    if not isinstance(records,list) or not 1<=len(records)<=MAX_TRANSITIONS:raise ContractError('endpoint transition history exceeds bounded limit')
    seen=set();requests=set();selected=original_runtime;previous=None
    for record in records:
        validate_transition(record)
        if (record['enrollment_request_sha256']!=enrollment_request_sha256 or record['enrollment_result_sha256']!=enrollment_result_sha256
                or record['request_id'] in requests or record['source_generation'] in seen
                or previous is not None and record['source_generation']!=previous):raise Conflict('endpoint history is cyclic or changes original enrollment')
        if digest(selected)!=record['source_runtime_sha256']:raise Conflict('endpoint history differs from exact preceding runtime')
        source=generations.get(record['source_generation']);destination=generations.get(record['destination_generation'])
        if source is None or destination is None:raise Conflict('endpoint history references unavailable retained generation')
        if _active(source,record['source_generation'])!=selected:raise Conflict('endpoint history source is not exact original runtime')
        selected=verify_transition(record,source,destination)
        seen.add(record['source_generation']);requests.add(record['request_id']);previous=record['destination_generation']
    if previous in seen:raise Conflict('endpoint history returns to an earlier generation')
    if set(generations)!=seen|{previous}:raise Conflict('endpoint history has orphan or unlinked generations')
    return selected
