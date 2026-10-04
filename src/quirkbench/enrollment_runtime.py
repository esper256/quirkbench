"""Repository/enrollment adapters inside the existing controller service owner."""
from .tls_primitives import _dates
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
from pathlib import Path
import subprocess
import ssl
import tempfile
import threading
import time

from .contracts import Conflict,ContractError,canonical,digest
from .enrollment_client import endpoint
from .enrollment_service import EnrollmentService
from .enrollment_records import _document, _now
from .controller_tls import FILES, inspect_identity, load_identity
from .tls_primitives import _openssl
from .filesystem import _read
from .store import atomic_write


@dataclass
class Publication:
    application: object = None
    capabilities: object = None
    tls_context: object = None




def _contexts(config, *, run,tls_inspector):
    directory=Path(config['cert']).parent
    observed=(tls_inspector(directory,host=config.get('host','127.0.0.1')) if tls_inspector is not None
              else inspect_identity(directory,host=config.get('host','127.0.0.1'),run=run))
    identity=load_identity(_read(directory,'identity.json'))
    captured={name:_read(directory,name) for name in FILES}
    if (digest(canonical(identity))!=observed['identity_sha256']
            or any(digest(raw)!=identity['files'][name] for name,raw in captured.items())
            or observed['certificate']!=config['cert'] or observed['key']!=config['key']):
        raise Conflict('listener identity changed after native validation')
    protocol=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);repository=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    validity=[]
    with tempfile.TemporaryDirectory(prefix='quirkbench-listener-tls-') as temporary:
        stage=Path(temporary)
        for name,raw in captured.items():atomic_write(stage/name,raw)
        for context in (protocol,repository):
            context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(stage/'controller.crt',stage/'controller.key')
        repository.load_verify_locations(cadata=captured['ca.crt'].decode('ascii'));repository.verify_mode=ssl.CERT_REQUIRED
        for name in ('ca.crt','controller.crt'):
            validity.append(_dates(_openssl(['x509','-in',str(stage/name),'-dates','-noout'],run=run)))
    return protocol,repository,{'tls_identity_sha256':observed['identity_sha256'],
        'not_before':max(value[0] for value in validity),'expires_at':min(value[1] for value in validity)}


def verify_listener_identity(config,value, *, clock=time.time):
    now=_now(clock);directory=Path(config['cert']).parent
    identity=load_identity(_read(directory,'identity.json'))
    if (digest(canonical(identity))!=value['tls_identity_sha256']
            or any(digest(_read(directory,name))!=identity['files'][name] for name in FILES)
            or not value['not_before']<=now<value['expires_at']):
        raise Conflict('controller listener trust changed or expired; explicit maintenance required')


