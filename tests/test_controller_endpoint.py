"""Retained-CA endpoint staging uses the stopped existing controller authority."""
import json
from pathlib import Path
import pytest
from quirkbench import controller_endpoint as endpoint,controller_tls as tls
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.controller import Controller
from quirkbench.maintenance import private_lock
from quirkbench.store import atomic_write
from tls_command_fixture import TLSCommands


@pytest.fixture
def configured(tmp_path):
    root=tmp_path/'state';Controller(root)
    native=TLSCommands();source=tls.create_identity(root,'127.0.0.1','initial',run=native)
    runtime=tmp_path/'runtime/bin';runtime.mkdir(parents=True)
    for name in ('quirkbench-controller-service','quirkbench-job-worker'):
        path=runtime/name;path.write_text('#!/bin/sh\n');path.chmod(0o755)
    config={'runtime':str(runtime/'quirkbench-controller-service'),'job_worker':str(runtime/'quirkbench-job-worker'),
        'cert':source['certificate'],'key':source['key'],'credential_registry':True,'host':'127.0.0.1','port':8443}
    atomic_write(root/'private/controller-service.json',canonical(config))
    return root,source,native


def stage(configured,**kw):
    root,source,native=configured
    return endpoint.stage_identity(root,'127.0.0.2','endpoint-1',source['identity_sha256'],run=kw.pop('run',native),**kw)


def test_successor_retains_ca_and_configuration_without_service_or_target_activation(configured):
    root,source,native=configured;before={p:p.read_bytes() for p in Path(source['directory']).iterdir() if p.is_file()}
    cfg=(root/'private/controller-service.json').read_bytes();answer=stage(configured);directory=Path(answer['directory'])
    assert answer['ca_retained'] and not answer['activated'] and not answer['targets_migrated']
    assert answer['certificate_sha256']!=source['certificate_sha256']
    assert tls.inspect_identity(directory,host='127.0.0.2',run=native)['identity_sha256']==answer['identity_sha256']
    assert all(p.read_bytes()==raw for p,raw in before.items()) and (root/'private/controller-service.json').read_bytes()==cfg
    assert {name:(directory/name).read_bytes() for name in ('ca.key','ca.crt')}=={name:before[Path(source['directory'])/name] for name in ('ca.key','ca.crt')}
    assert stage(configured)==answer


@pytest.mark.parametrize('phase',['endpoint_intent_retained','ca.key','ca.crt','controller.key','controller.csr','controller.crt','endpoint_identity_retained'])
def test_every_interruption_preserves_generated_keys_and_replays_same_stage(configured,phase):
    root,source,native=configured
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):stage(configured,fault_hook=fail)
    directory=root/'private/controller-tls'/('endpoint-'+digest(b'endpoint-1')[:32])
    retained={p:p.read_bytes() for p in directory.iterdir() if p.name in tls.FILES}
    answer=stage(configured);assert all(p.read_bytes()==raw for p,raw in retained.items())
    assert stage(configured)==answer


@pytest.mark.parametrize('lock',['command.lock','coordinator.lock'])
def test_existing_owners_block_before_new_tls_state(configured,lock):
    root,source,native=configured
    with private_lock(root/lock):
        with pytest.raises(Conflict):stage(configured)
    assert not any(p.name.startswith('endpoint-') for p in (root/'private/controller-tls').iterdir())


@pytest.mark.parametrize('change',['configuration','source-key','source-identity','command-inode','owner-inode','new-key'])
def test_native_callback_changes_cannot_publish_successor_identity(configured,change):
    root,source,native=configured;done=[False]
    def changed(argv,**kw):
        answer=native(argv,**kw)
        if argv[1]=='x509' and '-req' in argv and not done[0]:
            directory=Path(argv[argv.index('-out')+1]).parent
            if change=='configuration':atomic_write(root/'private/controller-service.json',b'{}')
            elif change=='source-key':atomic_write(Path(source['directory'])/'ca.key',b'changed')
            elif change=='source-identity':atomic_write(Path(source['directory'])/'identity.json',b'{}')
            elif change.endswith('inode'):
                path=root/('command.lock' if change=='command-inode' else 'coordinator.lock');path.rename(root/'lost.lock');atomic_write(path,b'')
            else:atomic_write(directory/'controller.key',b'changed')
            done[0]=True
        return answer
    with pytest.raises((Conflict,ContractError)):stage(configured,run=changed)
    directory=root/'private/controller-tls'/('endpoint-'+digest(b'endpoint-1')[:32])
    assert done[0] and not (directory/'identity.json').exists()


def test_changed_expected_source_or_replay_choice_and_native_deadline_do_not_replace_keys(configured):
    root,source,native=configured
    with pytest.raises(Conflict):endpoint.stage_identity(root,'127.0.0.2','endpoint-1','f'*64,run=native)
    first=stage(configured);key=Path(first['directory'],'controller.key').read_bytes()
    with pytest.raises(Conflict):endpoint.stage_identity(root,'127.0.0.3','endpoint-1',source['identity_sha256'],run=native)
    assert Path(first['directory'],'controller.key').read_bytes()==key
    now=[0]
    def delayed(argv,**kw):
        answer=native(argv,**kw);now[0]=181;return answer
    with pytest.raises(Conflict,match='deadline'):endpoint.stage_identity(root,'127.0.0.3','endpoint-2',source['identity_sha256'],run=delayed,monotonic=lambda:now[0])
    assert not list((root/'private/controller-tls'/('endpoint-'+digest(b'endpoint-2')[:32])).iterdir())


