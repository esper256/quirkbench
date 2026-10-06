"""Prepared-USB initial enrollment over the existing request/key machinery.

Controller preparation authorizes this staged trust. This adapter never selects
another invitation, replaces an active configuration, or authorizes a run.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import re
import subprocess
import time

from .contracts import Conflict, ContractError, canonical, digest, identifier, sha256
from .enrollment_records import _document, validate_code
from .filesystem import _read, private_lock
from .store import atomic_write, sync_directory

METADATA = 'prepared-enrollment.json'
SECRET = 'prepared-enrollment.code'
CERTIFICATE = 'prepared-controller.pem'


def validate_metadata(value):
    fields = {'schema_version', 'record_type', 'preparation_id', 'prepared_media_sha256',
              'media_instance_id', 'invitation', 'code_sha256'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['record_type'] != 'prepared-enrollment'):
        raise ContractError('invalid prepared enrollment metadata')
    identifier(value['preparation_id']); identifier(value['media_instance_id'])
    sha256(value['prepared_media_sha256'])
    sha256(value['code_sha256'])
    invitation = validate_code(value['invitation'])
    if invitation['schema_version'] != 2 or invitation['expires_at'] is not None:
        raise ContractError('prepared initial enrollment requires a non-expiring invitation')
    from .enrollment_client import endpoint
    host, _ = endpoint(invitation['controller_url'])
    # The existing activation supports literal-IP SANs. Do not invent a second
    # DNS enrollment path or pretend a bind-any/loopback address reaches the LAN.
    try: address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ContractError('prepared controller requires its configured literal LAN address') from exc
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ContractError('prepared controller address must be reachable from the target LAN; repair admin connection configuration')
    return value


def stage(control, metadata, code, *, verify_target):
    """Stage only on controller-verified selected media; publish metadata last."""
    metadata = validate_metadata(metadata)
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code):
        raise ContractError('invalid prepared single-use code')
    if digest(code.encode()) != metadata['code_sha256']:
        raise Conflict('prepared code differs from metadata')
    from .enrollment_target import _media, _storage
    control, verify_target = _storage(control, verify_target)
    with private_lock(control/'runtime-config.lock'):
        verify_target()
        if (control/'runtime.json').exists() or (control/'runtime.json').is_symlink():
            raise Conflict('prepared enrollment cannot replace an active configuration')
        _media(control, metadata['media_instance_id'])
        for name, raw in ((SECRET, code.encode()), (METADATA, canonical(metadata))):
            verify_target()
            path = control/name
            if path.exists() or path.is_symlink():
                if _read(control, name) != raw:
                    raise Conflict('prepared enrollment differs from retained preparation')
            else:
                atomic_write(path, raw)


def _finish_cleanup(control, metadata, *, verify_target, binding_reader, run, clock):
    """Recognize activation-before-secret-deletion without making a new request."""
    from .binding import verify_binding
    from .enrollment_activation import _bundle, _validate_native
    from .enrollment_target import _media, _saved
    from .enrollment_result import validate_result
    from .enrollment_records import _now
    with private_lock(control/'runtime-config.lock'):
        verify_target(); _media(control, metadata['media_instance_id'])
        pending = control/'enrollment/pending'
        captured = {name:_read(pending, name) for name in
                    ('intent.json', 'request.json', 'key.pem', 'result.json', CERTIFICATE)}
        intent = _document(captured['intent.json'])
        invitation = metadata['invitation']
        if (intent['controller_url'] != invitation['controller_url']
                or intent['certificate_sha256'] != invitation['certificate_sha256']
                or intent['code_id'] != invitation['code_id']):
            raise Conflict('active enrollment differs from prepared trust')
        request, private = _saved(pending, intent, run=run)
        if request['media_instance_id'] != metadata['media_instance_id']:
            raise Conflict('active request belongs to another prepared media identity')
        verify_binding(request['target_binding'], reader=binding_reader)
        result = validate_result(_document(captured['result.json']), request)
        if result['schema_version'] != 1 or result['controller_url'] != invitation['controller_url']:
            raise Conflict('active result differs from prepared initial controller enrollment')
        _validate_native(pending, intent, result, private, captured[CERTIFICATE].decode('ascii'),
                         verify_target, run)
        if not _now(clock) < result['credential_generation']['expires_at']:
            raise Conflict('active credentials expired; prepared secret remains for explicit repair')
        expected = _bundle(result, request, private)
        from .provisioning import generation_description
        manifest, generation, active = generation_description(expected)
        directory = control/'generations'/generation
        if (set(path.name for path in directory.iterdir()) != set(expected)|{'generation.json'}
                or any(_read(directory, name) != raw for name, raw in expected.items())
                or _read(directory, 'generation.json') != canonical(manifest)
                or _read(control, 'runtime.json') != canonical(active)):
            raise Conflict('durable activation differs from prepared enrollment result')
        verify_target(); _media(control, metadata['media_instance_id'])
        if _read(control, METADATA) != canonical(metadata):
            raise Conflict('prepared enrollment changed before secret cleanup')
        verify_binding(request['target_binding'], reader=binding_reader)
        if (any(_read(pending, name) != raw for name, raw in captured.items())
                or any(_read(directory, name) != raw for name, raw in expected.items())
                or _read(directory, 'generation.json') != canonical(manifest)
                or _read(control, 'runtime.json') != canonical(active)
                or not _now(clock) < result['credential_generation']['expires_at']):
            raise Conflict('authenticated activation sources changed before secret cleanup')
        path = control/SECRET
        if path.exists() or path.is_symlink():
            # Check ownership/substitution through the same secret-file reader.
            if digest(_read(control, SECRET)) != metadata['code_sha256']:
                raise Conflict('staged bootstrap secret changed before cleanup')
            path.unlink(); sync_directory(control)
        return {'enrolled':True, 'boot_authorized':False, 'device_id':result['device_id']}


def connect(control, *, verify_target, prepared_media_sha256, binding_reader=None,
            clock=time.time, run=subprocess.run, fault_hook=None):
    from .enrollment_target import _storage
    control, verify_target = _storage(control, verify_target)
    with private_lock(control/'prepared-enrollment.lock'):
        return _connect(control, verify_target=verify_target,
                        prepared_media_sha256=prepared_media_sha256,
                        binding_reader=binding_reader, clock=clock, run=run, fault_hook=fault_hook)


def _connect(control, *, verify_target, prepared_media_sha256, binding_reader,
             clock, run, fault_hook):
    """A timed-out exchange retries the same retained request with a fresh proof."""
    from .binding import read_system_uuid
    from .enrollment_client import inspect_certificate, PinnedEnrollmentClient
    from .enrollment_target import prepare_request, sign_challenge, _storage, _media
    from .enrollment_activation import activate_enrollment
    from .enrollment_console import connect_initial_controller
    control, verify_target = _storage(control, verify_target)
    binding_reader = binding_reader or read_system_uuid
    metadata = validate_metadata(_document(_read(control, METADATA)))
    if metadata['prepared_media_sha256'] != sha256(prepared_media_sha256):
        raise Conflict('prepared enrollment belongs to another prepared layout')
    verify_target(); _media(control, metadata['media_instance_id'])
    if (control/'runtime.json').exists() or (control/'runtime.json').is_symlink():
        return _finish_cleanup(control, metadata, verify_target=verify_target,
                               binding_reader=binding_reader, run=run, clock=clock)
    invitation = metadata['invitation']
    def exchange(selected, **unused):
        def unchanged():
            verify_target(); _media(control, metadata['media_instance_id'])
            if _read(control, METADATA) != canonical(metadata):
                raise Conflict('prepared enrollment changed during exchange')
        unchanged()
        observation = inspect_certificate(invitation['controller_url'], clock=clock)
        if observation['certificate_sha256'] != invitation['certificate_sha256']:
            raise Conflict('controller certificate changed; repair prepared trust, no code transmitted')
        client = PinnedEnrollmentClient(invitation['controller_url'], observation['certificate_pem'],
                                        invitation['certificate_sha256'], clock=clock)
        request = prepare_request(control, invitation['controller_url'], invitation['certificate_sha256'],
                                  invitation['code_id'], verify_target=unchanged,
                                  binding_reader=binding_reader, run=run)
        from .binding import verify_binding
        def bound():
            unchanged()
            verify_binding(request['target_binding'], reader=binding_reader)
        bound()
        pending = control/'enrollment/pending'
        certificate = observation['certificate_pem'].encode('ascii')
        if (pending/CERTIFICATE).exists() or (pending/CERTIFICATE).is_symlink():
            if _read(pending, CERTIFICATE) != certificate:
                raise Conflict('prepared controller certificate differs from retained inspection')
        else:
            bound(); atomic_write(pending/CERTIFICATE, certificate)
        code = _read(control, SECRET).decode('ascii')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', code):
            raise ContractError('invalid staged single-use code')
        if digest(code.encode()) != metadata['code_sha256']:
            raise Conflict('staged bootstrap secret changed before exchange')
        bound()
        nonce = client.post('/v1/enrollment/challenge', {'schema_version':1, 'request':request})
        signature = sign_challenge(control, nonce, verify_target=bound,
                                   binding_reader=binding_reader, run=run, clock=clock)
        bound()
        if _read(control, SECRET).decode('ascii') != code:
            raise Conflict('prepared code changed before redemption')
        result = client.post('/v1/enrollment/redeem', {'schema_version':1, 'request':request,
                            'challenge_id':nonce['challenge_id'], 'code':code, 'signature':signature})
        answer = activate_enrollment(control, result, observation['certificate_pem'],
                                     verify_target=bound, binding_reader=binding_reader,
                                     run=run, clock=clock)
        (fault_hook or (lambda _:None))('activation_durable')
        _finish_cleanup(control, metadata, verify_target=verify_target,
                        binding_reader=binding_reader, run=run, clock=clock)
        return answer
    return connect_initial_controller(control=control, verify_target=verify_target,
                                      enroll=exchange, run=run)
