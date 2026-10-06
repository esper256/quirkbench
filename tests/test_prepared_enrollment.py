"""Real staged records/activation/cleanup; no second pairing protocol."""
import json

import pytest

from quirkbench import prepared_enrollment as prepared
from quirkbench import enrollment_activation
from quirkbench.contracts import ContractError, Conflict, canonical, digest
from quirkbench.enrollment import row_code
from test_enrollment_activation import received
from test_enrollment_credentials import publication as loopback_publication
from test_enrollment_certificate import bound
from test_enrollment import issuer


@pytest.fixture
def initialized(tmp_path):
    # Use a LAN-configured controller through the real setup route. The older
    # enrollment fixtures intentionally use loopback, unsuitable for USB trust.
    from pathlib import Path
    from quirkbench.controller_install import install
    from quirkbench.controller_setup import setup_controller
    from test_controller_install import make_archive
    from test_resumable_setup import observations
    runtime = Path(install(make_archive(tmp_path), data_home=tmp_path/'data')['runtime_root'])
    setup_controller(tmp_path/'state', request_id='initial', runtime_root=runtime, reserve_gib=0,
                     host='192.0.2.44', allow_lan=True, config_home=tmp_path/'config', **observations())
    return runtime


@pytest.fixture
def publication(bound):
    value = loopback_publication.__wrapped__(bound)
    controller = value[0]
    path = controller.root/'private/controller-service.json'
    config = json.loads(path.read_bytes())
    config['repository_endpoint']['url'] = 'https://192.0.2.44:9443'
    path.write_bytes(canonical(config))
    return value


@pytest.fixture
def staged(received, publication):
    control, result, pem, local = received
    controller = publication[0]
    request = json.loads((control/'enrollment/pending/request.json').read_bytes())
    with controller.transaction() as db:
        invitation = row_code(db.execute('SELECT * FROM enrollment_codes WHERE id=?', (request['code_id'],)).fetchone())
    private = json.loads((controller.root/'private/enrollment/codes'/digest(invitation['request_id'].encode())/'issuance.json').read_bytes())
    metadata = {'schema_version':1, 'record_type':'prepared-enrollment', 'preparation_id':'prepare-fixture',
                'prepared_media_sha256':'a'*64, 'media_instance_id':request['media_instance_id'],
                'invitation':invitation, 'code_sha256':digest(private['code'].encode())}
    prepared.stage(control, metadata, private['code'], verify_target=local['verify_target'])
    # Emulate the adapter's durable inspection before an already completed
    # activation, for cleanup-only boundary cases below.
    (control/'enrollment/pending'/prepared.CERTIFICATE).write_text(pem)
    return control, result, pem, local, metadata, private['code']


def test_staged_trust_is_private_and_replay_does_not_replace_identity(staged):
    control, result, pem, local, metadata, code = staged
    before = (control/'enrollment/pending/key.pem').read_bytes()
    prepared.stage(control, metadata, code, verify_target=local['verify_target'])
    assert (control/prepared.SECRET).read_text() == code
    assert (control/prepared.SECRET).stat().st_mode & 0o777 == 0o600
    assert (control/'enrollment/pending/key.pem').read_bytes() == before
    with pytest.raises(Conflict):
        prepared.stage(control, {**metadata, 'code_sha256':digest(('b'*43).encode())}, 'b'*43,
                       verify_target=local['verify_target'])


def test_cleanup_after_real_durable_activation_preserves_retry_records(staged):
    control, result, pem, local, metadata, code = staged
    enrollment_activation.activate_enrollment(control, result, pem, **local)
    # Simulate loss after activation but before bootstrap cleanup. connect must
    # inspect existing activation, not begin another request or network exchange.
    kwargs = {key:value for key,value in local.items() if key != 'activator'}
    answer = prepared.connect(control, prepared_media_sha256='a'*64, **kwargs)
    assert answer['enrolled'] and not answer['boot_authorized']
    assert not (control/prepared.SECRET).exists()
    assert (control/prepared.METADATA).exists()
    for name in ('request.json', 'key.pem', 'result.json'):
        assert (control/'enrollment/pending'/name).exists()
    assert prepared.connect(control, prepared_media_sha256='a'*64, **kwargs) == answer


