"""Explicit recovery-only draining of retained evidence under original attribution.

This adapter never invokes the agent step loop, registers hardware, clears pending
work, activates networking/configuration, or completes/returns a physical attempt.
"""
from contextlib import contextmanager
from itertools import islice
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import time

from .contracts import CapabilityReport,Conflict,ContractError,canonical,digest,identifier,sha256
from .binding import read_system_uuid,verify_binding
from .filesystem import _durable_directory, _managed_path
from .filesystem import _read
from .enrollment_records import _document
from .enrollment_crypto import validate_request
from .enrollment_result import validate_result
from .enrollment_target import _media,_storage
from .evidence_drain_records import load, read_credential, validate_plan
from .evidence_drain_client import HTTPSDrainClient
from .filesystem import private_lock
from .filesystem import read_file
from .store import atomic_write
from .target import TargetAgent,read_sealed_evidence

MAX_JOURNAL=4*1024**2
MAX_PLANS=32


def validate_source(saved):
    fields={'schema_version','record_type','request_id','source_sha256','ca_sha256','journal_sha256','plan','pending_records_at_capture'}
    if (not isinstance(saved,dict) or set(saved)!=fields or type(saved['schema_version']) is not int
            or saved['schema_version']!=1 or saved['record_type']!='old-evidence-drain-source'):
        raise ContractError('invalid retained original drain source')
    identifier(saved['request_id'])
    for key in ('source_sha256','ca_sha256','journal_sha256'):sha256(saved[key])
    validate_plan(saved['plan'])
    if type(saved['pending_records_at_capture']) is not int or not len(saved['plan']['evidence'])<=saved['pending_records_at_capture']<=8192:
        raise ContractError('invalid original pending evidence count')
    load(canonical(saved))
    return saved


def _journal(control):
    return _journal_at(control/'agent')


def _journal_at(agent,name='journal.json'):
    from .product_contracts import _depth,_pairs
    agent=_managed_path(agent);path=agent/name;before=path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_nlink!=1:
        raise ContractError('original journal must remain an owned regular file')
    raw=read_file(agent,name,limit=MAX_JOURNAL)
    after=path.lstat()
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise Conflict('original evidence journal changed during capture')
    try:
        journal=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite retained journal')))
        _depth(journal)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as exc:raise ContractError('invalid retained evidence journal') from exc
    if (not isinstance(journal,dict) or set(journal)!={'schema_version','device_id','pending','claim_request_id'}
            or type(journal['schema_version']) is not int or journal['schema_version']!=1
            or journal['claim_request_id'] is not None or not isinstance(journal['pending'],dict)
            or raw!=canonical(journal)):
        raise Conflict('recognized pending original evidence journal required; no new claims may be pending')
    pending=journal['pending'];identifier(journal['device_id'])
    for key in ('attempt_id','boot_id'):identifier(pending.get(key))
    token=pending.get('token')
    if not isinstance(token,str) or not 32<=len(token)<=512 or not token.isascii():raise ContractError('original attempt token unavailable')
    records=pending.get('evidence')
    if not isinstance(records,list) or not 1<=len(records)<=8192:raise Conflict('no bounded retained evidence to drain')
    for index,item in enumerate(records):
        if not isinstance(item,dict) or set(item)!={'stream','sequence','sha256','size','uploaded_offset','evidence_acked'}:
            raise ContractError('invalid retained evidence descriptor')
        identifier(item['stream']);sha256(item['sha256'])
        if (type(item['sequence']) is not int or item['sequence']!=index or type(item['size']) is not int
                or not 0<=item['size']<=128*1024**2 or type(item['uploaded_offset']) is not int
                or not 0<=item['uploaded_offset']<=item['size'] or type(item['evidence_acked']) is not bool):
            raise ContractError('invalid retained evidence identity/progress')
    return journal


