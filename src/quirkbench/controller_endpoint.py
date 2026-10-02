"""Private retained-CA endpoint certificate staging; no service/trust activation."""
from pathlib import Path
import os
import subprocess
import tempfile
import time

from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _private_path,_durable_directory,_database_present
from .controller_tls import (FILES,_read,_openssl,validate_identity,load_identity,
    inspect_identity,_lineage,_generate_material,_inspect_material)
from .enrollment import _document,_now
from .maintenance import private_lock
from .release_http import _remaining
from .setup_contracts import SetupUnavailable
from .store import atomic_write


def validate_intent(value):
    fields={'schema_version','record_type','request_id','host','previous_directory','previous_identity_sha256',
        'configuration_sha256','created_at','days'}
    version=value.get('schema_version') if isinstance(value,dict) else None
    if version==3:fields.add('source_mode')
    if (not isinstance(value,dict) or set(value)!=fields or type(version) is not int
            or version not in (2,3) or value['record_type']!='controller-tls-intent'):
        raise ContractError('invalid endpoint TLS intent')
    if version==3 and value['source_mode']!='expired-leaf-renewal':raise ContractError('invalid explicit TLS renewal source mode')
    validate_identity({key:value[key] for key in ('request_id','host','previous_directory','previous_identity_sha256')}
        |{'schema_version':2,'record_type':'controller-tls-identity','files':{name:'0'*64 for name in FILES}})
    sha256(value['configuration_sha256'])
    if type(value['days']) is not int or not 1<=value['days']<=365 or type(value['created_at']) is not int:
        raise ContractError('invalid bounded endpoint certificate lifetime')
    _now(lambda:value['created_at']);return value


def _strict_read(directory,name):
    info=(directory/name).lstat()
    if info.st_nlink!=1:raise ContractError('endpoint TLS inputs must remain single-link')
    return _read(directory,name)


def _dates(captured, *,run,temporary_parent):
    from .enrollment_certificate import _expiry
    with tempfile.TemporaryDirectory(prefix='.endpoint-validity-',dir=temporary_parent) as temporary:
        stage=Path(temporary)
        for name in ('ca.crt','controller.crt'):atomic_write(stage/name,captured[name])
        return tuple(_expiry(_openssl(['x509','-in',str(stage/name),'-enddate','-noout'],run=run))
            for name in ('ca.crt','controller.crt'))


def stage_identity(root,host,request_id,expected_identity_sha256, *,run=subprocess.run,
                   fault_hook=None,clock=time.time,monotonic=time.monotonic):
    """Explicit idle-controller preparation, retaining exact current managed CA.

    This does not stop/start a service, replace configuration or approve target
    trust. Use the separate explicit renewal adapter for an expired source identity.
    """
    return _stage(root,host,request_id,expected_identity_sha256,run=run,fault_hook=fault_hook,
        clock=clock,monotonic=monotonic,expired_source=False)


def renew_expired_identity(root,host,request_id,expected_identity_sha256, *,run=subprocess.run,
                           fault_hook=None,clock=time.time,monotonic=time.monotonic):
    """Explicit stopped renewal; exact historical leaf, still-valid retained CA.

    Historical source validation never applies to transport, inspection/readiness,
    the retained CA or destination. No configuration or target switch is implied.
    """
    return _stage(root,host,request_id,expected_identity_sha256,run=run,fault_hook=fault_hook,
        clock=clock,monotonic=monotonic,expired_source=True)


