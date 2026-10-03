"""Explicit first repository publication; the existing native service owns listeners."""
import os
from pathlib import Path
import subprocess

from .contracts import Conflict,ContractError,canonical,digest,identifier
from .controller_setup import _private_path,_durable_directory,_database_present
from .controller_endpoint import _strict_read
from .controller_service import configuration,validate_configuration,UNIT
from .enrollment import _document,_snapshot
from .enrollment_client import endpoint
from .enrollment_credentials import export_public_key
from .maintenance import private_lock
from .publication_setup_contracts import STEPS,load,validate
from .setup_contracts import SetupUnavailable
from .state_reader import StateReader,read_file
from .store import atomic_write
from .controller_tls import FILES as TLS_FILES,load_identity,_read as tls_read

FILES={'source_configuration_sha256':'source.json','destination_configuration_sha256':'destination.json','public_key_sha256':'key.asc'}


def history(root):
    """Validate exact retained source→destination delta, independently of live state."""
    root=Path(root);directory=root/'private/publication-setup'
    journal=directory/'journal.json'
    if not journal.exists() and not journal.is_symlink():return None
    saved=load(_strict_read(directory,'journal.json'));intent=saved['intent']
    captured={key:_strict_read(directory,name) for key,name in FILES.items()}
    if intent['state_root']!=str(root) or any(digest(raw)!=intent[key] for key,raw in captured.items()):
        raise Conflict('publication setup retained inputs differ')
    old=_document(captured['source_configuration_sha256']);new=_document(captured['destination_configuration_sha256'])
    expected=destination(root,old,intent['repository_alias'],intent['repository_url'],intent['signing_home'],intent['signing_fingerprint'])
    if expected!=new or captured['destination_configuration_sha256']!=canonical(new):
        raise Conflict('publication setup delta differs from explicit choices')
    tls_directory=Path(old['cert']).parent
    identity_raw=tls_read(tls_directory,'identity.json');identity=load_identity(identity_raw)
    if (digest(identity_raw)!=intent['controller_tls_identity_sha256'] or
            any(digest(tls_read(tls_directory,name))!=identity['files'][name] for name in TLS_FILES)):
        raise Conflict('publication setup original TLS bytes changed')
    if 'repository_initialized' in saved['completed_steps']:
        retained=_strict_read(directory,'repository-config')
        if (digest(retained)!=saved['repository_configuration_sha256'] or
                retained!=read_file(root/'repositories'/intent['repository_alias'],'config',limit=65536)):
            raise Conflict('publication setup repository configuration changed')
    return saved,captured,old,new


def destination(root,source,alias,url,signing_home,fingerprint):
    identifier(alias)
    if source.get('credential_registry') is not True:raise Conflict('initial publication requires registry mode')
    if any(key in source for key in ('repositories','repository_endpoint','composition_signing')):
        raise Conflict('existing publication requires explicit trust maintenance')
    host,port=endpoint(url)
    if host!=source.get('host','127.0.0.1') or port==source.get('port',8443):
        raise ContractError('repository URL must match the controller TLS host with a separate port')
    expected=source|{'repositories':{alias:str(Path(root)/'repositories'/alias)},
        'repository_endpoint':{'url':url},'composition_signing':{'home':str(signing_home),'fingerprint':fingerprint}}
    validate_configuration(root,expected)
    return expected


def verified_successor(root,source_raw,current_raw):
    """Only this exact committed first-publication successor can satisfy setup replay."""
    value=history(root)
    if value is None:return False
    saved,captured,old,new=value
    return (saved['completed_steps']==list(STEPS) and source_raw==captured['source_configuration_sha256']
        and current_raw==captured['destination_configuration_sha256'])


def response(saved):
    intent=saved['intent']
    return {'request_id':saved['request_id'],'configured':True,'repository_url':intent['repository_url'],
        'repository_alias':intent['repository_alias'],'signing_fingerprint':intent['signing_fingerprint'],
        'public_key_sha256':intent['public_key_sha256'],'controller_certificate_sha256':intent['controller_certificate_sha256'],
        'enrollment_available':False,'service_start_required':True,'boot_authorized':False,
        'next_command':'systemctl --user start '+UNIT}