def _source(control,verify, *,locations=None,_endpoint_files=None):
    """Capture exact original enrollment/configuration; do not authenticate as it."""
    verify();runtime_home,pending,agent=locations or (control,control/'enrollment/pending',control/'agent')
    pending=_managed_path(pending)
    def read(directory,name):verify();return _read(directory,name)
    req_raw=read(pending,'request.json');request=validate_request(_document(req_raw))
    result_raw=read(pending,'result.json');result=validate_result(_document(result_raw),request)
    _media(control,result['media_instance_id'])
    active_raw=read(runtime_home,'runtime.json');active=_document(active_raw)
    if not isinstance(active,dict):raise ContractError('original active configuration must be an object')
    ca=active.get('ca')
    if (not isinstance(ca,str) or len(Path(ca).parts)!=3 or Path(ca).parts[0]!='generations' or Path(ca).parts[2]!='ca.pem'):
        raise Conflict('exact initial enrolled private generation required for original evidence drain')
    generation=sha256(Path(ca).parts[1]);directory=_managed_path(control/'generations'/generation)
    manifest_raw=read(directory,'generation.json');manifest=_document(manifest_raw)
    if not isinstance(manifest,dict) or digest(manifest_raw)!=generation:raise Conflict('original private generation identity differs')
    runtime={'schema_version':1,'device_id':result['device_id'],'controller_url':result['controller_url'],
        'ca':'ca.pem','token_file':'device.token','target_binding':result['target_binding'],'remotes':{}}
    files={'ca.pem':result['controller_ca_pem'].encode(),'device.token':result['device_token'].encode(),
        'repository.crt':result['repository_certificate_pem'].encode(),'repository.key':read(pending,'key.pem')}
    for alias,remote in result['repository_remotes'].items():
        name=alias+'.public.asc';files[name]=remote['public_key'].encode()
        runtime['remotes'][alias]={'url':remote['url'],'ca':'ca.pem','public_key':name,'client_cert':'repository.crt','client_key':'repository.key'}
    files['runtime.json']=canonical(runtime)
    if _endpoint_files is None and locations is None and (control/'endpoint/active.json').exists():
        from .endpoint_history import history
        from .endpoint_activation import selected_files
        selected_raw=read(control/'endpoint','active.json');history(control,selected_raw)
        _endpoint_files=selected_files(control,selected_raw)
    if _endpoint_files is not None:
        files=_endpoint_files;runtime=_document(files['runtime.json'])
        from .retarget_endpoint import project
        result=project(result,files)
    if (manifest!={name:digest(data) for name,data in sorted(files.items())}
            or set(path.name for path in islice(directory.iterdir(),len(files)+2))!=set(files)|{'generation.json'}
            or any(read(directory,name)!=raw for name,raw in files.items())):
        raise Conflict('retained original private generation bytes differ from enrollment')
    expected=json.loads(canonical(runtime));prefix='generations/'+generation+'/'
    for key in ('ca','token_file'):expected[key]=prefix+expected[key]
    for remote in expected['remotes'].values():
        for key in ('ca','public_key','client_cert','client_key'):remote[key]=prefix+remote[key]
    if canonical(expected)!=active_raw:raise Conflict('active configuration differs from original enrolled generation')
    verify();journal=_journal_at(agent)
    if journal['device_id']!=result['device_id']:raise Conflict('retained journal belongs to another original target')
    facts={'request_sha256':digest(req_raw),'result_sha256':digest(result_raw),'runtime_sha256':digest(active_raw),
        'generation_sha256':generation,'attempt_id':journal['pending']['attempt_id'],
        'boot_id':journal['pending']['boot_id'],'attempt_token_sha256':digest(journal['pending']['token'].encode()),
        'journal_identity_sha256':_journal_digest(journal,None)}
    verify()
    return result,journal,files['ca.pem'],digest(canonical(facts))


@contextmanager
def _locked(control,verify_target,binding_reader):
    control,verify=_storage(control,verify_target)
    agent=_managed_path(control/'agent')
    if not agent.is_dir():raise Conflict('original target spool unavailable')
    with private_lock(control/'runtime-config.lock'):
        with private_lock(agent/'agent.lock'):
            from .shutdown_local import require_available
            require_available(control)
            verify()
            from .endpoint_local import require_available
            require_available(control,binding_reader=binding_reader)
            request_raw=_read(control/'enrollment/pending','request.json')
            request=validate_request(_document(request_raw));base=verify
            def verify():
                base();verify_binding(request['target_binding'],reader=binding_reader)
                if _read(control/'enrollment/pending','request.json')!=request_raw:
                    raise Conflict('original enrollment request changed during evidence maintenance')
            verify();yield control,verify


def _journal_digest(journal,scope):
    frozen=json.loads(canonical(journal))
    for item in frozen['pending']['evidence']:
        if scope is None or tuple(item[key] for key in ('stream','sequence','sha256','size')) in scope:
            item.pop('uploaded_offset');item.pop('evidence_acked')
    return digest(canonical(frozen))