def shortened_ca(configured,remaining_days):
    from datetime import datetime,timezone,timedelta
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    root,source,native=configured;directory=Path(source['directory'])
    ca=x509.load_pem_x509_certificate((directory/'ca.crt').read_bytes());key=serialization.load_pem_private_key((directory/'ca.key').read_bytes(),None)
    old=x509.load_pem_x509_certificate((directory/'controller.crt').read_bytes());now=datetime.now(timezone.utc)
    def renew(cert,days):
        value=(x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer).public_key(cert.public_key())
            .serial_number(cert.serial_number).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=days)))
        for extension in cert.extensions:value=value.add_extension(extension.value,extension.critical)
        return value.sign(key,None).public_bytes(serialization.Encoding.PEM)
    atomic_write(directory/'ca.crt',renew(ca,remaining_days));atomic_write(directory/'controller.crt',renew(old,min(remaining_days,1)))
    record=json.loads((directory/'identity.json').read_bytes());record['files']={name:digest((directory/name).read_bytes()) for name in tls.FILES}
    atomic_write(directory/'identity.json',canonical(record))
    return root,tls.inspect_identity(directory,run=native),native


def test_certificate_lifetime_is_bounded_by_retained_ca_and_native_issuer_cannot_extend_it(configured):
    from cryptography import x509
    selected=shortened_ca(configured,30);answer=stage(selected);directory=Path(answer['directory'])
    ca=x509.load_pem_x509_certificate((directory/'ca.crt').read_bytes());leaf=x509.load_pem_x509_certificate((directory/'controller.crt').read_bytes())
    assert leaf.not_valid_after_utc<ca.not_valid_after_utc
    root,source,native=selected
    def too_long(argv,**kw):
        if argv[1]=='x509' and '-req' in argv:
            argv=list(argv);argv[argv.index('-days')+1]='365'
        return native(argv,**kw)
    with pytest.raises(Conflict,match='outlasts'):endpoint.stage_identity(root,'127.0.0.3','too-long',source['identity_sha256'],run=too_long)
    assert not (root/'private/controller-tls'/('endpoint-'+digest(b'too-long')[:32])/'identity.json').exists()


def test_insufficient_ca_lifetime_is_actionable_and_never_generates_new_server_key(configured):
    selected=shortened_ca(configured,1)
    with pytest.raises(Conflict,match='CA maintenance'):stage(selected)
    root=selected[0];directory=root/'private/controller-tls'/('endpoint-'+digest(b'endpoint-1')[:32])
    assert not (directory/'controller.key').exists() and not (directory/'identity.json').exists()


def test_versioned_endpoint_records_keep_initial_reader_and_actual_schemas(configured):
    from jsonschema import Draft202012Validator
    answer=stage(configured);root=Path(__file__).resolve().parents[1];directory=Path(answer['directory'])
    for kind in ('intent','identity'):
        schema=json.loads((root/f'schemas/controller-tls-{kind}.v2.schema.json').read_bytes());validator=Draft202012Validator(schema)
        validator.validate(json.loads((root/f'examples/controller-tls-{kind}.v2.json').read_bytes()))
        actual=json.loads((directory/(kind+'.json')).read_bytes());validator.validate(actual)
        reader=endpoint.validate_intent if kind=='intent' else tls.validate_identity
        assert reader(actual)==actual
        with pytest.raises(ContractError):reader(actual|{'extra':True})
    initial=json.loads((root/'examples/controller-tls-identity.json').read_bytes())
    assert tls.validate_identity(initial)==initial


@pytest.mark.parametrize('change',['modified','deleted'])
def test_final_intent_change_never_returns_success(configured,change):
    def changed(phase):
        if phase=='endpoint_identity_retained':
            directory=configured[0]/'private/controller-tls'/('endpoint-'+digest(b'endpoint-1')[:32])
            if change=='modified':atomic_write(directory/'intent.json',b'{}')
            else:(directory/'intent.json').unlink()
    with pytest.raises((Conflict,ContractError,OSError)):stage(configured,fault_hook=changed)


def test_all_native_ca_and_key_validation_temps_stay_inside_successor_stage(configured):
    root,source,native=configured;directory=root/'private/controller-tls'/('endpoint-'+digest(b'endpoint-1')[:32])
    def contained(argv,**kw):
        for flag in ('-CA','-CAkey','-CAfile','-key','-in'):
            if flag in argv:assert Path(argv[argv.index(flag)+1]).is_relative_to(directory)
        return native(argv,**kw)
    assert stage(configured,run=contained)['ca_retained']


