"""Owned stopped endpoint preflight; retained source only, no activation or unpause."""
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
import os
import subprocess
import time

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .controller_setup import _private_path
from .controller_endpoint import _strict_read
from .enrollment import _document
from .enrollment_proof import validate_request
from .enrollment_result import validate_result
from .enrollment_target import _storage,_media
from .endpoint_local import pending,location,_public_retarget
from .endpoint_generation import validate_transition,read_generation,verify_transition
from .endpoint_probe import probe
from .maintenance import private_lock
from .release_http import _remaining


def validate_source(value):
    if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','intent_sha256','transition','files'}
            or type(value['schema_version']) is not int or value['schema_version']!=1 or value['record_type']!='target-endpoint-source'):
        raise ContractError('invalid retained endpoint source')
    sha256(value['intent_sha256']);validate_transition(value['transition'])
    if not isinstance(value['files'],dict) or not 11<=len(value['files'])<=32:raise ContractError('endpoint source map exceeds bounded limit')
    for name,value_sha in value['files'].items():
        if not isinstance(name,str) or len(name)>256 or Path(name).is_absolute() or '..' in Path(name).parts or str(Path(name))!=name:
            raise ContractError('invalid endpoint source path')
        sha256(value_sha)
    if len(canonical(value))>65536:raise ContractError('retained endpoint source exceeds byte budget')
    return value