def _selected(plan,journal,result):
    validate_plan(plan);pending=journal['pending']
    if (plan['device_id']!=result['device_id'] or plan['generation']!=result['credential_generation']['generation']
            or plan['media_instance_id']!=result['media_instance_id'] or canonical(plan['target_binding'])!=canonical(result['target_binding'])
            or plan['attempt_id']!=pending['attempt_id'] or plan['boot_id']!=pending['boot_id']):
        raise Conflict('drain plan differs from original enrolled spool identity')
    available={tuple(item[key] for key in ('stream','sequence','sha256','size')) for item in pending['evidence']}
    selected=frozenset(tuple(item[key] for key in ('stream','sequence','sha256','size')) for item in plan['evidence'])
    if not selected<=available:raise Conflict('drain plan differs from retained original evidence')
    return selected


def _prepare(control,request_id,verify,fault,deadline,clock, *,source_reader=None,agent_root=None):
    source_reader=source_reader or (lambda check:_source(control,check));agent_root=agent_root or control/'agent'
    original=verify
    def verify():
        if clock()>=deadline:raise TimeoutError('bounded original evidence capture window elapsed')
        original()
        if clock()>=deadline:raise TimeoutError('bounded original evidence capture window elapsed')
    identifier(request_id);result,journal,ca,source=source_reader(verify)
    plans=_managed_path(control/'evidence-drain/plans');directory=_managed_path(plans/digest(request_id.encode()))
    if plans.exists() and len(list(islice(plans.iterdir(),MAX_PLANS)))>=MAX_PLANS and not directory.exists():raise Conflict('retained drain plan limit reached; preserve/export history before further maintenance')
    marker=directory/'source.json'
    if marker.exists() or marker.is_symlink():
        saved=validate_source(load(_read(directory,marker.name)))
        if saved['request_id']!=request_id or saved['source_sha256']!=source or saved['ca_sha256']!=digest(ca):
            raise Conflict('retained drain source differs from exact request/identity')
        plan=saved['plan'];scope=_selected(plan,journal,result)
        if saved['journal_sha256']!=_journal_digest(journal,scope):raise Conflict('original result or unselected evidence progress changed')
    else:
        if directory.exists() and any(directory.iterdir()):raise Conflict('unidentified drain plan files; original source remains retained')
        records=[item for item in journal['pending']['evidence'] if not item['evidence_acked']]
        selected=[];total=0
        for item in records[:128]:
            if total+item['size']>1024**3:break
            selected.append({key:item[key] for key in ('stream','sequence','sha256','size')});total+=item['size']
        if not selected:raise Conflict('no unacknowledged original evidence; reuse the previous exact plan to repair a lost controller acknowledgment')
        plan=validate_plan({'schema_version':1,'record_type':'old-evidence-drain-plan','device_id':result['device_id'],
            'generation':result['credential_generation']['generation'],'attempt_id':journal['pending']['attempt_id'],
            'boot_id':journal['pending']['boot_id'],'media_instance_id':result['media_instance_id'],
            'target_binding':result['target_binding'],'evidence':selected})
        saved={'schema_version':1,'record_type':'old-evidence-drain-source','request_id':request_id,'source_sha256':source,
            'ca_sha256':digest(ca),'plan':plan,'pending_records_at_capture':len(records),
            'journal_sha256':_journal_digest(journal,_selected(plan,journal,result))}
        validate_source(saved)
        verify();_durable_directory(directory);atomic_write(marker,canonical(saved))
    fault('drain_source_retained')
    for item in plan['evidence']:
        read_sealed_evidence(agent_root,item['sha256'],item['size'],verify=verify,deadline=deadline,clock=clock,collect=False)
    for name,raw in (('plan.json',canonical(plan)),('ca.pem',ca)):
        path=directory/name;verify()
        if path.exists() or path.is_symlink():
            if _read(directory,name)!=raw:raise Conflict('immutable drain plan/trust bytes changed')
        else:atomic_write(path,raw)
    fault('drain_plan_retained')
    latest,current,latest_ca,latest_source=source_reader(verify)
    scope=_selected(plan,current,latest)
    if latest_source!=source or latest_ca!=ca or saved['journal_sha256']!=_journal_digest(current,scope):
        raise Conflict('original drain source changed during capture')
    return saved,directory,result,journal