def configure(root,alias,url,signing_home,fingerprint,request_id, *,unit=None,runner=subprocess.run,
              run=subprocess.run,tls_inspector=None,fault_hook=None):
    """Configure only a stopped, idle initial service; no keys, start or target grant."""
    from .controller_install import _idle
    from .setup_service import _service_state,_effective_unit
    from .state_config import _config_home
    identifier(request_id);identifier(alias)
    root=_private_path(root);signing_home=_private_path(signing_home)
    if not signing_home.is_dir():raise SetupUnavailable('provision an existing private composition signing home first')
    if not _database_present(root):raise SetupUnavailable('complete initial controller setup first')
    unit=Path(unit or _config_home()/('systemd/user/'+UNIT))
    if str(unit)!=str(unit.absolute()) or unit.resolve()!=unit:raise ContractError('native controller unit must be canonical')
    fault=fault_hook or (lambda _:None)
    directory=root/'private/publication-setup'
    choices={'repository_alias':alias,'repository_url':url,'signing_home':str(signing_home),
        'signing_fingerprint':fingerprint,'unit':str(unit)}
    previous=history(root)
    if previous:
        saved,captured,old,new=previous
        if saved['request_id']!=request_id or any(saved['intent'][key]!=value for key,value in choices.items()):
            raise Conflict('publication setup already has another request or choices')
        if saved['completed_steps']==list(STEPS):
            if _strict_read(root/'private','controller-service.json')!=captured['destination_configuration_sha256']:
                raise Conflict('completed publication configuration is unavailable or maintained separately')
            return response(saved)
    with private_lock(root/'command.lock') as command_fd,private_lock(root/'coordinator.lock') as owner_fd:
        _idle(root)
        config=configuration(root);current=_strict_read(root/'private','controller-service.json')
        if current!=canonical(config):raise ContractError('publication setup requires canonical service configuration')
        previous=history(root)
        if previous:
            saved,captured,old,new=previous
            intent=saved['intent']
            if saved['request_id']!=request_id or any(intent[key]!=value for key,value in choices.items()):
                raise Conflict('publication setup already has another request or choices')
            if current not in (captured['source_configuration_sha256'],captured['destination_configuration_sha256']):
                raise Conflict('publication setup cannot overwrite later configuration maintenance')
            if saved['completed_steps']==list(STEPS):
                if current!=captured['destination_configuration_sha256']:raise Conflict('completed publication configuration is unavailable')
                return response(saved)
        else:
            old=config;new=destination(root,old,alias,url,signing_home,fingerprint)
            captured={'source_configuration_sha256':current,'destination_configuration_sha256':canonical(new)}
            saved=None
        runtime=Path(old['runtime']).parent.parent
        tls_directory=Path(old['cert']).parent
        tls_material={name:tls_read(tls_directory,name) for name in (*TLS_FILES,'identity.json')}
        def pure_fence(expected=None):
            _idle(root)
            for path,fd in ((root/'command.lock',command_fd),(root/'coordinator.lock',owner_fd)):
                held=os.fstat(fd);named=path.lstat()
                if (held.st_dev,held.st_ino)!=(named.st_dev,named.st_ino):raise Conflict('publication setup lock identity changed')
            fresh=_strict_read(root/'private','controller-service.json')
            if (fresh!=expected if expected is not None else fresh not in (captured['source_configuration_sha256'],captured['destination_configuration_sha256'])):
                raise Conflict('controller configuration changed during publication setup')
            # No initial trust replacement after an invitation or target was issued.
            with StateReader(root).connection() as db:
                if any(db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() for table in ('devices','enrollment_codes','enrollment_requests','credential_generations')):
                    raise Conflict('already-issued target trust requires explicit maintenance')
            validate_configuration(root,new)
            if any(tls_read(tls_directory,name)!=raw for name,raw in tls_material.items()):
                raise Conflict('original controller TLS changed during publication setup')
        def guard(expected=None):
            pure_fence(expected)
            if _service_state(runner)!='stopped':raise Conflict('stop the existing controller user service before publication setup')
            _effective_unit(runner,unit,runtime,root)
            pure_fence(expected)
        guard()
        snapshot=_snapshot(root,tls_inspector=tls_inspector);guard()
        public_key=export_public_key(new['composition_signing'],run=run).encode('ascii');guard()
        if saved and public_key!=captured['public_key_sha256']:raise Conflict('selected repository public key changed')
        if saved is None:
            captured['public_key_sha256']=public_key
            repo=root/'repositories'/alias
            if repo.exists() or repo.is_symlink():raise Conflict('initial publication repository already exists; select a fresh explicit alias')
            intent={'state_root':str(root),'repository_alias':alias,'repository_url':url,'signing_home':str(signing_home),
                'signing_fingerprint':fingerprint,'unit':str(unit),**{key:digest(raw) for key,raw in captured.items()},
                'controller_tls_identity_sha256':digest(tls_material['identity.json']),'controller_certificate_sha256':snapshot['certificate_sha256']}
            saved=validate({'schema_version':1,'record_type':'publication-setup','request_id':request_id,
                'request_digest':digest(canonical({'kind':'publication-setup','arguments':intent})),'intent':intent,'completed_steps':[],
                'repository_configuration_sha256':None})
            _durable_directory(_private_path(directory))
            for key,name in FILES.items():
                path=directory/name
                if path.exists() and _strict_read(directory,name)!=captured[key]:raise Conflict('uncommitted publication inputs differ')
                atomic_write(path,captured[key])
            atomic_write(directory/'journal.json',canonical(saved))
        def completed(step,repository_sha=None):
            guard(captured['destination_configuration_sha256'] if step=='configuration_published' else None)
            if history(root)[0]!=saved:raise Conflict('publication journal changed before completion')
            if step not in saved['completed_steps']:
                if repository_sha is not None:saved['repository_configuration_sha256']=repository_sha
                saved['completed_steps'].append(step);validate(saved);atomic_write(directory/'journal.json',canonical(saved))
            fault(step)
        completed('inputs_retained')
        repo=_private_path(root/'repositories'/alias)
        _durable_directory(repo)
        config_path=repo/'config'
        if 'repository_initialized' not in saved['completed_steps']:
            # OSTree init is idempotent for this exact fresh owned repository.
            # A present config alone cannot establish interrupted initialization.
            if config_path.exists():
                from .job_coordinator import repository_tree
                repository_tree(repo)
            try:
                result=run(['ostree','--repo='+str(repo),'init','--mode=archive'],capture_output=True,check=False,timeout=30,stdin=subprocess.DEVNULL)
            except (OSError,subprocess.TimeoutExpired) as exc:raise SetupUnavailable('native OSTree repository initialization unavailable') from exc
            if result.returncode:raise SetupUnavailable('native OSTree repository initialization failed; inspect private setup state')
        elif not config_path.exists():raise Conflict('completed publication repository configuration is unavailable')
        guard()
        from .job_coordinator import repository_tree
        repository_tree(repo)
        try:
            inspected=run(['ostree','--repo='+str(repo),'refs'],capture_output=True,check=False,timeout=15,stdin=subprocess.DEVNULL)
        except (OSError,subprocess.TimeoutExpired) as exc:raise SetupUnavailable('native OSTree repository inspection unavailable') from exc
        if inspected.returncode or inspected.stdout!=b'':raise Conflict('first publication requires an initialized empty OSTree repository')
        guard();repository_tree(repo)
        repo_raw=read_file(repo,'config',limit=65536)
        retained=directory/'repository-config'
        if retained.exists():
            if _strict_read(directory,'repository-config')!=repo_raw:raise Conflict('initialized repository configuration changed')
        else:atomic_write(retained,repo_raw)
        completed('repository_initialized',digest(repo_raw))
        if export_public_key(new['composition_signing'],run=run).encode('ascii')!=public_key:
            raise Conflict('repository key changed before configuration publication')
        guard()
        if history(root)[0]!=saved:raise Conflict('publication journal changed before configuration publication')
        repository_tree(repo)
        if _strict_read(directory,'repository-config')!=read_file(repo,'config',limit=65536):
            raise Conflict('repository configuration changed before publication')
        atomic_write(root/'private/controller-service.json',captured['destination_configuration_sha256'])
        fault('configuration_written')
        completed('configuration_published')
        guard(captured['destination_configuration_sha256'])
        repository_tree(repo)
        if history(root)[0]!=saved:raise Conflict('publication journal changed before acknowledgment')
        return response(saved)
