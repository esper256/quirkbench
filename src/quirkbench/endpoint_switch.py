"""Exact stopped controller configuration publication; existing owner/config only."""
from pathlib import Path
import ipaddress
import os
import stat
import subprocess
import time

from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .controller_endpoint import _strict_read, _dates, validate_intent
from .controller_setup import _managed_path, _database_present
from .controller_tls import FILES, load_identity, inspect_identity, _lineage
from .enrollment import _document, _now
from .enrollment_client import endpoint
from .maintenance import private_lock
from .release_http import _remaining
from .state_reader import read_file
from .store import atomic_write


def validate_switch(value):
    fields={'schema_version','record_type','request_id','source_configuration',
        'destination_configuration','tls_intent_sha256','source_identity_sha256',
        'destination_identity_sha256','approved_certificate_sha256','unit'}
    if isinstance(value,dict) and value.get('schema_version')==2:fields.remove('unit')
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version'] not in (1,2) or value['record_type']!='controller-endpoint-switch'):
        raise ContractError('invalid controller endpoint switch')
    identifier(value['request_id'])
    for name in ('tls_intent_sha256','source_identity_sha256','destination_identity_sha256','approved_certificate_sha256'):
        sha256(value[name])
    for name in ('source_configuration','destination_configuration'):
        if not isinstance(value[name],dict):raise ContractError('endpoint switch requires exact service configurations')
    if value['schema_version']==1:
        unit=value['unit']
        if not isinstance(unit,str) or len(unit)>4096 or str(Path(unit))!=unit or not Path(unit).is_absolute() or '..' in Path(unit).parts:
            raise ContractError('endpoint switch requires a normalized absolute native unit path')
    if len(canonical(value))>65536:raise ContractError('endpoint switch exceeds byte limit')
    return value


def switch_stopped(root, request_id, expected_identity_sha256, approved_certificate_sha256, *,
                   unit=None, repository_url=None, run=subprocess.run, runner=subprocess.run,
                   fault_hook=None, clock=time.time, monotonic=time.monotonic):
    """Publish only the exact approved staged endpoint while native ownership is stopped.

    No service action, target migration, reachability claim or new approval is
    implied. Existing repository publication requires an explicit successor URL.
    """
    return _switch(root,request_id,expected_identity_sha256,approved_certificate_sha256,
        unit=unit,repository_url=repository_url,run=run,runner=runner,fault_hook=fault_hook,
        clock=clock,monotonic=monotonic,rollback=False)


def rollback_stopped(root, request_id, expected_switch_sha256, *, run=subprocess.run,
                     runner=subprocess.run, fault_hook=None, clock=time.time, monotonic=time.monotonic):
    """Restore a retained exact configuration; never overwrite unrelated maintenance."""
    sha256(expected_switch_sha256)
    return _switch(root,request_id,None,None,unit=None,repository_url=None,run=run,runner=runner,
        fault_hook=fault_hook,clock=clock,monotonic=monotonic,rollback=True,
        expected_switch_sha256=expected_switch_sha256)


