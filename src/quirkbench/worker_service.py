"""Container-backed workers owned by the foreground controller.

The historical module/error names are retained for callers. No host service
manager participates in admission, execution or shutdown.
"""
from __future__ import annotations

from .process_identity import WorkerServiceError, verify_empty_cgroup
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .contracts import canonical, Conflict, digest
from .process_identity import controller_boot_id, validate_boot_id
from .filesystem import read_file
from .state_reader import StateReader
from .store import atomic_write
from .build import BuildError
from .container_engine import ContainerEngine, bounded_run

UNIT = re.compile(r'qb-worker-v2-[0-9a-f]{32}-[1-9][0-9]*\Z')
LEGACY_UNIT = re.compile(r'quirkbench-worker-[0-9a-f]{32}-[1-9][0-9]*\.service\Z')
LABEL = 'org.quirkbench.worker-v2'




def _run(argv, timeout=30):
    return bounded_run(argv, timeout=timeout)


class ContainerWorkerServices:
    def __init__(self, *, worker_program=None, engine='podman', worker_image=None,
                 runner=_run, boot_id_reader=controller_boot_id, clock=time.time,
                 development=False, monotonic=time.monotonic, resource_options=None):
        self.worker_program = worker_program  # compatibility with installed launchers
        self.engine, self.worker_image = engine, worker_image
        self.runner, self.boot_id_reader, self.clock = runner, boot_id_reader, clock
        self.monotonic=monotonic
        self.root = None
        self.development = development
        self.resource_options = resource_options or {}
        self.cpu_count = None
        self.memory_limit = None
        self._claim_reader = None

    @staticmethod
    def worker_identity(operation, generation):
        return f'qb-worker-v2-{operation}-{generation}'

    @property
    def backend(self):
        return ContainerEngine(self.engine, runner=self.runner, error=WorkerServiceError)

    def command(self, *args):
        return self.backend.command(*args)

    def _invoke(self, *args, timeout=30):
        return self.backend.invoke(*args, timeout=timeout)

    def _engine_identity(self):
        field = '{{.ID}}' if self.engine == 'docker' else '{{.Store.GraphRoot}}'
        value = self._invoke('info', '--format', field).strip()
        if not value or len(value)>4096 or '\n' in value:
            raise WorkerServiceError('local engine identity is unavailable')
        return self.engine+':'+str(os.geteuid())+':'+value

    def _image(self, identity):
        if not isinstance(identity,str) or not re.fullmatch('sha256:[0-9a-f]{64}',identity):
            raise WorkerServiceError('configure an exact local --worker-image sha256 ID')
        result = json.loads(self._invoke('image','inspect',identity))
        if not isinstance(result,list) or len(result)!=1:
            raise WorkerServiceError('builder image identity is ambiguous')
        item=result[0]
        if (item.get('Id',item.get('ID','')).removeprefix('sha256:')!=identity[7:]
                or item.get('Os',item.get('OS'))!='linux'
                or item.get('Config',{}).get('Entrypoint') not in (None,[])):
            raise WorkerServiceError('local worker image differs or overrides the fixed entry point')
        return item

    def preflight(self, state_root, deadline):
        remaining = self.engine_preflight(state_root, deadline)
        if self.worker_image is None:
            from .controller_service import configuration
            from .installed_release import inspect_selected
            from .builder_setup import retained_builder
            try:
                config = configuration(self.root)
                if not config.get('runtime'):
                    raise ValueError('no installed controller runtime is configured')
                selected = retained_builder(self.root, inspect_selected(Path(config['runtime']).parent.parent))
            except (OSError, ValueError) as exc:
                raise WorkerServiceError('prepare the signed first builder with setup --builder-archive; no verified worker image is selected') from exc
            self.worker_image = selected['builder_config_digest']
        self._image(self.worker_image)
        return remaining

    def preflight_operation(self, state_root, deadline, operation_id):
        with StateReader(state_root).connection() as db:
            row = db.execute('SELECT kind FROM operations WHERE id=?', (operation_id,)).fetchone()
        if row is not None and row['kind'] == 'builder_prepare':
            return self.engine_preflight(state_root, deadline)
        return self.preflight(state_root, deadline)

    def engine_preflight(self, state_root, deadline):
        root=Path(state_root)
        if not root.is_absolute() or root.resolve()!=root or root.is_symlink():
            raise WorkerServiceError('worker state root must be canonical')
        from .recovery_podman import _canonical
        _canonical(root,directory=True)
        if type(deadline) not in (int,float) or not 0<deadline-self.clock()<=86400:
            raise WorkerServiceError('worker deadline must be bounded and in the future')
        self.root=root
        if os.geteuid()==0:
            raise WorkerServiceError('run controller workers as a regular Linux user')
        if self.engine=='docker':
            endpoint=(os.environ.get('DOCKER_HOST') if not os.environ.get('DOCKER_CONTEXT') else None)
            if not endpoint:
                endpoint=self._invoke('context','inspect','--format','{{.Endpoints.docker.Host}}').strip()
            if not endpoint.startswith('unix:///'):
                raise WorkerServiceError('controller workers require a local Docker Unix socket and shared filesystem')
        self._engine_identity()
        return deadline-self.clock()

    def _path(self, unit):
        if self.root is None or not isinstance(unit,str) or not UNIT.fullmatch(unit):
            raise WorkerServiceError('unknown container worker identity; legacy workers require reconciliation')
        return self.root/'worker-executions'/ (unit+'.json')

    def _load(self, unit):
        path=self._path(unit)
        from .worker_execution import load
        record=load(read_file(path.parent,path.name,limit=65536),self.root)
        if (record.get('schema_version') not in (2,3) or record.get('unit')!=unit
                or record.get('engine')!=self.engine
                or record.get('engine_identity')!=self._engine_identity()):
            raise WorkerServiceError('worker execution backend differs from its recorded owner')
        return record

    def _save(self, record):
        from .worker_execution import validate
        validate(record,self.root)
        path=self._path(record['unit'])
        if path.parent.is_symlink() or path.parent.resolve()!=path.parent:
            raise WorkerServiceError('worker execution journal is linked')
        path.parent.mkdir(mode=0o700,exist_ok=True)
        atomic_write(path,canonical(record))

    def _handshake(self,record,phase):
        from .worker_execution import PHASES
        if phase not in PHASES[1:]:raise WorkerServiceError('unknown fixed worker phase')
        path=self._path(record['unit']).parent/record['unit']/('handshake-'+phase)
        if path.resolve()!=path:raise WorkerServiceError('worker handshake path is linked')
        path.mkdir(mode=0o700,exist_ok=True)
        return path

    def _current(self, record):
        with StateReader(self.root).connection() as db:
            epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            row=db.execute('SELECT * FROM operations WHERE id=?',(record['claim']['id'],)).fetchone()
        fields=('worker_epoch','worker_generation','worker_unit','worker_boot_id','stage','stage_dir','input_digest','deadline')
        if (row is None or row['state']!='RUNNING' or epoch!=record['claim']['worker_epoch']
                or any(row[k]!=record['claim'][k] for k in fields)
                or self.clock()>=row['deadline'] or self.monotonic()>=record['monotonic_deadline']):
            raise WorkerServiceError('worker claim changed or expired')

    def _inspect(self, record, execution):
        return self.backend.owned(execution['name'], execution['image'], LABEL,
                                  record['unit'], execution.get('id'))

    def _stop(self, record, execution):
        return self.backend.stop(execution['name'], execution['image'], LABEL,
                                 record['unit'], execution.get('id'))

    def _start(self, record, phase):
        from .worker_container_plan import plan
        self._current(record)
        from .resource_budget import resolve
        workload=('preparation' if record['claim']['kind'] in ('source_prepare','source_capture','builder_prepare','recovery_download')
                  else 'recovery' if record['claim']['kind']=='image_prepare' else 'kernel')
        try:
            budget=resolve(workload,**self.resource_options)
        except BuildError as exc:raise WorkerServiceError(str(exc)) from exc
        self.cpu_count,self.memory_limit=budget.cpus,budget.memory_bytes
        spec=plan(self,record,phase)
        self._image(spec['image'])
        execution={'name':'qb-'+uuid.uuid4().hex,'image':spec['image'],'phase':phase,
                   'start_requested':False}
        record['executions'].append(execution)
        record['phase']=phase
        self._save(record)  # creation name survives even an ambiguous create RPC
        remaining=max(1,math.ceil(min(record['claim']['deadline']-self.clock(),
                                      record['monotonic_deadline']-self.monotonic())))
        command=['create','--name',execution['name'],'--label',LABEL+'='+record['unit'],
                 '--pull=never','--network='+spec.get('network','none'),
                 '--ipc=private','--cgroupns=host',
                 *self.backend.containment_args(self.cpu_count,self.memory_limit),
                 '--user='+spec.get('user',str(os.getuid())+':'+str(os.getgid())),
                 '--env=PYTHONDONTWRITEBYTECODE=1','--env=PYTHONPATH='+spec['pythonpath'],
                 '--env=QUIRKBENCH_BUILDER_CONFIG_DIGEST='+spec['image']]
        if self.engine=='podman' and spec.get('user')!='0': command+=['--userns=keep-id']
        if self.engine=='podman': command+=['--timeout='+str(remaining)]
        command+=spec.get('options',[])
        for source,target,mode in spec['mounts']:
            from .recovery_podman import _canonical
            _canonical(Path(source),directory=Path(source).is_dir())
            command+=['--volume',str(source)+':'+str(target)+':'+mode]
        command += ['--env=QUIRKBENCH_WORKER_RECORD='+str(self._path(record['unit']))]
        command += [spec['image'],'python3','-m','quirkbench.container_worker',str(remaining),*spec['payload']]
        identity=self.backend.create(*command[1:])
        execution['id']=identity;self._save(record)
        value=self._inspect(record,execution)
        self.backend.validate_limits(value,self.cpu_count,self.memory_limit)
        self._current(record)
        execution['start_requested']=True;self._save(record)
        # Workers observe a separate immutable dispatch copy, never the host's
        # authoritative stop/reconciliation journal. This also keeps privileged
        # rootless composition mounts away from controller execution records.
        atomic_write(self._handshake(record,phase)/(record['unit']+'.json'),canonical(record))
        self.backend.start(identity)

    def launch(self, claim, state_root):
        self.preflight_operation(state_root,claim['deadline'],claim['id'])
        unit=claim['worker_unit']; path=self._path(unit)
        if unit!=self.worker_identity(claim['id'],claim['worker_generation']) or path.exists():
            raise WorkerServiceError('worker execution identity is already used')
        from .controller_service import configuration
        config=configuration(self.root)
        from .worker_container_plan import intent_document, capture_runtime, private_inputs
        from .worker_execution import CLAIM_FIELDS
        intent=intent_document(self.root,claim)
        bootstrap = claim['kind'] == 'builder_prepare'
        record={'schema_version':2,'unit':unit,'engine':self.engine,
                'engine_identity':self._engine_identity(),'claim':{k:claim[k] for k in CLAIM_FIELDS},
                'worker_image':self.worker_image,'reserve_bytes':int(config.get('reserve_gib',20)*1024**3),
                'monotonic_deadline':self.monotonic()+min(86400,claim['deadline']-self.clock()),
                'payload_args':{k:v for k,v in intent['arguments'].items() if k=='recipe_sha256'},
                'executions':[],'phase':'reserved','complete':False}
        if bootstrap:
            record.update(schema_version=3, bootstrap=True,
                          worker_image=intent['arguments']['builder_config_digest'])
        self._save(record)
        try:
            if bootstrap:
                from .worker_container_plan import bootstrap_builder
                bootstrap_builder(self, record, intent)
                return
            # Keep WAL/shm present while preparation uses a read-only bind.
            # SQLite otherwise removes them at the last close and a subsequent
            # read-only process cannot recreate its shared-memory index.
            self._claim_reader=sqlite3.connect((self.root/'controller.sqlite').as_uri()+'?mode=rw',uri=True)
            self._claim_reader.execute('PRAGMA query_only=ON')
            self._claim_reader.execute('SELECT epoch FROM controller_lifecycle').fetchall()
            if claim['kind'] in ('build','compose'):
                cache=self.root/'intermediate-cache'
                if cache.resolve()!=cache or cache.is_symlink():
                    raise WorkerServiceError('worker cache path is linked')
                cache.mkdir(mode=0o700,exist_ok=True)
            runtime=path.parent/unit
            runtime.mkdir(mode=0o700)
            capture_runtime(runtime/'code',excluded=private_inputs(self.root))
            self._start(record,'prepare')
        except BaseException as exc:
            raise WorkerServiceError(str(exc),possibly_started=True) from exc

    def advance(self, claim, root):
        self.root=Path(root)
        record=self._load(claim['worker_unit'])
        if record['complete']: return
        self._current(record)
        execution=record['executions'][-1]
        value=self._inspect(record,execution)
        if value.get('State',{}).get('Running'): return
        value=self._stop(record,execution)
        self._capture_log(record,execution)
        from .worker_container_plan import completed
        following=completed(self,record,value['State'].get('ExitCode'))
        if following:
            self._start(record,following)
        else:
            record['complete']=True
            self._save(record)

    def _capture_log(self, record, execution):
        log=Path(record['claim']['stage_dir'])/'diagnostics'/(execution['phase']+'.log')
        if log.parent.resolve()!=log.parent:
            raise WorkerServiceError('worker diagnostics path is linked')
        log.parent.mkdir(mode=0o700,exist_ok=True)
        if execution.get('log_sha256'):
            if digest(read_file(log.parent,log.name,limit=8*1024**2))!=execution['log_sha256']:
                raise WorkerServiceError('retained worker diagnostics changed')
            return
        attempt=log.with_name(log.stem+'-'+uuid.uuid4().hex+'.log')
        result=self.backend.stream('logs',execution.get('id',execution['name']),attempt,
            deadline=self.clock()+60,max_duration=60)
        if result['exit_code']!=0:
            raise WorkerServiceError('worker log retrieval failed; stopped container and attempt log retained')
        value=digest(read_file(attempt.parent,attempt.name,limit=8*1024**2))
        os.replace(attempt,log)
        execution['log_sha256']=value;self._save(record)

    def finished(self, unit, boot_id):
        if validate_boot_id(boot_id)!=validate_boot_id(self.boot_id_reader()):
            raise WorkerServiceError('worker boot changed; reconcile before consuming output')
        record=self._load(unit)
        if record.get('bootstrap'):
            from .retention import stop_proof
            try: stop_proof(Path(record['claim']['stage_dir']))
            except (OSError, ValueError) as exc:
                raise WorkerServiceError('bootstrap subprocess stop remains unresolved') from exc
        return record['complete']

    def stop_and_verify(self, unit, recorded_boot_id):
        recorded=validate_boot_id(recorded_boot_id)
        if LEGACY_UNIT.fullmatch(unit or ''):
            if recorded!=validate_boot_id(self.boot_id_reader()): return 'previous_boot'
            raise WorkerServiceError('legacy worker must be stopped using its original installation, or reconciled after host reboot; no container stop proof exists')
        record=self._load(unit)
        if record.get('bootstrap'):
            from .retention import stop_proof
            try: stop_proof(Path(record['claim']['stage_dir']))
            except (OSError, ValueError) as exc:
                raise WorkerServiceError('bootstrap subprocess stop remains unresolved') from exc
        for execution in record['executions']:
            if execution.get('removed'): continue
            if not execution.get('start_requested',True) or execution.get('stopped'):
                # A successful engine listing distinguishes absence from an
                # unavailable manager. Without a start request, an ambiguous
                # create can leave only an unstarted container. After a proven
                # stop it also reconciles lost remove acknowledgements.
                found=self._invoke('ps','--all','--no-trunc','--filter',
                                   'name=^'+execution['name']+'$','--format','{{.ID}}').strip()
                if not found:
                    execution['removed']=True;self._save(record)
                    continue
            value=self._stop(record,execution)
            execution['stopped']=True;self._save(record)
            self._capture_log(record,execution)
            self.backend.remove(value.get('Id',value.get('ID')))
            execution['removed']=True;self._save(record)
        record['stopped']=True;self._save(record)
        runtime=self._path(record['unit']).parent/record['unit']
        if runtime.exists():
            if runtime.resolve()!=runtime or runtime.is_symlink():
                raise WorkerServiceError('stopped worker runtime path is linked')
            import shutil
            shutil.rmtree(runtime)
        if self._claim_reader is not None:
            self._claim_reader.close();self._claim_reader=None
        return 'stopped'
