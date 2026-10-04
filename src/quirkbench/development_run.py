"""Recorded foreground development builds and explicit artifact retention."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import time

from .contracts import ContractError, canonical, identifier, digest, sha256
from .controller import controller_boot_id
from .state_config import discover_state_root, canonical_user_path
from .state_reader import development_run,read_file
from .store import ArtifactStore, atomic_write
from .development_container import DevelopmentServices


def validate_run(record):
    fields={'schema_version','run_id','unit','boot_id','started','log','status','work','command_sha256'}
    if (not isinstance(record,dict) or set(record)!=fields or type(record['schema_version']) is not int
            or record['schema_version']!=2):raise ContractError('invalid foreground development run')
    run_id=record['run_id'];identifier(run_id)
    if not re.fullmatch(r'quirkbench-build-[a-z0-9-]+',run_id):raise ContractError('invalid development run name')
    if record['unit']!='qb-development-v2-'+run_id or record['work']!='work':
        raise ContractError('development run identity differs')
    from .controller import validate_boot_id
    import math
    validate_boot_id(record['boot_id']);sha256(record['command_sha256'])
    if type(record['started']) not in (int,float) or not math.isfinite(record['started']):
        raise ContractError('invalid development start time')
    for name in ('log','status'):
        if not isinstance(record[name],str) or not re.fullmatch(r'[a-z][a-z0-9.-]+',record[name]):
            raise ContractError('invalid development diagnostic filename')
    if len(canonical(record))>8192:raise ContractError('development run exceeds read budget')
    return record


def prepare(unit, stage, log, status, arguments=()):
    DevelopmentServices._unit(unit)
    run_id = unit.removesuffix('.service')
    identifier(run_id)
    root = canonical_user_path(discover_state_root())
    if not (root / 'controller.sqlite').is_file():
        raise ContractError('run quirkbench setup-state before starting a build')
    stage = canonical_user_path(stage)
    if stage != root / 'development-runs' / run_id / 'work':
        raise ContractError(f'build stage must be {root}/development-runs/{run_id}/work')
    directory = stage.parent
    if directory.resolve() != directory:
        raise ContractError('development run directory must be canonical')
    if (directory / 'run.json').exists():
        raise ContractError('development run identity already used')
    for name in (log, status):
        if not re.fullmatch(r'[a-z][a-z0-9.-]+', name) or (directory / name).exists():
            raise ContractError('development log/status must be new plain filenames')
    atomic_write(directory / 'run.json', canonical(validate_run({'schema_version': 2, 'run_id': run_id,
        'unit': 'qb-development-v2-'+run_id, 'boot_id': controller_boot_id(), 'started': time.time(),
        'log': log, 'status': status, 'work': 'work','command_sha256':digest(canonical(list(arguments)))})))
    atomic_write(directory/'command.json',canonical({'podman_arguments':list(arguments)}))
    atomic_write(directory / status, b'queued\n')
    return {'run_id': run_id, 'state_root': str(root), 'log': str(directory / log),
            'monitor': f'quirkbench monitor --run {run_id}'}


def retain(root, run_id, *, outputs=(), abandon=False):
    """Explicitly preserve ad hoc outputs before making their work disposable."""
    root = canonical_user_path(root)
    record = development_run(root, run_id)
    expected=(run_id+'.service' if record.get('schema_version')==1 else 'qb-development-v2-'+run_id)
    if record['unit'] != expected:
        raise ContractError('development service differs from run identity')
    services = DevelopmentServices(root)
    if record['exit_status'] is None:
        if not abandon or not services.finished(record['unit'],record['boot_id']):
            raise ContractError('development build has no terminal exit record; stop its container before abandoning interrupted work')
        proof=services.stop_and_verify(record['unit'],record['boot_id'])
        run=Path(root)/'development-runs'/run_id
        atomic_write(run/'stopped.json',canonical({'unit':record['unit'],'boot_id':record['boot_id'],'proof':proof,'at':time.time()}))
        atomic_write(run/record['status'],b'130\n')
        record=development_run(root,run_id)
    if record['state'] != 'SUCCEEDED' and not abandon:
        raise ContractError('failed/interrupted work requires explicit --abandon')
    if record['state'] == 'SUCCEEDED' and (not outputs or abandon):
        raise ContractError('successful work requires --output paths to retain')
    run = root / 'development-runs' / run_id
    if len(outputs)>64:
        raise ContractError('retain at most 64 outputs per run')
    paths={}
    for name in outputs:
        relative=Path(name)
        path=run/'work'/relative
        if (relative.is_absolute() or '..' in relative.parts or len(name)>256
                or path.resolve()!=path or not path.is_file()):
            raise ContractError('output must be a regular file inside the recorded work directory')
        paths[str(relative)]=path
    try:
        stop=json.loads(read_file(root,run.relative_to(root)/'stopped.json',limit=4096))
    except FileNotFoundError:
        stop={}
    if (stop.get('unit')!=record['unit'] or stop.get('boot_id')!=record['boot_id']
            or stop.get('proof') not in ('stopped','previous_boot')):
        if record['boot_id'] == controller_boot_id() and not services.finished(record['unit'], record['boot_id']):
            raise ContractError('development build service is still active')
        proof = services.stop_and_verify(record['unit'], record['boot_id'])
        if proof not in ('stopped', 'previous_boot'):
            raise ContractError('development whole-service stop is unverified')
        stop={'unit':record['unit'],'boot_id':record['boot_id'],'proof':proof,'at':time.time()}
        # Persist before copying: a collected unit can no longer be queried on retry.
        atomic_write(run/'stopped.json',canonical(stop))
    store = ArtifactStore(root / 'artifacts')
    refs = {}
    for name,path in paths.items():
        refs[name] = store.put_file(path).sha256
    publication={'schema_version':1,'run':record,'outputs':refs,'abandoned':abandon,'terminal_state':record['state']}
    manifest=store.put(canonical(publication))
    # Existing refs make both outputs and their role map survive ordinary backups.
    db=sqlite3.connect((root/'controller.sqlite').as_uri()+'?mode=rw',uri=True)
    try:
        db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA synchronous=FULL')
        with db:
            db.executemany('INSERT OR IGNORE INTO refs(owner,digest) VALUES(?,?)',
                [(run_id,value) for value in (*refs.values(),manifest.sha256)])
    finally:
        db.close()
    from .retention import register
    register(root,'development',(*refs.values(),manifest.sha256),owner=run_id,
             state='SUCCEEDED' if record['state']=='SUCCEEDED' else 'FAILED',stop_proof=stop)
    atomic_write(run / 'published.json', canonical({**publication,'manifest_sha256':manifest.sha256}))
    if record.get('schema_version')==2:services.remove_stopped(record['unit'])
    return {'run_id': run_id, 'retained_outputs': refs, 'abandoned': abandon}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('unit'); parser.add_argument('stage', type=Path)
    parser.add_argument('log'); parser.add_argument('status')
    parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments=args.arguments[1:] if args.arguments[:1]==['--'] else args.arguments
    answer=prepare(args.unit,args.stage,args.log,args.status,arguments)
    print(json.dumps(answer,sort_keys=True),flush=True)
    directory=Path(answer['state_root'])/'development-runs'/answer['run_id']
    run=json.loads(read_file(directory,'run.json',limit=8192))
    import signal
    previous=signal.getsignal(signal.SIGTERM)
    def terminate(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    try:return DevelopmentServices(answer['state_root']).run(run,arguments)
    finally:signal.signal(signal.SIGTERM,previous)


if __name__ == '__main__':
    raise SystemExit(main())