@pytest.mark.parametrize('fault', ['layout', 'media', 'binding', 'secret', 'runtime'])
def test_cleanup_refuses_substitution_and_preserves_secret(staged, fault):
    control, result, pem, local, metadata, code = staged
    enrollment_activation.activate_enrollment(control, result, pem, **local)
    kwargs = {key:value for key,value in local.items() if key != 'activator'}
    layout = 'a'*64
    if fault == 'layout':layout = 'b'*64
    elif fault == 'media':
        (control/'media-instance.json').write_bytes(canonical({'schema_version':1, 'media_instance_id':'other-media'}))
    elif fault == 'binding':kwargs['binding_reader'] = lambda:'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    elif fault == 'secret':(control/prepared.SECRET).write_text('b'*43)
    else:
        runtime = json.loads((control/'runtime.json').read_bytes())
        runtime['controller_url'] = 'https://192.0.2.9:8443'
        (control/'runtime.json').write_bytes(canonical(runtime))
    with pytest.raises((ContractError, Conflict)):
        prepared.connect(control, prepared_media_sha256=layout, **kwargs)
    assert (control/prepared.SECRET).exists()


@pytest.mark.parametrize('endpoint', ['https://127.0.0.1:8443', 'https://0.0.0.0:8443',
    'https://[::1]:8443', 'https://169.254.1.1:8443'])
def test_unreachable_prepared_endpoint_rejected(staged, endpoint):
    metadata = staged[4]
    with pytest.raises(ContractError, match='LAN|specific endpoint'):
        prepared.validate_metadata({**metadata, 'invitation':{**metadata['invitation'], 'controller_url':endpoint}})


def test_rejected_storage_creates_no_staging_lock(staged, tmp_path):
    control = tmp_path/'rejected'; control.mkdir()
    def reject():raise Conflict('selected media changed')
    with pytest.raises(Conflict, match='selected media'):
        prepared.stage(control, staged[4], staged[5], verify_target=reject)
    assert list(control.iterdir()) == []


@pytest.mark.parametrize('fault', ['request-media', 'result-endpoint', 'certificate', 'coherent-ca'])
def test_cleanup_joins_original_media_endpoint_and_native_trust(staged, fault):
    control, result, pem, local, metadata, code = staged
    enrollment_activation.activate_enrollment(control, result, pem, **local)
    pending = control/'enrollment/pending'
    if fault == 'request-media':
        metadata['media_instance_id'] = 'substituted-media'
        (control/prepared.METADATA).write_bytes(canonical(metadata))
        (control/'media-instance.json').write_bytes(canonical({'schema_version':1,
            'media_instance_id':metadata['media_instance_id']}))
    elif fault == 'certificate':
        (pending/prepared.CERTIFICATE).write_text(result['repository_certificate_pem'])
    else:
        value = json.loads((pending/'result.json').read_bytes())
        if fault == 'result-endpoint':value['controller_url'] = 'https://192.0.2.9:8443'
        else:value['controller_ca_pem'] = value['repository_certificate_pem']
        # Replace the entire retained result, generation and runtime coherently:
        # local hash agreement must not substitute for original TLS trust.
        from quirkbench.provisioning import generation_description
        bundle = enrollment_activation._bundle(value,
            json.loads((pending/'request.json').read_bytes()), (pending/'key.pem').read_bytes())
        manifest, generation, active = generation_description(bundle)
        directory = control/'generations'/generation; directory.mkdir(exist_ok=True)
        for name, raw in bundle.items():(directory/name).write_bytes(raw)
        (directory/'generation.json').write_bytes(canonical(manifest))
        (control/'runtime.json').write_bytes(canonical(active))
        (pending/'result.json').write_bytes(canonical(value))
    if fault == 'coherent-ca':
        # Host-facing OpenSSL adapter: use real signature verification and
        # translate rejection into its normal nonzero process result.
        original = local['run']
        def rejecting_native(argv, **kw):
            if argv[:2] == ['openssl', 'verify']:
                from pathlib import Path
                from cryptography import x509
                from cryptography.exceptions import InvalidSignature
                import subprocess
                ca = x509.load_pem_x509_certificate(Path(argv[argv.index('-CAfile')+1]).read_bytes())
                cert = x509.load_pem_x509_certificate(Path(argv[-1]).read_bytes())
                try:ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
                except InvalidSignature:return subprocess.CompletedProcess(argv, 1, b'', b'invalid signature')
            return original(argv, **kw)
        local['run'] = rejecting_native
    with pytest.raises((Conflict, ContractError)):
        prepared.connect(control, prepared_media_sha256='a'*64,
                         **{key:value for key,value in local.items() if key != 'activator'})
    assert (control/prepared.SECRET).read_text() == code


