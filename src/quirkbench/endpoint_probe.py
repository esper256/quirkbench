"""Native original-CA validation and bounded read-only endpoint admission checks."""
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import Conflict,ContractError,canonical,digest
from .filesystem import _managed_path
from .enrollment_records import _now, _document
from .enrollment_activation import _bundle,_validate_native
from .enrollment_crypto import validate_request, _bytes
from .enrollment_result import validate_result
from .enrollment_target import _public
from .endpoint_generation import verify_transition,_bundle as validate_bundle
from .release_http import _response,_length,_remaining
from .transport import TransportError


def probe(record,source_files,destination_files,request,result,approved_certificate_pem, *,
          temporary_parent,verify_target,run,clock=time.time,monotonic=time.monotonic,response=_response,deadline=None,final_checks=None):
    """Caller owns stopped recovery/storage/binding and exact captured inputs.

    Preserve original authenticated enrollment documents. No pointer, generation,
    journal, contact, registration, work or approval is published by this adapter.
    """
    parent=_managed_path(temporary_parent)
    if not parent.is_dir():raise ContractError('owned private endpoint staging directory required')
    deadline=monotonic()+120 if deadline is None else deadline
    _remaining(deadline,monotonic)
    frozen_record=canonical(record);frozen_request=canonical(request);frozen_result=canonical(result)
    captured_source=dict(source_files);captured_destination=dict(destination_files)
    validity=[]
    def final_exact():
        _remaining(deadline,monotonic)
        if (canonical(record)!=frozen_record or canonical(request)!=frozen_request or canonical(result)!=frozen_result
                or source_files!=captured_source or destination_files!=captured_destination):
            raise Conflict('endpoint preflight captured inputs changed')
        if not _now(clock)<result['credential_generation']['expires_at']:raise Conflict('endpoint credentials expired during preflight')
        now=_now(clock)
        if any(not start<=now<end for start,end in validity):raise Conflict('endpoint native trust validity changed during preflight')
    def exact():
        verify_target();final_exact()
    native_run=run
    def run(argv,**kwargs):
        exact();kwargs['timeout']=min(kwargs.get('timeout',15),_remaining(deadline,monotonic))
        try:return native_run(argv,**kwargs)
        finally:exact()
    verify_target();validate_request(request);validate_result(result,request)
    if digest(frozen_request)!=record['enrollment_request_sha256'] or digest(frozen_result)!=record['enrollment_result_sha256']:
        raise Conflict('endpoint transition differs from exact original enrollment evidence')
    verify_transition(record,source_files,destination_files)
    original=_bundle(result,request,source_files['repository.key']);source_runtime,_,_=validate_bundle(source_files)
    if (any(source_files.get(name)!=raw for name,raw in original.items() if name!='runtime.json')
            or source_runtime['device_id']!=result['device_id'] or source_runtime['target_binding']!=request['target_binding']
            or set(source_runtime['remotes'])!=set(result['repository_remotes'])
            or any(urlsplit(remote['url']).path!=urlsplit(result['repository_remotes'][alias]['url']).path for alias,remote in source_runtime['remotes'].items())):
        raise Conflict('endpoint generation cannot replace original enrolled credentials or repository identity')
    exact()
    if _public(parent,source_files['repository.key'],run=run)!=_bytes(request['public_key'],44):
        raise Conflict('endpoint source private key differs from original enrollment request')
    exact()
    # Native checks retain the original authenticated CA and client credentials.
    # This ephemeral view never rewrites the immutable enrollment result.
    native_result=_document(frozen_result);native_result['controller_url']=record['controller_url']
    for alias,url in record['remote_urls'].items():native_result['repository_remotes'][alias]['url']=url
    intent={'controller_url':record['controller_url'],'certificate_sha256':record['approved_certificate_sha256']}
    _validate_native(parent,intent,native_result,source_files['repository.key'],approved_certificate_pem,exact,run)
    exact()
    import tempfile
    from .store import atomic_write
    from .tls_primitives import _openssl
    from .tls_primitives import _dates
    with tempfile.TemporaryDirectory(prefix='.endpoint-validity-',dir=parent) as temporary:
        for name,raw in {'ca.pem':destination_files['ca.pem'],'controller.pem':approved_certificate_pem.encode(),
                         'repository.crt':destination_files['repository.crt']}.items():
            exact();path=Path(temporary)/name;atomic_write(path,raw)
            validity.append(_dates(_openssl(['x509','-in',str(path),'-dates','-noout'],run=run)));exact()
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.hostname_checks_common_name=False
    context.verify_flags|=ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=approved_certificate_pem)
    headers={'Content-Type':'application/json','Cache-Control':'no-store','X-Device-ID':result['device_id'],'Authorization':'Bearer '+result['device_token']}
    with response(record['controller_url']+'/v1/endpoint-check',deadline,monotonic,method='POST',body=canonical({'schema_version':1}),
                  headers=headers,context=context,expected_peer_sha256=record['approved_certificate_sha256']) as reply:
        exact();raw=_body(reply,1024,deadline,monotonic)
        expected={'schema_version':1,'data':{'value':{'device_id':result['device_id'],'credential_accepted':True,'work_queued':False}}}
        if reply.getheader('Content-Type','').split(';')[0]!='application/json' or canonical(_document(raw))!=canonical(expected):
            raise TransportError('endpoint credential acceptance reply differs')
    exact()
    # Client identity remains private; native exact-leaf trust is established
    # before TLS client proof. Native validation already proved this leaf uses
    # the independently retained original CA and matches the approved full pin.
    with tempfile.TemporaryDirectory(prefix='.endpoint-client-',dir=parent) as temporary:
        stage=Path(temporary)
        for name in ('repository.crt','repository.key'):exact();atomic_write(stage/name,destination_files[name])
        context.load_cert_chain(stage/'repository.crt',stage/'repository.key');exact()
        checks={}
        for alias,url in sorted(record['remote_urls'].items()):
            exact()
            with response(url+'/config',deadline,monotonic,context=context,
                          expected_peer_sha256=record['approved_certificate_sha256']) as reply:
                raw=_body(reply,65536,deadline,monotonic);checks[alias]=digest(raw)
            exact()
    exact()
    if final_checks is not None:final_checks.append(final_exact)
    return {'native_trust_verified':True,'protocol_credential_accepted':True,'repository_credential_accepted':True,
        'repository_config_sha256':checks,'reachability_verified':True,'activated':False,'boot_authorized':False}


def _body(reply,limit,deadline,monotonic):
    if reply.status!=200:raise TransportError('endpoint preflight HTTP '+str(reply.status))
    size=_length(reply,limit);raw=bytearray()
    while len(raw)<size:
        _remaining(deadline,monotonic);chunk=reply.read1(min(4096,size-len(raw)));_remaining(deadline,monotonic)
        if not chunk:raise TransportError('incomplete endpoint preflight response')
        raw.extend(chunk)
    return bytes(raw)