@contextmanager
def owned(control,config,request_id, *,verify_target,binding_reader=read_system_uuid,
          clearer=None,recovery_verifier=None,monotonic=time.monotonic,final_checks=None,_publication=None):
    """Fresh clearance before every source-secret read; retain both existing owners."""
    from .boot import clear_once,_verify_state_identity
    identifier(request_id);deadline=monotonic()+120
    recover=recovery_verifier or (lambda cfg:_verify_state_identity(cfg,Path('/boot/quirkbench-state')))
    recover(config);control,storage=_storage(control,verify_target);directory=location(control,request_id)
    agent=_private_path(control/'agent')
    if not agent.is_dir():raise Conflict('original endpoint spool required; no initialization permitted')
    with private_lock(control/'runtime-config.lock') as config_fd,private_lock(agent/'agent.lock') as agent_fd:
        intent=pending(control)
        if intent is None or intent['request_id']!=request_id or intent['boot_config_sha256']!=digest(canonical(config.to_dict())):
            raise Conflict('exact prepared endpoint/boot selection required')
        verify_binding(intent['target_binding'],reader=binding_reader);_media(control,intent['media_instance_id'])
        public={name:_strict_read(directory,name) for name in ('intent.json','source.json','approved-controller.pem','capture-completion.json')}
        source=validate_source(_document(public['source.json']));record=source['transition']
        if public['source.json']!=canonical(source):raise ContractError('endpoint source must remain canonical')
        completion=canonical({'schema_version':1,'record_type':'target-endpoint-capture','source_sha256':digest(public['source.json'])})
        if (source['intent_sha256']!=digest(public['intent.json']) or public['capture-completion.json']!=completion
                or record['request_id']!=request_id or record['source_runtime_sha256']!=intent['runtime_sha256']
                or any(record[name]!=intent[name] for name in ('controller_url','remote_urls','approved_certificate_sha256'))):
            raise Conflict('endpoint source differs from exact prepared intent/completion')
        lineage=_public_retarget(control)
        def publication():
            value=_publication() if _publication is not None else {}
            if set(value)-{'runtime_sha256','pointer','files'} or set(value.get('files',{}))-{'activation.json','completion.json'}:
                raise ContractError('invalid private endpoint publication state')
            return value
        def public_guard():
            _remaining(deadline,monotonic);storage();recover(config);verify_binding(intent['target_binding'],reader=binding_reader);_media(control,intent['media_instance_id'])
            for path,fd in ((control/'runtime-config.lock',config_fd),(agent/'agent.lock',agent_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('endpoint preflight ownership changed')
            state=publication()
            selected=(_strict_read(control/'endpoint','active.json')==state['pointer'] if state.get('pointer') is not None else pending(control)==intent)
            if not selected or _public_retarget(control)!=lineage or any(_strict_read(directory,name)!=raw for name,raw in public.items()):
                raise Conflict('endpoint prepared selection or lineage changed')
            if {path.name for path in islice(directory.parent.iterdir(),2)}!={directory.name}:raise Conflict('endpoint publication has ambiguous retained requests')
            for name in ('activation.json','completion.json'):
                expected=state.get('files',{}).get(name);path=directory/name
                if expected is None:
                    if path.exists() or path.is_symlink():raise Conflict('endpoint publication appeared without owned intent')
                elif _strict_read(directory,name)!=expected:raise Conflict('endpoint publication record changed')
            _remaining(deadline,monotonic)
        public_guard();(clearer or clear_once)(config);public_guard()
        # The public lineage above has no secret fields. Full completion is
        # deliberately deferred until fresh clearance under owned actual binding.
        if lineage[0] is not None:
            from .retarget_activation import completed
            completed(control,lineage[0],binding_reader=binding_reader);public_guard()
        request_raw=_strict_read(control/'enrollment/pending','request.json');result_raw=_strict_read(control/'enrollment/pending','result.json')
        request=validate_request(_document(request_raw));result=validate_result(_document(result_raw),request)
        if (request['target_binding']!=intent['target_binding'] or request['media_instance_id']!=intent['media_instance_id']
                or result['device_id']!=intent['device_id']):raise Conflict('endpoint source enrollment differs from actual prepared binding')
        files=read_generation(control,record['source_generation'])
        from .endpoint_generation import transition
        actual,destination=transition(files,request_id,digest(request_raw),digest(result_raw),record['controller_url'],record['remote_urls'],record['approved_certificate_sha256'])
        if actual!=record:raise Conflict('endpoint source transition differs from retained generation')
        verify_transition(record,files,destination)
        expected_names={'runtime.json','media-instance.json','agent/journal.json',
            *(str(Path('enrollment/pending')/name) for name in ('intent.json','request.json','result.json','key.pem')),
            *(str(Path('generations')/record['source_generation']/name) for name in (*files,'generation.json'))}
        if set(source['files'])!=expected_names:raise Conflict('endpoint source map differs from exact original namespace')
        blank=canonical({'schema_version':1,'device_id':intent['device_id'],'pending':None,'claim_request_id':None})
        def exact():
            public_guard()
            if _strict_read(agent,'journal.json')!=blank:raise Conflict('endpoint source work requires reconciliation')
            def matches(name,checksum):
                raw=_strict_read(control/Path(name).parent,Path(name).name)
                selected_sha=publication().get('runtime_sha256');selected=verify_transition(record,files,destination)
                if selected_sha is not None and selected_sha!=digest(selected):raise Conflict('private publication runtime differs from exact URL transition')
                return digest(raw)==checksum or (name=='runtime.json' and selected_sha is not None and raw==selected)
            if any(not matches(name,checksum) for name,checksum in source['files'].items()):
                raise Conflict('retained endpoint source bytes changed')
            if _strict_read(control/'enrollment/pending','key.pem')!=files['repository.key']:
                raise Conflict('endpoint source key differs from captured private generation')
            _remaining(deadline,monotonic)
        exact()
        names={'intent.json','source.json','approved-controller.pem','capture-completion.json'}
        if {path.name for path in islice(directory.iterdir(),7)}!=names|set(publication().get('files',{})):raise Conflict('prepared endpoint contains unknown files')
        yield directory,record,files,destination,request,result,public['approved-controller.pem'].decode('ascii'),exact,deadline
        exact()
        if {path.name for path in islice(directory.iterdir(),7)}!=names|set(publication().get('files',{})):raise Conflict('endpoint preflight staging contains unknown retained files')
        for check in final_checks or ():check()


def preflight(control,config,request_id, *,verify_target,binding_reader=read_system_uuid,clearer=None,
              recovery_verifier=None,run=subprocess.run,clock=time.time,monotonic=time.monotonic,response=None):
    final_checks=[]
    with owned(control,config,request_id,verify_target=verify_target,binding_reader=binding_reader,
               clearer=clearer,recovery_verifier=recovery_verifier,monotonic=monotonic,final_checks=final_checks) as view:
        directory,record,files,destination,request,result,pem,exact,deadline=view
        kwargs={} if response is None else {'response':response}
        # Preserve the OWNED absolute budget across native preparation and probe.
        answer=probe(record,files,destination,request,result,pem,temporary_parent=directory,verify_target=exact,
            run=run,clock=clock,monotonic=monotonic,deadline=deadline,final_checks=final_checks,**kwargs)
        exact();return answer|{'request_id':request_id,'prepared_source_sha256':digest(_strict_read(directory,'source.json'))}