@pytest.fixture
def fresh(publication, tmp_path, monkeypatch):
    from quirkbench import enrollment_client, enrollment_proof as proof
    from quirkbench import enrollment_console
    native_prerequisites = enrollment_console.require_native_tools
    monkeypatch.setattr(enrollment_console, 'require_native_tools',
        lambda:native_prerequisites(which=lambda name:'/fixture/native/'+name))
    from quirkbench.controller_tls import inspect_identity
    from quirkbench.enrollment import create_code
    from test_enrollment_credentials import Commands, complete
    from test_enrollment_proof import args
    from test_enrollment_activation import UUID
    controller, _, _, options = publication
    config = json.loads((controller.root/'private/controller-service.json').read_bytes())
    from pathlib import Path
    pem = Path(config['cert']).read_text()
    identity = inspect_identity(Path(config['cert']).parent, run=Commands())
    invitation = create_code(controller, 'prepared-target', 'prepared-invitation', **options)
    control = tmp_path/'fresh-control'; control.mkdir(mode=0o700)
    (control/'media-instance.json').write_bytes(canonical({'schema_version':1, 'media_instance_id':'fresh-media'}))
    metadata = {'schema_version':1, 'record_type':'prepared-enrollment', 'preparation_id':'prepare-fresh',
                'prepared_media_sha256':'a'*64, 'media_instance_id':'fresh-media',
                'invitation':invitation['record'], 'code_sha256':digest(invitation['code'].encode())}
    prepared.stage(control, metadata, invitation['code'], verify_target=lambda:True)
    calls = []; failures = {}; mutate = [lambda phase:None]
    commands = Commands()
    def run(argv, **kw):
        if argv[0] == 'systemctl':
            import subprocess
            calls.append(argv[1]); return subprocess.CompletedProcess(argv, 0, b'', b'')
        return commands(argv, **kw)
    def inspect(url, **kw):
        mutate[0]('inspect')
        return {'certificate_pem':pem, 'certificate_sha256':identity['certificate_sha256']}
    class Client:
        def __init__(self, *a, **kw):pass
        def post(self, route, body):
            calls.append(route); mutate[0](route)
            if route.endswith('challenge'):
                return proof.challenge(controller, body['request'], '192.0.2.2', **args(options))
            proof.reserve_redemption(controller, body['request'], body['challenge_id'], body['code'],
                                     body['signature'], **args(options))
            reply = complete(controller, body['request'], options)
            if failures.pop('reply', False):raise TimeoutError('lost redemption reply')
            return reply
    monkeypatch.setattr(enrollment_client, 'inspect_certificate', inspect)
    monkeypatch.setattr(enrollment_client, 'PinnedEnrollmentClient', Client)
    original = enrollment_activation.activate_enrollment
    def activate(*a, **kw):
        from quirkbench.provisioning import activate_bundle
        return original(*a, **kw, activator=lambda bundle, target, **opts:
            activate_bundle(bundle, target, validator=lambda path:json.loads(path.read_bytes()), **opts))
    monkeypatch.setattr(enrollment_activation, 'activate_enrollment', activate)
    return control, calls, failures, mutate, {'verify_target':lambda:True,
        'binding_reader':lambda:UUID, 'run':run}