def _stage(root,host,request_id,expected_identity_sha256, *,run,fault_hook,clock,monotonic,expired_source):
    identifier(request_id);sha256(expected_identity_sha256)
    validate_identity({'schema_version':1,'record_type':'controller-tls-identity','request_id':request_id,
        'host':host,'files':{name:'0'*64 for name in FILES}})
    root=_private_path(root);deadline=monotonic()+180;fault=fault_hook or (lambda _:None)
    if not _database_present(root):raise SetupUnavailable('controller setup required before endpoint maintenance')
    from .controller_install import _idle
    from .controller_service import configuration
    with private_lock(root/'command.lock') as command_fd,private_lock(root/'coordinator.lock') as owner_fd:
        _remaining(deadline,monotonic);_idle(root);config=configuration(root)
        config_raw=_strict_read(root/'private','controller-service.json')
        previous=_private_path(Path(config['cert']).parent)
        if (previous.parent!=root/'private/controller-tls' or Path(config['cert'])!=previous/'controller.crt'
                or Path(config['key'])!=previous/'controller.key'):
            raise Conflict('endpoint maintenance requires the currently configured managed TLS identity')
        previous_raw=_strict_read(previous,'identity.json');old=load_identity(previous_raw)
        if digest(previous_raw)!=expected_identity_sha256:raise Conflict('confirm the exact currently configured TLS identity')
        captured={name:_strict_read(previous,name) for name in FILES}
        if any(digest(raw)!=old['files'][name] for name,raw in captured.items()):raise Conflict('current managed TLS bytes changed')
        _lineage(previous,old)
        def guard():
            _remaining(deadline,monotonic);_idle(root)
            for path,fd in ((root/'command.lock',command_fd),(root/'coordinator.lock',owner_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('endpoint maintenance ownership changed')
            if (_strict_read(root/'private','controller-service.json')!=config_raw
                    or _strict_read(previous,'identity.json')!=previous_raw
                    or any(_strict_read(previous,name)!=raw for name,raw in captured.items())):
                raise Conflict('current endpoint configuration or original trust changed')
            _lineage(previous,old);_remaining(deadline,monotonic)
        directory=_private_path(root/'private/controller-tls'/('endpoint-'+digest(request_id.encode())[:32]))
        guard();_durable_directory(directory)
        intent_path=directory/'intent.json'
        fields={'host':host,'request_id':request_id,'previous_directory':previous.name,
            'previous_identity_sha256':expected_identity_sha256,'configuration_sha256':digest(config_raw)}
        if expired_source:fields['source_mode']='expired-leaf-renewal'
        if intent_path.exists() or intent_path.is_symlink():
            retained=validate_intent(_document(_strict_read(directory,'intent.json')))
            if retained['schema_version']!=(3 if expired_source else 2) or any(retained[key]!=value for key,value in fields.items()):
                raise Conflict('endpoint request already has another immutable source/choice')
        if expired_source:
            _inspect_material(old,captured,run=run,temporary_parent=directory,historical_source=True)
            source={'identity_sha256':digest(previous_raw)}
        else:source=inspect_identity(previous,run=run,temporary_parent=directory)
        guard();ca_expiry,source_expiry=_dates(captured,run=run,temporary_parent=directory);guard()
        if expired_source and _now(clock)<source_expiry:raise Conflict('source is still valid; use ordinary endpoint staging')
        if intent_path.exists() or intent_path.is_symlink():
            intent=validate_intent(_document(_strict_read(directory,'intent.json')))
            if intent['schema_version']!=(3 if expired_source else 2) or any(intent[key]!=value for key,value in fields.items()):raise Conflict('endpoint request already has another immutable source/choice')
        else:
            if any(directory.iterdir()):raise Conflict('unidentified endpoint TLS staging; no key replacement permitted')
            now=_now(clock);days=min(365,(ca_expiry-now)//86400-1)
            if days<1:raise Conflict('retained CA lifetime insufficient; explicit CA maintenance required')
            intent=validate_intent({'schema_version':3 if expired_source else 2,'record_type':'controller-tls-intent',**fields,'created_at':now,'days':days})
            guard();atomic_write(intent_path,canonical(intent));fault('endpoint_intent_retained');guard()
        def source_fresh(now):
            if not now<ca_expiry or (now<source_expiry if expired_source else now>=source_expiry):
                raise Conflict('endpoint maintenance source expired or retained CA validity changed')
        def native_guard():
            guard()
            if _strict_read(directory,'intent.json')!=canonical(intent):raise Conflict('endpoint TLS intent changed during native work')
            now=_now(clock);source_fresh(now)
            if not intent['created_at']<=now:raise Conflict('endpoint maintenance clock moved backwards')
        native_guard()
        if (directory/'identity.json').exists() or (directory/'identity.json').is_symlink():
            value=load_identity(_strict_read(directory,'identity.json'));new={name:_strict_read(directory,name) for name in FILES}
        else:
            new=_generate_material(directory,host,run=run,fault_hook=fault,guard=native_guard,
                retained_ca={name:captured[name] for name in ('ca.key','ca.crt')},days=intent['days'])
            value=validate_identity({'schema_version':2,'record_type':'controller-tls-identity',
                'request_id':request_id,'host':host,'previous_directory':previous.name,
                'previous_identity_sha256':expected_identity_sha256,'files':{name:digest(raw) for name,raw in new.items()}})
        if (value['schema_version']!=2 or any(value[key]!=choice for key,choice in fields.items() if key not in ('configuration_sha256','source_mode'))
                or any(digest(raw)!=value['files'][name] for name,raw in new.items())
                or any(new[name]!=captured[name] for name in ('ca.key','ca.crt'))):
            raise Conflict('endpoint successor differs from immutable source/intent')
        native_guard();_lineage(directory,value);fingerprint=_inspect_material(value,new,run=run,temporary_parent=directory)
        fresh_ca,expiry=_dates(new,run=run,temporary_parent=directory);native_guard()
        if fresh_ca!=ca_expiry or expiry>ca_expiry:raise Conflict('endpoint certificate outlasts retained CA')
        # No native work after this exact old/new fence and final freshness check.
        if (_strict_read(directory,'intent.json')!=canonical(intent)
                or any(_strict_read(directory,name)!=raw for name,raw in new.items())):
            raise Conflict('endpoint successor bytes or durable intent changed after native validation')
        now=_now(clock);source_fresh(now)
        if not intent['created_at']<=now<expiry:raise Conflict('endpoint successor expired or clock moved backwards')
        _remaining(deadline,monotonic)
        if (directory/'identity.json').exists():
            if _strict_read(directory,'identity.json')!=canonical(value):raise Conflict('endpoint successor identity changed')
        else:atomic_write(directory/'identity.json',canonical(value))
        fault('endpoint_identity_retained')
        guard()
        if (_strict_read(directory,'intent.json')!=canonical(intent)
                or any(_strict_read(directory,name)!=raw for name,raw in new.items())
                or _strict_read(directory,'identity.json')!=canonical(value)):
            raise Conflict('endpoint successor changed before receipt')
        now=_now(clock);source_fresh(now)
        if not intent['created_at']<=now<expiry:raise Conflict('endpoint successor expired before receipt')
        _remaining(deadline,monotonic)
        return {'request_id':request_id,'directory':str(directory),'identity_sha256':digest(canonical(value)),
            'certificate_sha256':fingerprint,'previous_identity_sha256':source['identity_sha256'],
            'host':host,'expires_at':expiry,'ca_retained':True,'expired_source_renewal':expired_source,'activated':False,'targets_migrated':False}
