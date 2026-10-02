"""Public controller endpoint maintenance over existing stopped application services."""
from pathlib import Path
import ssl

from .contracts import Conflict,digest,identifier
from .controller_setup import _private_path,_database_present
from .controller_endpoint import _strict_read,stage_identity,renew_expired_identity
from .controller_service import configuration
from .controller_tls import load_identity
from .enrollment import _document
from .endpoint_switch import switch_stopped,rollback_stopped,validate_switch
from .setup_contracts import SetupUnavailable


def show(root,request_id=None):
    root=_private_path(root)
    if not _database_present(root):raise SetupUnavailable('run controller setup before endpoint maintenance')
    config_raw=_strict_read(root/'private','controller-service.json');config=configuration(root)
    if config!=_document(config_raw):raise Conflict('controller endpoint configuration changed during observation')
    if request_id is not None:
        identifier(request_id);directory=_private_path(root/'private/controller-tls'/('endpoint-'+digest(request_id.encode())[:32]))
    else:
        directory=_private_path(Path(config['cert']).parent)
        if directory.parent!=root/'private/controller-tls' or Path(config['cert'])!=directory/'controller.crt' or Path(config['key'])!=directory/'controller.key':
            raise Conflict('endpoint maintenance requires the currently configured managed TLS identity')
    raw=_strict_read(directory,'identity.json');identity=load_identity(raw)
    certificate=_strict_read(directory,'controller.crt')
    if digest(certificate)!=identity['files']['controller.crt']:raise Conflict('public certificate differs from retained identity')
    if request_id is not None and identity['request_id']!=request_id:raise Conflict('staged endpoint belongs to another request')
    fingerprint=digest(ssl.PEM_cert_to_DER_cert(certificate.decode('ascii')))
    host=identity['host'];address='['+host+']' if ':' in host else host
    receipt={'schema_version':1,'record_type':'controller-endpoint-status','request_id':identity['request_id'],
        'identity_sha256':digest(raw),'certificate_sha256':fingerprint,'certificate_pem':certificate.decode('ascii'),
        'controller_url':'https://'+address+':'+str(config['port']),'configured':Path(config['cert'])==directory/'controller.crt',
        'reachability_verified':False,'targets_migrated':False,'repository_url':None}
    if config.get('repository_endpoint'):
        from urllib.parse import urlsplit
        port=urlsplit(config['repository_endpoint']['url']).port
        receipt['repository_url']='https://'+address+':'+str(port)
    if (_strict_read(root/'private','controller-service.json')!=config_raw or _strict_read(directory,'identity.json')!=raw
            or _strict_read(directory,'controller.crt')!=certificate):raise Conflict('public endpoint facts changed during observation')
    return receipt


def execute(root,action, *,request_id=None,host=None,source_sha256=None,identity_sha256=None,fingerprint=None,
            repository_url=None,unit=None,switch_sha256=None,**adapters):
    if action=='show':return show(root,request_id)
    if action in ('stage','renew'):
        return (stage_identity if action=='stage' else renew_expired_identity)(root,host,request_id,source_sha256,**adapters)
    if action=='apply':
        return switch_stopped(root,request_id,identity_sha256,fingerprint,unit=unit,repository_url=repository_url,**adapters)
    if action=='rollback':return rollback_stopped(root,request_id,switch_sha256,**adapters)
    from .contracts import ContractError
    raise ContractError('choose show, stage, renew, apply or rollback')


def wizard(root, *,unit,input_stream=None,output_stream=None,run=None,runner=None):
    """Attended address/SAN maintenance; the existing native unit stays stopped."""
    import sys
    from .enrollment_console import _answer
    from .contracts import ContractError,sha256
    source=input_stream or sys.stdin;output=output_stream or sys.stdout
    original=show(root)
    print('Finish or reconcile active work, then stop the existing controller user service before staging.',file=output)
    print('Current controller: '+original['controller_url'],file=output)
    print('Current identity SHA-256: '+original['identity_sha256'],file=output)
    request_id=_answer(source,output,'Endpoint request ID (reuse after interruption; empty cancels): ',128)
    if not request_id:return None
    identifier(request_id)
    directory=root/'private/controller-tls'/('endpoint-'+digest(request_id.encode())[:32])
    switch_path=directory/'switch-intent.json'
    adapters={}
    if run is not None:adapters['run']=run
    retained=None
    if switch_path.exists() or switch_path.is_symlink():
        # Public choices guide replay; the stopped backend independently proves
        # their original source, immutable material and current configuration.
        retained=validate_switch(_document(_strict_read(directory,switch_path.name)))
        if retained['request_id']!=request_id:raise Conflict('retained switch belongs to another request')
        selected=show(root,request_id)
        if selected['identity_sha256']!=retained['destination_identity_sha256'] or selected['certificate_sha256']!=retained['approved_certificate_sha256']:
            raise Conflict('retained switch differs from staged identity')
        if str(unit)!=retained['unit']:raise Conflict('resume with the exact original native unit')
        staged=selected
        print('Resuming the retained switch from identity SHA-256: '+retained['source_identity_sha256'],file=output)
        selected['repository_url']=retained['destination_configuration'].get('repository_endpoint',{}).get('url')
    else:
        mode=_answer(source,output,'Address change or expired-leaf renewal: change, renew (empty cancels): ',16)
        if not mode:return None
        if mode not in ('change','renew'):raise ContractError('choose change or renew')
        host=_answer(source,output,'Successor literal controller IP (consider a stable DHCP reservation): ',64)
        if not host:return None
        confirmed=_answer(source,output,'Type the current identity SHA-256 to retain its CA (empty cancels): ',64)
        if not confirmed:return None
        sha256(confirmed)
        if confirmed!=original['identity_sha256']:raise Conflict('confirm the exact original controller identity')
        staged=execute(root,'stage' if mode=='change' else 'renew',request_id=request_id,host=host,source_sha256=confirmed,**adapters)
        selected=show(root,request_id)
    print('Staged identity SHA-256: '+selected['identity_sha256'],file=output)
    print('Controller: '+selected['controller_url'],file=output)
    print('Controller certificate SHA-256: '+selected['certificate_sha256'],file=output)
    print('The existing CA is retained. Compare this full fingerprint on each target recovery screen.',file=output)
    approved=_answer(source,output,'Type the staged full certificate fingerprint to apply (empty leaves it staged): ',64)
    if not approved:return staged
    sha256(approved)
    if approved!=selected['certificate_sha256']:raise Conflict('approve the exact staged controller fingerprint')
    repository_url=None
    if selected['repository_url'] is not None:
        print('Proposed repository service: '+selected['repository_url'],file=output)
        repository_url=_answer(source,output,'Type the successor repository service URL to confirm (empty leaves staged): ',4096)
        if not repository_url:return staged
        if repository_url!=selected['repository_url']:raise Conflict('confirm the exact successor repository service endpoint')
    if runner is not None:adapters['runner']=runner
    result=execute(root,'apply',request_id=request_id,identity_sha256=selected['identity_sha256'],fingerprint=approved,
        unit=unit,repository_url=repository_url,**adapters)
    print('Controller configuration applied. Start its existing user service, then apply this address and fingerprint on each target.',file=output)
    print('Targets keep their evidence and credentials. Reachability and boot approval remain separate.',file=output,flush=True)
    return result