def _switch(root,request_id,expected_identity,approved_pin, *,unit,repository_url,run,runner,
            fault_hook,clock,monotonic,rollback,expected_switch_sha256=None):
    from .controller_install import _idle
    from .controller_service import configuration, validate_configuration
    identifier(request_id)
    if not rollback:sha256(expected_identity);sha256(approved_pin)
    root=_managed_path(root)
    if not _database_present(root):raise Conflict('endpoint switch requires configured controller state')
    directory=_managed_path(root/'private/controller-tls'/('endpoint-'+digest(request_id.encode())[:32]))
    deadline=monotonic()+180;fault=fault_hook or (lambda _:None)
    with private_lock(root/'command.lock') as command_fd,private_lock(root/'coordinator.lock') as owner_fd:
        _remaining(deadline,monotonic);_idle(root)
        current=configuration(root);current_raw=_strict_read(root/'private','controller-service.json')
        if current_raw!=canonical(current):raise ContractError('endpoint maintenance requires canonical service configuration')
        intent_raw=_strict_read(directory,'intent.json');intent=validate_intent(_document(intent_raw))
        identity_raw=_strict_read(directory,'identity.json');identity=load_identity(identity_raw)
        if identity['schema_version']!=2 or intent['request_id']!=request_id or any(identity[name]!=intent[name]
                for name in ('request_id','host','previous_directory','previous_identity_sha256')):
            raise Conflict('endpoint switch requires the exact completed successor stage')
        previous=_managed_path(directory.parent/identity['previous_directory'])
        source_raw=_strict_read(previous,'identity.json');source=load_identity(source_raw)
        material={path:{name:_strict_read(path,name) for name in FILES} for path in (previous,directory)}
        if (digest(source_raw)!=identity['previous_identity_sha256']
                or any(digest(raw)!=record['files'][name] for path,record in ((previous,source),(directory,identity)) for name,raw in material[path].items())
                or any(material[previous][name]!=material[directory][name] for name in ('ca.key','ca.crt'))):
            raise Conflict('endpoint switch source, successor or retained CA changed')
        _lineage(directory,identity)
        journal=directory/'switch-intent.json'
        if journal.exists() or journal.is_symlink():
            saved_raw=_strict_read(directory,journal.name);saved=validate_switch(_document(saved_raw))
            if saved_raw!=canonical(saved):raise ContractError('endpoint switch must be canonical')
        else:
            if rollback:raise Conflict('no retained endpoint switch to roll back')
            old=dict(current)
            if (old['cert']!=str(previous/'controller.crt') or old['key']!=str(previous/'controller.key')
                    or digest(current_raw)!=intent['configuration_sha256']):
                raise Conflict('endpoint stage belongs to another currently configured source')
            new=old|{'host':identity['host'],'cert':str(directory/'controller.crt'),'key':str(directory/'controller.key')}
            if 'repository_endpoint' in old:
                host,port=endpoint(repository_url)
                if host!=identity['host'] or port==old.get('port',8443):raise ContractError('explicit repository URL must match successor SAN and use a separate port')
                new['repository_endpoint']={'url':repository_url}
            elif repository_url is not None:raise Conflict('endpoint maintenance cannot introduce repository publication')
            saved=validate_switch({'schema_version':2,'record_type':'controller-endpoint-switch',
                'request_id':request_id,'source_configuration':old,'destination_configuration':new,
                'tls_intent_sha256':digest(intent_raw),'source_identity_sha256':digest(source_raw),
                'destination_identity_sha256':digest(identity_raw),'approved_certificate_sha256':approved_pin})
            saved_raw=canonical(saved)
        old,new=saved['source_configuration'],saved['destination_configuration']
        # Recompute every permitted configuration delta rather than trusting a journal.
        expected_new=old|{'host':identity['host'],'cert':str(directory/'controller.crt'),'key':str(directory/'controller.key')}
        if 'repository_endpoint' in old:
            repo=new.get('repository_endpoint')
            if not isinstance(repo,dict) or set(repo)!={'url'}:raise ContractError('invalid successor repository endpoint')
            host,port=endpoint(repo['url'])
            if host!=identity['host'] or port==old.get('port',8443):raise Conflict('successor repository SAN/port changed')
            expected_new['repository_endpoint']=repo
        if (saved['request_id']!=request_id or saved['tls_intent_sha256']!=digest(intent_raw)
                or saved['source_identity_sha256']!=digest(source_raw) or saved['destination_identity_sha256']!=digest(identity_raw)
                or digest(canonical(old))!=intent['configuration_sha256'] or old['cert']!=str(previous/'controller.crt')
                or old['key']!=str(previous/'controller.key') or new!=expected_new):
            raise Conflict('endpoint switch differs from exact staged source or permitted delta')
        for config in (old,new):validate_configuration(root,config)
        if not ipaddress.ip_address(new['host']).is_loopback and new.get('allow_lan') is not True:
            raise Conflict('successor LAN endpoint requires existing explicit LAN publication authorization')
        if rollback:
            if digest(saved_raw)!=expected_switch_sha256:raise Conflict('confirm the exact endpoint switch before rollback')
        elif (saved['destination_identity_sha256']!=expected_identity or saved['approved_certificate_sha256']!=approved_pin
                or (saved['schema_version']==1 and unit is not None and saved['unit']!=str(unit)) or (new.get('repository_endpoint',{}).get('url')!=repository_url)):
            raise Conflict('endpoint switch retry requires the original exact operator choices')
        old_raw,new_raw=canonical(old),canonical(new)
        if current_raw not in (old_raw,new_raw):raise Conflict('another configuration superseded this endpoint switch')
        journal_required=journal.exists()
        rolled=directory/'rollback-intent.json';receipt=directory/'switch-completion.json'
        retained_rollback=_strict_read(directory,rolled.name) if rolled.exists() or rolled.is_symlink() else None
        retained_receipt=_strict_read(directory,receipt.name) if receipt.exists() or receipt.is_symlink() else None
        def exact():
            _remaining(deadline,monotonic);_idle(root)
            for path,fd in ((root/'command.lock',command_fd),(root/'coordinator.lock',owner_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('endpoint switch ownership changed')
            if (_strict_read(root/'private','controller-service.json')!=current_raw
                    or _strict_read(previous,'identity.json')!=source_raw or _strict_read(directory,'identity.json')!=identity_raw
                    or _strict_read(directory,'intent.json')!=intent_raw
                    or any(_strict_read(path,name)!=raw for path,files in material.items() for name,raw in files.items())):
                raise Conflict('endpoint switch inputs changed after validation')
            if journal_required:
                if _strict_read(directory,journal.name)!=saved_raw:raise Conflict('endpoint switch intent changed')
            elif journal.exists() or journal.is_symlink():raise Conflict('endpoint switch intent appeared during validation')
            for path,expected in ((rolled,retained_rollback),(receipt,retained_receipt)):
                if expected is None:
                    if path.exists() or path.is_symlink():raise Conflict('endpoint maintenance receipt appeared during validation')
                elif _strict_read(directory,path.name)!=expected:raise Conflict('endpoint maintenance receipt changed')
            _lineage(directory,identity);_remaining(deadline,monotonic)
        def stopped():
            # Both command and lifecycle ownership locks remain held.
            exact()
        exact();stopped()
        rollback_raw=canonical({'schema_version':1,'record_type':'controller-endpoint-rollback','switch_sha256':digest(saved_raw)})
        if rolled.exists() or rolled.is_symlink():
            if _strict_read(directory,rolled.name)!=rollback_raw:raise Conflict('endpoint rollback intent changed')
            if not rollback:raise Conflict('endpoint switch was selected for rollback; use its exact stopped rollback')
        if receipt.exists() or receipt.is_symlink():
            completed=_strict_read(directory,receipt.name)
            choices={canonical({'schema_version':1,'record_type':'controller-endpoint-completion','switch_sha256':digest(saved_raw),'state':s}) for s in ('COMMITTED','ROLLED_BACK')}
            if completed not in choices:raise Conflict('endpoint completion changed')
            state=_document(completed)['state']
            if state=='ROLLED_BACK' and not rollback:raise Conflict('endpoint switch already rolled back; stage another explicit request')
            if current_raw!=(old_raw if state=='ROLLED_BACK' else new_raw) and not (rollback and retained_rollback==rollback_raw and current_raw==old_raw and state=='COMMITTED'):
                raise Conflict('endpoint completion disagrees with configured state')
        if not rollback:
            observed=inspect_identity(directory,run=run,temporary_parent=directory);exact()
            if observed['certificate_sha256']!=approved_pin:raise Conflict('approve the exact successor certificate fingerprint')
            ca_expiry,leaf_expiry=_dates(material[directory],run=run,temporary_parent=directory);exact()
            if not _now(clock)<min(ca_expiry,leaf_expiry):raise Conflict('successor identity expired before switch')
        # Rollback is restoration of captured bytes, including an expired old leaf.
        # It never reports readiness or supplies transport validation exceptions.
        stopped();exact()
        if not journal_required:
            atomic_write(journal,saved_raw);journal_required=True;fault('endpoint_switch_intent');exact()
        if rollback and retained_rollback is None:
            atomic_write(rolled,rollback_raw);retained_rollback=rollback_raw;fault('endpoint_rollback_intent');exact()
        def publishable():
            stopped();exact()
            if not rollback and not _now(clock)<min(ca_expiry,leaf_expiry):raise Conflict('successor expired before publication')
            _remaining(deadline,monotonic)
        target=old_raw if rollback else new_raw
        publishable()
        atomic_write(root/'private/controller-service.json',target);current_raw=target
        fault('endpoint_configuration_restored' if rollback else 'endpoint_configuration_switched');publishable()
        if rollback and _strict_read(directory,rolled.name)!=rollback_raw:raise Conflict('endpoint rollback intent changed before completion')
        done=canonical({'schema_version':1,'record_type':'controller-endpoint-completion','switch_sha256':digest(saved_raw),'state':'ROLLED_BACK' if rollback else 'COMMITTED'})
        atomic_write(receipt,done);retained_receipt=done;fault('endpoint_switch_completed');publishable()
        if _strict_read(directory,receipt.name)!=done:raise Conflict('endpoint completion changed before receipt')
        return {'request_id':request_id,'switch_sha256':digest(saved_raw),'configuration_sha256':digest(target),
            'configured':True,'rolled_back':rollback,'service_started':False,'reachability_verified':False,'targets_migrated':False}