def _export_locked(control,verify,request_id, *,fault_hook=None,clock=time.monotonic,source_reader=None,agent_root=None,deadline=None):
    deadline=clock()+120 if deadline is None else deadline
    saved,directory,_,_=_prepare(control,request_id,verify,fault_hook or (lambda _:None),deadline,clock,
                               source_reader=source_reader,agent_root=agent_root)
    return {'request_id':request_id,'plan':saved['plan'],'plan_file':str(directory/'plan.json'),
        'pending_records_at_capture':saved['pending_records_at_capture'],'selected_records':len(saved['plan']['evidence']),
        'additional_records_at_capture':saved['pending_records_at_capture']-len(saved['plan']['evidence']),
        'boot_authorized':False,'attempt_completed':False}


def export_plan(control,request_id, *,verify_target,binding_reader=read_system_uuid,fault_hook=None,clock=time.monotonic):
    deadline=clock()+120
    with _locked(control,verify_target,binding_reader) as (control,verify):
        return _export_locked(control,verify,request_id,fault_hook=fault_hook,clock=clock,deadline=deadline)


def _drain_locked(control,verify,request_id,grant_id, *,client_factory=HTTPSDrainClient,
                  timeout_s=120,clock=time.monotonic,fault_hook=None,source_reader=None,agent_root=None,deadline=None):
    # Private adapters must call check() before any source read and after capture,
    # before returning, propagating failures. Both _source and the archived reader
    # do this; their entry check also gates every network request/journal write.
    source_reader=source_reader or (lambda check:_source(control,check));agent_root=agent_root or control/'agent'
    identifier(grant_id)
    if type(timeout_s) not in (int,float) or not 0<timeout_s<=120:raise ContractError('old-evidence drain window must be 0 to 120 seconds')
    fault=fault_hook or (lambda _:None)
    deadline=clock()+timeout_s if deadline is None else deadline
    saved,directory,result,journal=_prepare(control,request_id,verify,fault,deadline,clock,source_reader=source_reader,agent_root=agent_root)
    staged=_managed_path(control/'setup')/(grant_id+'.json');verify()
    credential=read_credential(staged,stores=(control,control.parent))
    if credential['record']['grant_id']!=grant_id or canonical(credential['record']['plan'])!=canonical(saved['plan']):
        raise Conflict('staged grant differs from exact original evidence plan')
    verify();client=client_factory(result['controller_url'],{'record':credential['record'],'token':credential['token']},str(directory/'ca.pem'))
    fault('drain_client_prepared')
    latest,current,ca,source=source_reader(verify)
    if source!=saved['source_sha256'] or digest(ca)!=saved['ca_sha256'] or read_credential(staged,stores=(control,control.parent))!=credential:
        raise Conflict('original source or staged drain credential changed before exchange')
    scope=_selected(saved['plan'],current,latest)
    if isinstance(client,HTTPSDrainClient):
        client._absolute_deadline=deadline;client._monotonic=clock
    def live_verify():
        if clock()>=deadline:raise TimeoutError('bounded old-evidence drain window elapsed')
        latest,current,ca,source=source_reader(verify)
        if (source!=saved['source_sha256'] or digest(ca)!=saved['ca_sha256']
                or _journal_digest(current,scope)!=saved['journal_sha256']
                or _read(directory,'ca.pem')!=ca or _read(directory,'plan.json')!=canonical(saved['plan'])
                or read_credential(staged,stores=(control,control.parent))!=credential):raise Conflict('original drain source changed before request or journal write')
        if clock()>=deadline:raise TimeoutError('bounded old-evidence drain window elapsed')
    class GuardedClient:
        device_id=client.device_id
        def upload(self,*args):
            live_verify();client.timeout=min(15,deadline-clock())
            if client.timeout<=0:raise TimeoutError('bounded old-evidence request window elapsed')
            return client.upload(*args)
        def evidence(self,*args):
            live_verify();client.timeout=min(15,deadline-clock())
            if client.timeout<=0:raise TimeoutError('bounded old-evidence request window elapsed')
            return client.evidence(*args)
    # The historical report is only constructor plumbing for the existing
    # spool writer. No report/register/step/recipe/physical operation is called.
    report=CapabilityReport(saved['plan']['device_id'],saved['plan']['boot_id'],[],mode='recovery')
    agent=TargetAgent(GuardedClient(),agent_root,report,verify_storage=live_verify,recovery_only=True)
    if canonical(agent._journal)!=canonical(current):raise Conflict('original journal changed before scoped drain')
    agent._drain(agent._journal['pending'],finish=False,deadline=deadline,evidence_scope=scope,clock=clock)
    fault('drain_evidence_acknowledged');verify()
    latest,current,ca,source=source_reader(verify)
    if source!=saved['source_sha256'] or digest(ca)!=saved['ca_sha256'] or _journal_digest(current,scope)!=saved['journal_sha256']:
        raise Conflict('original source changed during evidence drain')
    _selected(saved['plan'],current,latest)
    return {'request_id':request_id,'grant_id':grant_id,'attempt_id':saved['plan']['attempt_id'],
        'acknowledged_records':len(scope),'attempt_completed':False,'boot_authorized':False,'physical_shutdown_verified':False}


