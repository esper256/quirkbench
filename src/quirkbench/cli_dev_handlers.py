"""Parsed command handlers over existing application services."""
from __future__ import annotations
import json
import sqlite3
import sys
import time
from pathlib import Path
from dataclasses import asdict
from .contracts import CapabilityReport, Experiment, canonical, digest
from .controller import Controller
from .state_config import configure_state_root, discover_state_root
from .cli_output import emit, error

def replay_check(args):
    from .recovery_replay import check_replay
    from .recovery_acquisition import load_spec, MAX_SPEC
    from .filesystem import read_file
    try:
        path = args.spec.expanduser().absolute()
        selected = load_spec(read_file(path.parent.resolve(strict=True), path.name, limit=MAX_SPEC))
        answer = check_replay(selected, args.directory)
        emit(args,answer)
        return 0 if answer['selected_inputs_available'] else 4
    except (OSError, ValueError, RuntimeError) as exc:
        error(args,'RPM replay inventory unavailable: '+str(exc))
        return 2


def candidate_spec(args):
    from .recovery_acquisition import stock_candidate_spec
    try:
        answer = stock_candidate_spec(args.candidate, args.repository, args.repository_id)
        emit(args,answer)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        error(args,'Stock specification unavailable: '+str(exc))
        return 2


def release_check(args):
    from .release_plan import inspect
    from .release_trust import ReleaseUnavailable
    from .build import BuildError
    from .operations import operation_response
    from .state_reader import safe_text
    try:
        value=inspect(args.directory,args.inputs,trust_bundle=args.trust_bundle,baseline=args.baseline,timeout_s=args.timeout)
        if args.json:print(json.dumps(operation_response(data=value),sort_keys=True))
        else:print(safe_text(json.dumps(value,indent=2,sort_keys=True)))
        return 0 if value['input_closure_complete'] else 2
    except (OSError,ValueError,BuildError) as exc:
        message=safe_text(str(exc))[:512]
        code,status=(('UNAVAILABLE',4) if isinstance(exc,ReleaseUnavailable) else ('INVALID_INPUT',2) if isinstance(exc,ValueError) else ('INFRASTRUCTURE',5))
        if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':status==5}),sort_keys=True))
        else:print(message,file=sys.stderr)
        return status


def development_install(args):
    from .controller_install import install, activate, rollback
    from .contracts import Conflict, ContractError
    from .operations import operation_response
    try:
        release_inputs = (args.release_statement, args.release_signature, args.release_key, args.release_fingerprint)
        asset_inputs = (args.release_recovery, args.release_builder, args.release_catalog)
        sidecars = (args.release_recovery_manifest, args.release_recovery_candidate)
        assets = None
        if any(value is not None for value in (*asset_inputs, *sidecars)):
            if not all(value is not None for value in (*release_inputs, *asset_inputs)):
                raise ContractError('release assets require complete signed release inputs and all three asset paths')
            assets = dict(zip(('recovery_image', 'builder_archive', 'baseline_catalog'), asset_inputs))
            if any(value is not None for value in sidecars):
                if not all(value is not None for value in sidecars):
                    raise ContractError('release recovery compatibility requires both manifest and candidate')
                assets.update(zip(('recovery_manifest', 'recovery_candidate'), sidecars))
        authenticated = None
        if any(value is not None for value in release_inputs):
            if not all(value is not None for value in release_inputs) or args.rollback or args.archive is None:
                raise ContractError('release verification requires archive, statement, signature, independent key and fingerprint')
            from .controller_release import bounded_file, verify_release
            parameters = {'assets': assets} if assets is not None else {}
            authenticated = verify_release(args.archive, bounded_file(args.release_statement, 16384),
                bounded_file(args.release_signature, 65536), args.release_key, args.release_fingerprint, **parameters)
        if args.rollback:
            if args.archive or args.activate: raise ContractError('--rollback takes no archive or --activate')
            answer = rollback(discover_state_root(args.state))
        else:
            if args.archive is None: raise ContractError('dev install requires ARCHIVE')
            if authenticated is None:
                answer = install(args.archive)
            else:
                statement = authenticated['statement']
                answer = install(args.archive, expected_archive_sha256=statement['controller_archive_sha256'],
                                 expected_version=statement['controller_version'])
            if args.activate: answer = activate(answer, discover_state_root(args.state))
            if authenticated is not None:
                answer = {**answer, 'distribution_verification': authenticated}
        print(json.dumps(operation_response(data=answer), sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        code = 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, ContractError) else 'INFRASTRUCTURE'
        print(json.dumps(operation_response(error={'code':code,'message':str(exc)[:512],'retryable':False}), sort_keys=True))
        return 3 if code == 'CONFLICT' else 2 if code == 'INVALID_INPUT' else 5