@contextmanager
def publication_runtime(controller, *, registry,service_runtime,host,port,certfile,keyfile,allow_lan,
                        run=subprocess.run,tls_inspector=None,repository_factory=None,thread_factory=threading.Thread):
    """Optional configured listeners, no execution owner/database/scheduler added."""
    if service_runtime is None:
        yield Publication();return
    from .controller_service import configuration
    config=configuration(controller.root)
    if config.get('repository_endpoint') is None:
        yield Publication();return
    owner=controller._lifecycle_owner
    if owner is None or owner.closed:
        raise Conflict('guided publication requires the existing controller lifecycle owner')
    if (registry is None or config.get('credential_registry') is not True or config['runtime']!=str(service_runtime)
            or config.get('host','127.0.0.1')!=host or config.get('port',8443)!=port
            or config['cert']!=str(certfile) or config['key']!=str(keyfile)
            or type(allow_lan) is not bool or type(config.get('allow_lan',False)) is not bool
            or config.get('allow_lan',False)!=allow_lan):
        raise Conflict('guided publication must match the configured native controller service')
    if not ipaddress.ip_address(host).is_loopback and not allow_lan:
        raise ContractError('LAN repository/enrollment publication requires explicit allow_lan')
    application=EnrollmentService(controller,run=run,tls_inspector=tls_inspector)
    application.preflight(certfile,keyfile)
    protocol_context,repository_context,trust=_contexts(config,run=run,tls_inspector=tls_inspector)
    stopping=threading.Event()
    original=digest(canonical(config));repository_url=config['repository_endpoint']['url']
    repository_host,repository_port=endpoint(repository_url)
    from .repository_http import make_repository_server
    thread=None
    def guard(db):
        if (stopping.is_set() or owner.closed or controller._lifecycle_owner is not owner
                or db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]!=owner.epoch):
            raise Conflict('controller enrollment publication owner ended or changed epoch')
        if thread is None or not thread.is_alive():
            raise Conflict('configured repository listener is unavailable')
        current=configuration(controller.root)
        if digest(canonical(current))!=original:
            raise Conflict('controller publication configuration changed; paused maintenance/restart required')
        verify_listener_identity(current,trust)
    def available():
        if stopping.is_set() or thread is None or not thread.is_alive():raise Conflict('configured repository listener is unavailable')
        from .state_reader import StateReader
        with StateReader(controller.root).connection() as db:guard(db)
    repository=(repository_factory or make_repository_server)((repository_host,repository_port),config['repositories'],
        certfile,keyfile,Path(certfile).parent/'ca.crt',credential_registry=registry,
        tls_context=repository_context,availability=available)
    thread=thread_factory(target=repository.serve_forever,name='controller-repository',daemon=True)
    try:
        thread.start()
        application.available=available
        application.guard=guard
        def capabilities():
            available()
            return {'schema_version':1,'enrollment_available':True,'repository_url':repository_url,
                    'configuration_sha256':original,**trust}
        yield Publication(application,capabilities,protocol_context)
    finally:
        stopping.set()
        if thread.is_alive():repository.shutdown();thread.join(5)
        repository.server_close()


def require_enrollment(root, *, ready=None,clock=time.time):
    """Read-only current-owner capability; never target or attempt authorization."""
    from .controller_service import require_ready,configuration
    from .state_reader import StateReader
    (ready or require_ready)(root)
    now=clock();_now(lambda:now)
    config=configuration(root)
    with StateReader(root).connection() as db:
        db.execute('BEGIN')
        owner=db.execute('SELECT * FROM controller_job_service WHERE id=1').fetchone()
        capability=db.execute('SELECT * FROM controller_service_capabilities WHERE id=1').fetchone()
        epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
    from .process_identity import controller_boot_id
    if (owner is None or capability is None or owner['epoch']!=epoch or owner['boot']!=controller_boot_id()
            or not 0<=now-owner['heartbeat']<15 or not 0<=now-capability['heartbeat']<15
            or any(capability[key]!=owner[key] for key in ('epoch','boot','pid'))
            or capability['configuration_sha256']!=digest(canonical(config))):
        raise Conflict('guided enrollment unavailable: configure and restart the native controller repository publication')
    value=_document(capability['document'].encode())
    validate_capabilities(value)
    verify_listener_identity(config,value,clock=lambda:now)
    if (value['configuration_sha256']!=capability['configuration_sha256'] or value['enrollment_available'] is not True
            or value['repository_url']!=config.get('repository_endpoint',{}).get('url')):
        raise Conflict('guided enrollment capability differs from configured publication')
    return value


def validate_capabilities(value):
    from .contracts import sha256
    if (not isinstance(value,dict) or set(value)!={'schema_version','enrollment_available','repository_url','configuration_sha256','tls_identity_sha256','not_before','expires_at'}
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['enrollment_available'] is not True):
        raise ContractError('invalid controller publication capability')
    endpoint(value['repository_url']);sha256(value['configuration_sha256']);sha256(value['tls_identity_sha256'])
    if (type(value['not_before']) is not int or type(value['expires_at']) is not int
            or not 0<value['not_before']<value['expires_at']<=4102444800):
        raise ContractError('invalid controller listener validity bounds')
    return value