def test_source_expiry_during_final_work_does_not_publish_successor(configured):
    from cryptography import x509
    root,source,native=configured;old=x509.load_pem_x509_certificate(Path(source['certificate']).read_bytes())
    now=[int(old.not_valid_after_utc.timestamp())-120]
    def crossed(argv,**kw):
        answer=native(argv,**kw)
        if argv[1]=='x509' and '-req' in argv:now[0]=int(old.not_valid_after_utc.timestamp())
        return answer
    with pytest.raises(Conflict,match='expired'):stage(configured,run=crossed,clock=lambda:now[0])
    directory=root/'private/controller-tls'/('endpoint-'+digest(b'endpoint-1')[:32])
    assert not (directory/'identity.json').exists()


def expired_source(configured,*,expired_ca=False):
    from datetime import datetime,timezone,timedelta
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    root,source,native=configured;directory=Path(source['directory']);now=datetime.now(timezone.utc)
    key=serialization.load_pem_private_key((directory/'ca.key').read_bytes(),None)
    for name in ('controller.crt','ca.crt') if expired_ca else ('controller.crt',):
        cert=x509.load_pem_x509_certificate((directory/name).read_bytes())
        value=(x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer).public_key(cert.public_key())
            .serial_number(cert.serial_number).not_valid_before(now-timedelta(days=3)).not_valid_after(now-timedelta(days=1)))
        for extension in cert.extensions:value=value.add_extension(extension.value,extension.critical)
        atomic_write(directory/name,value.sign(key,None).public_bytes(serialization.Encoding.PEM))
    record=json.loads((directory/'identity.json').read_bytes());record['files']={name:digest((directory/name).read_bytes()) for name in tls.FILES}
    atomic_write(directory/'identity.json',canonical(record))
    return root,source|{'identity_sha256':digest(canonical(record))},native


def renew(configured,**kw):
    root,source,native=configured
    return endpoint.renew_expired_identity(root,'127.0.0.2','renewal-1',source['identity_sha256'],run=kw.pop('run',native),**kw)


def test_expired_source_renewal_keeps_ca_and_full_destination_checks_separate(configured):
    selected=expired_source(configured);root,source,native=selected;before={p:p.read_bytes() for p in Path(source['directory']).iterdir() if p.is_file()}
    historical=[];current=[]
    def checked(argv,**kw):
        if argv[1]=='verify':
            path=Path(argv[-1])
            (historical if '-no_check_time' in argv else current).append(path)
            if '-no_check_time' in argv:assert path.read_bytes()==before[Path(source['certificate'])]
        return native(argv,**kw)
    answer=renew(selected,run=checked);assert answer['expired_source_renewal'] and not answer['activated']
    assert historical and current and all(p.read_bytes()==raw for p,raw in before.items())
    assert renew(selected)==answer
    assert tls.inspect_identity(Path(answer['directory']),run=native)['certificate_sha256']==answer['certificate_sha256']
    # Normal inspection/readiness cannot inherit source-only historical mode.
    with pytest.raises(AssertionError):tls.inspect_identity(Path(source['directory']),run=native)
    with pytest.raises(AssertionError):stage(selected)


def test_explicit_expired_renewal_refuses_live_source_and_expired_ca(configured):
    with pytest.raises(Conflict,match='still valid'):renew(configured)
    selected=expired_source(configured,expired_ca=True)
    with pytest.raises(AssertionError):renew(selected)
    assert not (selected[0]/'private/controller-tls'/('endpoint-'+digest(b'renewal-1')[:32])/'identity.json').exists()


@pytest.mark.parametrize('phase',['endpoint_intent_retained','controller.key','controller.crt','endpoint_identity_retained'])
def test_expired_renewal_interruptions_retain_same_keys_and_typed_intent(configured,phase):
    selected=expired_source(configured);root=selected[0]
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):renew(selected,fault_hook=fail)
    directory=root/'private/controller-tls'/('endpoint-'+digest(b'renewal-1')[:32]);retained={p:p.read_bytes() for p in directory.iterdir() if p.name in tls.FILES}
    answer=renew(selected);assert all(p.read_bytes()==raw for p,raw in retained.items())
    intent=json.loads((directory/'intent.json').read_bytes());assert intent['schema_version']==3 and intent['source_mode']=='expired-leaf-renewal'
    with pytest.raises(Conflict):endpoint.stage_identity(root,'127.0.0.2','renewal-1',selected[1]['identity_sha256'],run=selected[2])


def test_expired_renewal_v3_intent_schema_and_strict_reader(configured):
    from jsonschema import Draft202012Validator
    answer=renew(expired_source(configured));root=Path(__file__).resolve().parents[1]
    validator=Draft202012Validator(json.loads((root/'schemas/controller-tls-intent.v3.schema.json').read_bytes()))
    validator.validate(json.loads((root/'examples/controller-tls-intent.v3.json').read_bytes()))
    record=json.loads(Path(answer['directory'],'intent.json').read_bytes());validator.validate(record)
    assert endpoint.validate_intent(record)==record
    with pytest.raises(ContractError):endpoint.validate_intent(record|{'source_mode':'tls-bypass'})