@pytest.mark.parametrize('interruption', ['none', 'reply', 'activation'])
def test_fresh_exchange_lost_reply_and_activation_restart_retain_exact_request(fresh, interruption):
    control, calls, failures, mutate, options = fresh
    if interruption != 'none':
        if interruption == 'reply':failures['reply'] = True
        def fail(phase):raise KeyboardInterrupt()
        with pytest.raises(TimeoutError if interruption == 'reply' else KeyboardInterrupt):
            prepared.connect(control, prepared_media_sha256='a'*64, **options,
                             fault_hook=fail if interruption == 'activation' else None)
        assert (control/prepared.SECRET).exists()
        key = (control/'enrollment/pending/key.pem').read_bytes()
        request = (control/'enrollment/pending/request.json').read_bytes()
    answer = prepared.connect(control, prepared_media_sha256='a'*64, **options)
    assert answer['enrolled'] and not answer['boot_authorized']
    assert calls[0] == 'stop' and calls[-1] == 'start'
    assert not (control/prepared.SECRET).exists()
    if interruption != 'none':
        assert (control/'enrollment/pending/key.pem').read_bytes() == key
        assert (control/'enrollment/pending/request.json').read_bytes() == request
    if interruption == 'activation':assert calls.count('/v1/enrollment/redeem') == 1
    elif interruption == 'reply':assert calls.count('/v1/enrollment/redeem') == 2


@pytest.mark.parametrize('phase', ['inspect', '/v1/enrollment/challenge'])
@pytest.mark.parametrize('record', ['metadata', 'media', 'binding'])
def test_storage_or_binding_change_during_exchange_cannot_transmit_secret(fresh, phase, record):
    control, calls, failures, mutate, options = fresh
    if record == 'binding':
        from quirkbench.enrollment_target import prepare_request
        invitation = json.loads((control/prepared.METADATA).read_bytes())['invitation']
        prepare_request(control, invitation['controller_url'], invitation['certificate_sha256'],
                        invitation['code_id'], **options)
    def change(current):
        if current == phase:
            if record == 'binding':
                # Reader was already captured by connect, so mutate its source.
                binding[0] = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            else:
                path = control/(prepared.METADATA if record == 'metadata' else 'media-instance.json')
                value = json.loads(path.read_bytes())
                value['preparation_id' if record == 'metadata' else 'media_instance_id'] = 'substituted'
                path.write_bytes(canonical(value))
    binding = [options['binding_reader']()]
    options['binding_reader'] = lambda:binding[0]
    mutate[0] = change
    with pytest.raises((Conflict, ContractError)):
        prepared.connect(control, prepared_media_sha256='a'*64, **options)
    assert '/v1/enrollment/redeem' not in calls
    assert (control/prepared.SECRET).exists() and not (control/'runtime.json').exists()
    assert calls[-1] == 'start'


def test_changed_activation_sources_during_native_cleanup_preserve_secret(staged):
    control, result, pem, local, metadata, code = staged
    enrollment_activation.activate_enrollment(control, result, pem, **local)
    original = local['run']
    def changed(argv, **kw):
        answer = original(argv, **kw)
        if argv[0] == 'gpg':
            path = control/'enrollment/pending/result.json'
            value = json.loads(path.read_bytes()); value['device_id'] = 'changed-result'
            path.write_bytes(canonical(value))
        return answer
    with pytest.raises(Conflict, match='sources changed'):
        prepared.connect(control, prepared_media_sha256='a'*64,
            **{key:value for key,value in local.items() if key not in ('activator', 'run')}, run=changed)
    assert (control/prepared.SECRET).read_text() == code
