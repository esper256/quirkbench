"""Stopped endpoint publication has one existing owner and exact rollback."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quirkbench import endpoint_switch as switch
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.controller_service import UNIT,configuration,configuration_document
from quirkbench.maintenance import private_lock
from quirkbench.store import atomic_write
from test_controller_endpoint import configured,stage,expired_source,renew


class Manager:
    def __init__(self,root,unit,runtime):
        self.root=root;self.unit=unit;self.runtime=runtime;self.active=False;self.overrides=False;self.calls=[]
    def __call__(self,argv,**kw):
        assert argv[:4]==['systemctl','--user','show',UNIT]
        self.calls.append(argv)
        if '--property=ActiveState' in argv:
            output='ActiveState=active\nMainPID=999\n' if self.active else 'ActiveState=inactive\nMainPID=0\n'
        else:
            executable=str(self.runtime/'bin/quirkbench-controller-service')
            fields={'LoadState':'loaded','FragmentPath':str(self.unit),'DropInPaths':'override.conf' if self.overrides else '',
                'ExecStart':'{ path='+executable+' ; argv[]='+executable+' --state '+str(self.root)+' ; ignore_errors=no ; }',
                'ExecStartPre':'','ExecStartPost':'','ExecCondition':'','KillMode':'control-group'}
            output=''.join(name+'='+value+'\n' for name,value in fields.items())
        return SimpleNamespace(returncode=0,stdout=output)


@pytest.fixture
def prepared(configured):
    root,source,native=configured;answer=stage(configured)
    unit=root.parent/'config/systemd/user'/UNIT;unit.parent.mkdir(parents=True);unit.write_text('[Service]\nKillMode=control-group\n')
    runtime=Path(configuration(root)['runtime']).parent.parent
    return configured,answer,unit,Manager(root,unit,runtime)


def activate(prepared,**kw):
    (root,source,native),answer,unit,manager=prepared
    return switch.switch_stopped(root,answer['request_id'],answer['identity_sha256'],answer['certificate_sha256'],
        unit=unit,run=kw.pop('run',native),runner=kw.pop('runner',manager),**kw)


def restore(prepared,switched,**kw):
    (root,source,native),answer,unit,manager=prepared
    return switch.rollback_stopped(root,answer['request_id'],switched['switch_sha256'],run=native,runner=manager,**kw)


def test_exact_successor_switch_and_rollback_preserve_ca_auth_workers_and_source(prepared):
    (root,source,native),answer,unit,manager=prepared
    before=configuration(root);old_material={p:p.read_bytes() for p in Path(source['directory']).iterdir() if p.is_file()}
    result=activate(prepared);after=configuration(root)
    assert after==before|{'host':answer['host'],'cert':answer['directory']+'/controller.crt','key':answer['directory']+'/controller.key','tls_identity':{'kind':'endpoint','request_id':'endpoint-1'}}
    assert result['configured'] and not any(result[name] for name in ('service_started','reachability_verified','targets_migrated','rolled_back'))
    assert activate(prepared)==result
    rolled=restore(prepared,result);assert rolled['rolled_back'] and configuration(root)==before
    assert restore(prepared,result)==rolled
    assert all(p.read_bytes()==raw for p,raw in old_material.items())
    with pytest.raises(Conflict,match='rollback|rolled back'):activate(prepared)
    assert all(call[2]=='show' for call in manager.calls)


@pytest.mark.parametrize('phase',['endpoint_switch_intent','endpoint_configuration_switched','endpoint_switch_completed'])
def test_switch_crash_retry_uses_same_intent_config_and_keys(prepared,phase):
    directory=Path(prepared[1]['directory']);before={name:(directory/name).read_bytes() for name in ('controller.key','ca.key')}
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):activate(prepared,fault_hook=fail)
    intent=(directory/'switch-intent.json').read_bytes();result=activate(prepared)
    assert result['switch_sha256']==digest(intent) and (directory/'switch-intent.json').read_bytes()==intent
    assert all((directory/name).read_bytes()==raw for name,raw in before.items())


@pytest.mark.parametrize('phase',['endpoint_rollback_intent','endpoint_configuration_restored','endpoint_switch_completed'])
def test_rollback_crash_retry_retains_original_config_and_disables_switch_resume(prepared,phase):
    root=prepared[0][0];old=configuration(root);result=activate(prepared)
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):restore(prepared,result,fault_hook=fail)
    with pytest.raises(Conflict):activate(prepared)
    assert restore(prepared,result)['rolled_back'] and configuration(root)==old


@pytest.mark.parametrize('lock',['command.lock','coordinator.lock'])
def test_existing_owner_blocks_switch_before_durable_intent(prepared,lock):
    with private_lock(prepared[0][0]/lock):
        with pytest.raises(Conflict):activate(prepared)
    assert not Path(prepared[1]['directory'],'switch-intent.json').exists()


@pytest.mark.parametrize('change',['config','source-key','destination-key','command-inode','intent','completion'])
def test_native_callback_mutations_fail_before_configuration_publication(prepared,change):
    (root,source,native),answer,unit,manager=prepared;directory=Path(answer['directory']);before=configuration(root);done=[False]
    def changed(argv,**kw):
        result=native(argv,**kw)
        if not done[0]:
            if change=='config':atomic_write(root/'private/controller-service.json',canonical(configuration_document(before|{'port':9443})))
            elif change=='source-key':atomic_write(Path(source['directory'])/'ca.key',b'changed')
            elif change=='destination-key':atomic_write(directory/'controller.key',b'changed')
            elif change=='command-inode':(root/'command.lock').rename(root/'lost.lock');atomic_write(root/'command.lock',b'')
            elif change=='intent':atomic_write(directory/'switch-intent.json',b'{}')
            else:atomic_write(directory/'switch-completion.json',b'{}')
            done[0]=True
        return result
    with pytest.raises((Conflict,ContractError,OSError)):activate(prepared,run=changed)
    assert done[0] and configuration(root)['cert']==before['cert']


def test_full_pin_changed_retry_and_superseded_config_never_overwrite(prepared):
    (root,source,native),answer,unit,manager=prepared;before=configuration(root)
    with pytest.raises(Conflict,match='fingerprint'):switch.switch_stopped(root,answer['request_id'],answer['identity_sha256'],'f'*64,unit=unit,run=native,runner=manager)
    assert configuration(root)==before and not Path(answer['directory'],'switch-intent.json').exists()
    result=activate(prepared)
    with pytest.raises(Conflict):switch.rollback_stopped(root,answer['request_id'],'e'*64,runner=manager)
    replacement=configuration(root)|{'port':9443};atomic_write(root/'private/controller-service.json',canonical(configuration_document(replacement)))
    with pytest.raises(Conflict,match='superseded'):restore(prepared,result)
    assert configuration(root)==replacement


@pytest.mark.parametrize('phase',['endpoint_switch_intent','endpoint_configuration_switched','endpoint_switch_completed'])
def test_intent_deletion_after_publication_blocks_receipt(prepared,phase):
    directory=Path(prepared[1]['directory'])
    def changed(actual):
        if actual==phase:(directory/'switch-intent.json').unlink()
    with pytest.raises(OSError):activate(prepared,fault_hook=changed)


def test_explicit_repository_url_is_required_and_bound_to_successor_san(configured):
    root,source,native=configured;value=configuration(root)|{'repository_endpoint':{'url':'https://127.0.0.1:8444'}}
    atomic_write(root/'private/controller-service.json',canonical(configuration_document(value)));answer=stage(configured)
    unit=root.parent/UNIT;unit.write_text('unit');manager=Manager(root,unit,Path(value['runtime']).parent.parent)
    prepared=(configured,answer,unit,manager)
    for url in (None,'https://127.0.0.3:8444','https://127.0.0.2:8443'):
        with pytest.raises(ContractError):activate(prepared,repository_url=url)
    result=activate(prepared,repository_url='https://127.0.0.2:8444')
    assert configuration(root)['repository_endpoint']=={'url':'https://127.0.0.2:8444'}
    with pytest.raises(Conflict):activate(prepared,repository_url='https://127.0.0.2:9444')
    assert restore(prepared,result)['rolled_back'] and configuration(root)==value


def test_rollback_of_expired_leaf_restores_bytes_without_claiming_readiness(configured):
    configured=expired_source(configured);root,source,native=configured;old=configuration(root);answer=renew(configured)
    unit=root.parent/UNIT;unit.write_text('unit');manager=Manager(root,unit,Path(old['runtime']).parent.parent)
    prepared=(configured,answer,unit,manager);result=activate(prepared);assert restore(prepared,result)['rolled_back']
    assert configuration(root)==old
    from quirkbench.controller_tls import inspect_identity
    with pytest.raises(AssertionError):inspect_identity(Path(source['directory']),run=native)


def test_switch_publication_deadline_and_expiry_are_checked_after_native_work(prepared):
    from cryptography import x509
    now=[0];wall=[int(__import__('time').time())]
    expiry=int(x509.load_pem_x509_certificate(Path(prepared[1]['directory'],'controller.crt').read_bytes()).not_valid_after_utc.timestamp())
    native=prepared[0][2]
    def elapsed(argv,**kw):
        result=native(argv,**kw);now[0]=181;return result
    with pytest.raises(Conflict,match='deadline'):activate(prepared,run=elapsed,monotonic=lambda:now[0])
    now[0]=0
    def expired(argv,**kw):
        result=native(argv,**kw);wall[0]=expiry;return result
    with pytest.raises(Conflict,match='expired'):activate(prepared,run=expired,clock=lambda:wall[0])
    assert not Path(prepared[1]['directory'],'switch-intent.json').exists()


@pytest.mark.parametrize('phase',['endpoint_switch_intent','endpoint_configuration_switched','endpoint_switch_completed'])
@pytest.mark.parametrize('change',['coordinator.lock','command.lock','expiry'])
def test_durable_switch_boundary_rechecks_native_owner_and_expiry(prepared,phase,change):
    from cryptography import x509
    now=[int(__import__('time').time())]
    expiry=int(x509.load_pem_x509_certificate(Path(prepared[1]['directory'],'controller.crt').read_bytes()).not_valid_after_utc.timestamp())
    def altered(actual):
        if actual==phase:
            if change=='expiry':now[0]=expiry
            else:
                path=prepared[0][0]/change;path.rename(path.with_suffix('.old'));atomic_write(path,b'')
    with pytest.raises(Conflict):activate(prepared,fault_hook=altered,clock=lambda:now[0])
    if phase=='endpoint_switch_intent':assert configuration(prepared[0][0])['host']=='127.0.0.1'


@pytest.mark.parametrize('phase',['endpoint_rollback_intent','endpoint_configuration_restored','endpoint_switch_completed'])
def test_durable_rollback_boundary_rechecks_lifecycle_lock_before_receipt(prepared,phase):
    result=activate(prepared)
    def altered(actual):
        if actual==phase:
            path=prepared[0][0]/'coordinator.lock';path.rename(path.with_suffix('.old'));atomic_write(path,b'')
    with pytest.raises(Conflict):restore(prepared,result,fault_hook=altered)


def test_historical_unit_file_is_not_a_foreground_prerequisite(prepared):
    prepared[2].write_bytes(b'x'*16385)
    assert activate(prepared)['configured']
    assert prepared[3].calls==[]


def test_staged_switch_reader_and_schema_are_strict(prepared):
    from jsonschema import Draft202012Validator
    activate(prepared);repo=Path(__file__).resolve().parents[1]
    value=json.loads(Path(prepared[1]['directory'],'switch-intent.json').read_bytes())
    schema=json.loads((repo/'schemas/controller-endpoint-switch.v2.schema.json').read_bytes())
    validator=Draft202012Validator(schema);validator.validate(value)
    legacy=json.loads((repo/'examples/controller-endpoint-switch.json').read_bytes())
    old_schema=json.loads((repo/'schemas/controller-endpoint-switch.v1.schema.json').read_bytes())
    Draft202012Validator(old_schema).validate(legacy)
    assert switch.validate_switch(legacy)==legacy
    with pytest.raises(ContractError):switch.validate_switch(legacy|{'schema_version':1,'unit':'/old/controller.service'})
    assert switch.validate_switch(value)==value
    with pytest.raises(ContractError):switch.validate_switch(value|{'unexpected':True})
