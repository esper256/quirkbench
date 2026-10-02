"""Explicit archived drain under completed retarget, with original attribution.

This exception permits only the existing scoped upload/evidence paths. The new
identity and journal stay fixed; no one-shot, watchdog or execution operation runs.
"""
from contextlib import contextmanager
from pathlib import Path
import os
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _private_path
from .controller_tls import _read
from .enrollment import _document
from .enrollment_target import _storage
from .evidence_drain_client import HTTPSDrainClient
from .evidence_drain_target import _journal_at,_journal_digest,_source,_export_locked,_drain_locked
from .maintenance import private_lock
from .retarget_activation import completed,_records,_new_view
from .retarget_enrollment import _same_source_scope
from .retarget_local import pending_intent,_location,validate_intent,_capture_source,_history,_private_journal
from .release_http import _remaining
from .state_reader import read_file


def _source_identity(source,snapshot):
    """The reader must describe the immutable archive, even after a coherent swap."""
    files=source['files']
    generations=[Path(name).parts[1] for name in files
        if len(Path(name).parts)==3 and Path(name).parts[0]=='generations' and Path(name).name=='generation.json']
    if len(generations)!=1:raise Conflict('archived source must pin one exact original generation')
    generation=sha256(generations[0]);pending=snapshot['pending']
    facts={'request_sha256':files['enrollment/pending/request.json'],
        'result_sha256':files['enrollment/pending/result.json'],'runtime_sha256':files['runtime.json'],
        'generation_sha256':generation,'attempt_id':pending['attempt_id'],'boot_id':pending['boot_id'],
        'attempt_token_sha256':digest(pending['token'].encode()),'journal_identity_sha256':_journal_digest(snapshot,None)}
    return digest(canonical(facts))