def drain_original(control,request_id,grant_id, *,verify_target,client_factory=HTTPSDrainClient,
                   binding_reader=read_system_uuid,timeout_s=120,clock=time.monotonic,fault_hook=None):
    """A staged exact grant uses original trust and only modifies original ACK progress."""
    identifier(grant_id)
    if type(timeout_s) not in (int,float) or not 0<timeout_s<=120:raise ContractError('old-evidence drain window must be 0 to 120 seconds')
    deadline=clock()+timeout_s
    with _locked(control,verify_target,binding_reader) as (control,verify):
        return _drain_locked(control,verify,request_id,grant_id,client_factory=client_factory,
                             timeout_s=timeout_s,clock=clock,fault_hook=fault_hook,deadline=deadline)


def attended_drain(*,input_stream=None,output_stream=None,control=None,verify_target=None,run=subprocess.run,
                   exporter=export_plan,drainer=drain_original,config=None,archived_exporter=None,archived_drainer=None):
    from .enrollment_console import _answer
    from .runtime import CONTROL,boot_context
    source=input_stream or sys.stdin;output=output_stream or sys.stdout;control=control or CONTROL
    if verify_target is None:
        config,boot,verify_target=boot_context()
        if boot['quirkbench.mode']!='recovery':raise ContractError('old-evidence drain requires verified recovery')
    control,verify_target=_storage(control,verify_target)
    stopped=None
    try:
        stopped=run(['systemctl','stop','quirkbench-supervisor.service'],check=False,capture_output=True,timeout=45)
        if stopped.returncode!=0:raise Conflict('target supervisor could not stop; old-evidence drain blocked')
        choice=_answer(source,output,'Type plan REQUEST_ID, drain REQUEST_ID GRANT_ID, archived-plan RETARGET_ID REQUEST_ID or archived-drain RETARGET_ID REQUEST_ID GRANT_ID (empty cancels): ',450)
        if choice is None or not choice:return None
        parts=choice.split()
        if parts[0] in ('archived-plan','archived-drain'):
            if config is None:raise ContractError('archived evidence requires exact verified recovery configuration')
            from .retarget_evidence import export_archived_plan,drain_archived
            if len(parts)==3 and parts[0]=='archived-plan':
                answer=(archived_exporter or export_archived_plan)(control,config,parts[1],parts[2],verify_target=verify_target)
                print('Original archived evidence plan: '+answer['plan_file'],file=output)
                print('Approve this exact original plan with target evidence approve and stage setup/GRANT_ID.json privately. Use archived-drain with the same retarget and plan IDs.',file=output)
                return answer
            if len(parts)==4 and parts[0]=='archived-drain':
                answer=(archived_drainer or drain_archived)(control,config,parts[1],parts[2],parts[3],verify_target=verify_target)
                print('Original archived evidence acknowledged: '+str(answer['acknowledged_records'])+' records. New target work and one-shot state are unchanged.',file=output)
                return answer
            raise ContractError('choose explicit archived retarget, plan and staged-grant IDs')
        if len(parts)==2 and parts[0]=='plan':
            answer=exporter(control,parts[1],verify_target=verify_target)
            print('Original evidence plan: '+answer['plan_file'],file=output)
            print('Selected '+str(answer['selected_records'])+' records; '+str(answer['additional_records_at_capture'])+' additional records remain.',file=output)
            print('Approve this exact plan on the controller with target evidence approve. Stage its private credential as setup/GRANT_ID.json on this verified media, then use drain with the same request ID.',file=output)
            return answer
        if len(parts)==3 and parts[0]=='drain':
            answer=drainer(control,parts[1],parts[2],verify_target=verify_target)
            print('Original evidence acknowledged: '+str(answer['acknowledged_records'])+' records. Pending attempt/result and original attribution remain retained.',file=output)
            return answer
        raise ContractError('choose an explicit plan or staged-grant drain request')
    finally:
        if stopped is None or stopped.returncode==0:
            restarted=run(['systemctl','start','quirkbench-supervisor.service'],check=False,capture_output=True,timeout=120)
            if restarted.returncode!=0:raise Conflict('target supervisor restart failed; original evidence remains retained')
