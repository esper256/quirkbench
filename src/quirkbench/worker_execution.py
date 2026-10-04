"""Bounded, versioned container execution journal; no publication authority."""
import json
import math
from pathlib import Path
import re

from .contracts import ContractError, canonical, sha256
from .product_contracts import _pairs, _depth

UNIT = re.compile(r'qb-worker-v2-[0-9a-f]{32}-[1-9][0-9]*\Z')
CLAIM_FIELDS = ('id', 'kind', 'stage', 'worker_epoch', 'worker_generation',
                'worker_unit', 'worker_boot_id', 'stage_dir', 'input_digest', 'deadline')
PHASES = ('reserved', 'prepare', 'build', 'compose', 'candidate', 'recovery', 'builder-marker',
          'distribution', 'distribution-import')
LIMIT = 65536


def validate(record, root):
    required = {'schema_version', 'unit', 'engine', 'engine_identity', 'claim',
                'worker_image', 'reserve_bytes', 'payload_args', 'executions', 'phase', 'complete', 'monotonic_deadline'}
    if (not isinstance(record, dict) or not required <= set(record)
            or set(record)-required-{'stopped','bootstrap','cgroup_manager'} or type(record['schema_version']) is not int
            or record['schema_version'] not in (2,3,4)):
        raise ContractError('invalid container execution journal')
    bootstrap = record.get('bootstrap',False)
    if (record['schema_version']==2 and 'bootstrap' in record) or (record['schema_version']==3 and not bootstrap) or ('bootstrap' in record and record['bootstrap'] is not True):
        raise ContractError('invalid bootstrap journal version')
    if record['schema_version']==4:
        if record['engine']!='podman' or record.get('cgroup_manager') not in ('systemd','cgroupfs'):
            raise ContractError('invalid recorded Podman manager')
    elif 'cgroup_manager' in record:raise ContractError('manager requires versioned execution journal')
    if (not isinstance(record['unit'],str) or not UNIT.fullmatch(record['unit'])
            or record['engine'] not in ('docker','podman')
            or not isinstance(record['engine_identity'],str)
            or not 1 <= len(record['engine_identity']) <= 4096
            or record['phase'] not in PHASES):
        raise ContractError('invalid execution backend or phase')
    def image(value):
        if not isinstance(value,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',value):
            raise ContractError('worker image must be an immutable local identity')
    image(record['worker_image'])
    if type(record['monotonic_deadline']) not in (int,float) or not math.isfinite(record['monotonic_deadline']):
        raise ContractError('invalid execution elapsed deadline')
    for flag in ('complete','stopped'):
        if flag in record and type(record[flag]) is not bool:
            raise ContractError('invalid worker execution flag')
    if type(record['reserve_bytes']) is not int or not 0 <= record['reserve_bytes'] <= 1048576*1024**3:
        raise ContractError('invalid worker storage reserve')
    args=record['payload_args']
    if not isinstance(args,dict) or set(args)-{'recipe_sha256'}:
        raise ContractError('invalid fixed payload parameters')
    if args: sha256(args['recipe_sha256'])
    claim=record['claim']
    if not isinstance(claim,dict) or set(claim)!=set(CLAIM_FIELDS):
        raise ContractError('invalid recorded worker claim')
    if bootstrap and (claim['kind'] != 'builder_prepare' or claim['stage'] not in ('builder_capture','builder_import')):
        raise ContractError('bootstrap is restricted to signed builder preparation')
    if not isinstance(claim['id'],str) or not re.fullmatch('[0-9a-f]{32}',claim['id']):
        raise ContractError('invalid worker operation')
    for field in ('worker_epoch','worker_generation'):
        if type(claim[field]) is not int or claim[field]<1: raise ContractError('invalid claim generation')
    from .process_identity import validate_boot_id
    from .job_operations import STAGES
    validate_boot_id(claim['worker_boot_id']);sha256(claim['input_digest'])
    if ((claim['kind'],claim['stage']) not in STAGES
            or type(claim['deadline']) not in (int,float) or not math.isfinite(claim['deadline'])
            or record['unit'] != f"qb-worker-v2-{claim['id']}-{claim['worker_generation']}"
            or claim['worker_unit']!=record['unit']):
        raise ContractError('execution differs from claim identity')
    stage=Path(claim['stage_dir'])
    if (not stage.is_absolute() or stage.parent!=Path(root)/'workers'/claim['id']
            or not re.fullmatch(str(claim['worker_generation'])+r'-[0-9a-f]{32}',stage.name)):
        raise ContractError('worker stage escapes its claim')
    executions=record['executions']
    if not isinstance(executions,list) or len(executions)>3:
        raise ContractError('invalid fixed execution phases')
    for execution in executions:
        required={'name','image','phase','start_requested'}
        if (not isinstance(execution,dict) or not required<=set(execution)
                or set(execution)-required-{'id','stopped','removed','log_sha256','bounds','cgroup','payload_released'}
                or not isinstance(execution['name'],str)
                or not re.fullmatch('qb-[0-9a-f]{32}',execution['name'])
                or execution['phase'] not in PHASES[1:]):
            raise ContractError('invalid container execution identity')
        image(execution['image'])
        extra={'bounds','cgroup','payload_released'} & set(execution)
        if record['schema_version']!=4 and extra:raise ContractError('containment requires versioned execution journal')
        if record['schema_version']==4:
            bounds=execution.get('bounds')
            if (not isinstance(bounds,dict) or set(bounds)!={'cpus','memory','pids'}
                    or any(type(v) is not int or v<=0 for v in bounds.values())
                    or bounds['cpus']>128 or bounds['pids']!=4096):
                raise ContractError('invalid recorded container bounds')
            if 'cgroup' in execution:
                from .container_containment import container_group
                try:container_group(execution.get('id',''),'0::'+execution['cgroup'])
                except (TypeError,ValueError,RuntimeError) as exc:raise ContractError('invalid recorded container cgroup') from exc
            if 'payload_released' in execution and (execution['payload_released'] is not True or 'cgroup' not in execution):
                raise ContractError('payload release requires recorded kernel containment')
        if 'log_sha256' in execution: sha256(execution['log_sha256'])
        if 'id' in execution and (not isinstance(execution['id'],str) or not re.fullmatch('[0-9a-f]{64}',execution['id'])):
            raise ContractError('invalid immutable container ID')
        for flag in ('start_requested','stopped','removed'):
            if flag in execution and type(execution[flag]) is not bool:
                raise ContractError('invalid container execution flag')
        if execution['start_requested'] and 'id' not in execution:
            raise ContractError('start requires recorded immutable container ID')
        if execution.get('removed') and execution['start_requested'] and not execution.get('stopped'):
            raise ContractError('removing a started execution requires verified stop')
    if executions:
        if bootstrap:
            if claim['stage'] != 'builder_import' or len(executions) != 1 or executions[0]['phase'] != 'builder-marker':
                raise ContractError('invalid bootstrap marker execution')
        elif executions[0]['phase']!='prepare':raise ContractError('execution must begin with preparation')
        if len(executions)==3 and [e['phase'] for e in executions]!=['prepare','distribution','distribution-import']:
            raise ContractError('unsupported fixed phase sequence')
        if any(e['phase']=='prepare' for e in executions[1:]):raise ContractError('preparation cannot restart in the same claim')
    if (executions and record['phase']!=executions[-1]['phase']) or (not executions and record['phase']!='reserved'):
        raise ContractError('execution phase differs from its journal')
    _depth(record)
    if len(canonical(record))>LIMIT: raise ContractError('execution journal exceeds byte limit')
    return record


def load(raw,root):
    if len(raw)>LIMIT: raise ContractError('execution journal exceeds byte limit')
    try:
        record=json.loads(raw,object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite execution JSON')))
        return validate(record,root)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as exc:
        raise ContractError('invalid execution journal JSON') from exc