@contextmanager
def _archived(control,config,retarget_id,verify_target,binding_reader,recovery_verifier,clock,deadline, *,operation='archived evidence export'):
    from .boot import _verify_state_identity
    def remaining():return _remaining(deadline,clock,operation=operation)
    identifier(retarget_id);remaining()
    recover=recovery_verifier or (lambda config:_verify_state_identity(config,Path('/boot/quirkbench-state')))
    recover(config);control,storage=_storage(control,verify_target)
    with private_lock(control/'runtime-config.lock') as config_fd:
        directory=_location(control,retarget_id);intent=validate_intent(_document(_read(directory,'intent.json')))
        if digest(canonical(config.to_dict()))!=intent['boot_config_sha256']:raise Conflict('archived drain boot identity differs from retarget')
        # Current head, not historical hardware, gates all credential access.
        pointer=_read(control/'retarget','active.json');public=_document(pointer)
        if public.get('schema_version')!=2:raise Conflict('completed retarget selection required for archived evidence')
        history=_history(control,pointer)
        selected=history.get(directory.name)
        if selected is None or selected[0]['schema_version']!=2:
            raise Conflict('exact completed historical retarget required for archived evidence')
        head_id=public['request_id'];head=history[digest(head_id.encode())][1]
        verify_binding(head['new_target_binding'],reader=binding_reader)
        if pending_intent(control,binding_reader=binding_reader) is not None:raise Conflict('retarget remains paused; archived drain unavailable')
        completed(control,head_id,binding_reader=binding_reader)
        new_agent=_private_path(control/'agent');archive=_private_path(directory/'archive');old_agent=_private_path(archive/'agent')
        if not new_agent.is_dir() or not old_agent.is_dir():raise Conflict('exact current and original spools must exist')
        with private_lock(new_agent/'agent.lock') as new_fd,private_lock(old_agent/'agent.lock') as old_fd:
            activation,source=_records(control,directory,intent)
            new_journal=read_file(new_agent,'journal.json',limit=4*1024**2)
            retained={name:_read(directory,name) for name in ('intent.json','source.json','activation.json','completion.json')}
            runtime=_read(control,'runtime.json')
            def basic():
                remaining();storage();recover(config)
                verify_binding(head['new_target_binding'],reader=binding_reader)
                for path,fd in ((control/'runtime-config.lock',config_fd),(new_agent/'agent.lock',new_fd),(old_agent/'agent.lock',old_fd)):
                    held=os.fstat(fd);named=path.lstat()
                    if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('archived drain lock ownership changed')
                if (_read(control/'retarget','active.json')!=pointer or _read(control,'runtime.json')!=runtime
                        or any(_read(directory,name)!=raw for name,raw in retained.items())
                        or read_file(new_agent,'journal.json',limit=4*1024**2)!=new_journal):
                    raise Conflict('current retarget authority or new target work changed during archived drain')
                remaining()
            basic()
            locations=(archive,archive/'enrollment-pending',old_agent)
            # The immutable raw snapshot pins attribution; only ACK/offset fields
            # can differ in the mutable original journal after scoped drains.
            snapshot_raw=_private_journal(archive,name='journal.initial.json')
            if digest(snapshot_raw)!=source['files']['agent/journal.json']:raise Conflict('original retarget journal snapshot changed')
            snapshot=_journal_at(archive,'journal.initial.json');frozen=_journal_digest(snapshot,None)
            expected_identity=_source_identity(source,snapshot)
            def full():
                basic()
                if read_file(archive,'journal.initial.json',limit=4*1024**2)!=snapshot_raw:
                    raise Conflict('immutable original attribution snapshot changed')
                current=_journal_at(old_agent)
                if _journal_digest(current,None)!=frozen:raise Conflict('archived original attribution changed beyond acknowledgment progress')
                captured=_capture_source(control,intent,basic,locations=locations)
                captured['files']['agent/journal.json']=digest(snapshot_raw)
                if captured!=source:raise Conflict('static archived enrollment or original generation changed')
                from .retarget_endpoint import original as original_endpoint
                original=original_endpoint(control,intent,locations=locations)
                _,result,_,_=_new_view(directory,intent,activation)
                _same_source_scope(result,original,intent)
                # Reconstruct history/completion once per full boundary, after
                # the last native callback. The live head UUID was checked by
                # basic in this pass; these final readers are deliberately pure.
                if _history(control,pointer)!=history:raise Conflict('archived retarget history changed')
                completed(control,head_id,binding_reader=lambda:head['new_target_binding']['system_uuid'])
                if _private_journal(archive,name='journal.initial.json')!=snapshot_raw:
                    raise Conflict('immutable original attribution snapshot changed during capture')
                if _journal_digest(_journal_at(old_agent),None)!=frozen:
                    raise Conflict('archived original attribution changed during capture')
                remaining()
            full()
            def source_reader(check):
                # Per-file checks keep live binding/ownership/deadline fences;
                # full semantic reconstruction brackets the whole source read.
                check();selected=None
                if intent['schema_version']==3:
                    from .retarget_endpoint import files as endpoint_files
                    selected=endpoint_files(control,intent,locations=locations)
                value=_source(control,basic,locations=locations,_endpoint_files=selected)
                check()
                if value[3]!=expected_identity:raise Conflict('archived drain reader differs from exact retained source identity')
                return value
            yield control,full,source_reader,old_agent
            full();remaining()


def export_archived_plan(control,config,retarget_id,request_id, *,verify_target,
                         binding_reader=read_system_uuid,recovery_verifier=None,fault_hook=None,clock=time.monotonic):
    deadline=clock()+120
    with _archived(control,config,retarget_id,verify_target,binding_reader,recovery_verifier,clock,deadline,operation='archived evidence export') as (control,verify,reader,agent):
        return _export_locked(control,verify,request_id,fault_hook=fault_hook,clock=clock,source_reader=reader,agent_root=agent,deadline=deadline)


def drain_archived(control,config,retarget_id,request_id,grant_id, *,verify_target,
                   binding_reader=read_system_uuid,recovery_verifier=None,client_factory=HTTPSDrainClient,
                   timeout_s=120,fault_hook=None,clock=time.monotonic):
    if type(timeout_s) not in (int,float) or not 0<timeout_s<=120:raise ContractError('archived drain window must be 0 to 120 seconds')
    deadline=clock()+timeout_s
    with _archived(control,config,retarget_id,verify_target,binding_reader,recovery_verifier,clock,deadline,operation='archived evidence drain') as (control,verify,reader,agent):
        return _drain_locked(control,verify,request_id,grant_id,client_factory=client_factory,timeout_s=timeout_s,
            clock=clock,fault_hook=fault_hook,source_reader=reader,agent_root=agent,deadline=deadline)
