"""Recorded development builds; systemd remains their sole execution owner."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import time

from .contracts import ContractError, canonical, identifier, digest
from .controller import controller_boot_id
from .state_config import discover_state_root, outside_checkout
from .state_reader import development_run,read_file
from .store import ArtifactStore, atomic_write
from .worker_service import SystemdUserWorkerServices


class DevelopmentServices(SystemdUserWorkerServices):
    @staticmethod
    def _unit(unit):
        if not isinstance(unit,str) or not re.fullmatch(r'quirkbench-build-[a-z0-9-]+\.service',unit):
            raise ContractError('invalid development build service')
        return unit


def prepare(unit, stage, log, status, arguments=()):
    DevelopmentServices._unit(unit)
    run_id = unit.removesuffix('.service')
    identifier(run_id)
    root = outside_checkout(discover_state_root())
    if not (root / 'controller.sqlite').is_file():
        raise ContractError('run quirkbench setup-state before starting a build')
    stage = outside_checkout(stage)
    if stage != root / 'development-runs' / run_id / 'work':
        raise ContractError(f'build stage must be {root}/development-runs/{run_id}/work')
    directory = stage.parent
    if directory.resolve() != directory or directory.stat().st_mode & 0o077:
        raise ContractError('development run directory must be canonical and private')
    if (directory / 'run.json').exists():
        raise ContractError('development run identity already used')
    for name in (log, status):
        if not re.fullmatch(r'[a-z][a-z0-9.-]+', name) or (directory / name).exists():
            raise ContractError('development log/status must be new plain filenames')
    atomic_write(directory / 'run.json', canonical({'schema_version': 1, 'run_id': run_id,
        'unit': unit, 'boot_id': controller_boot_id(), 'started': time.time(),
        'log': log, 'status': status, 'work': 'work','command_sha256':digest(canonical(list(arguments)))}))
    atomic_write(directory/'command.json',canonical({'podman_arguments':list(arguments)}))
    atomic_write(directory / status, b'queued\n')
    return {'run_id': run_id, 'state_root': str(root), 'log': str(directory / log),
            'monitor': f'quirkbench monitor --run {run_id}'}


def retain(root, run_id, *, outputs=(), abandon=False):
    """Explicitly preserve ad hoc outputs before making their work disposable."""
    root = outside_checkout(root)
    record = development_run(root, run_id)
    if record['unit'] != run_id + '.service':
        raise ContractError('development service differs from run identity')
    if record['exit_status'] is None:
        raise ContractError('development build has no terminal exit record')
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
    services = DevelopmentServices()
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
    return {'run_id': run_id, 'retained_outputs': refs, 'abandoned': abandon}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('unit'); parser.add_argument('stage', type=Path)
    parser.add_argument('log'); parser.add_argument('status')
    parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments=args.arguments[1:] if args.arguments[:1]==['--'] else args.arguments
    print(json.dumps(prepare(args.unit,args.stage,args.log,args.status,arguments),sort_keys=True))


if __name__ == '__main__':
    main()
