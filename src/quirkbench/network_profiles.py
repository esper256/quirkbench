"""Explicit private NetworkManager selections and binding-gated RAM replay."""
import configparser
import os
from pathlib import Path
import uuid
import unicodedata
import sys

from .binding import read_system_uuid,verify_binding
from .contracts import Conflict,ContractError,canonical,digest,identifier,sha256
from .filesystem import _managed_path, _durable_directory
from .filesystem import _read
from .enrollment_records import _document
from .enrollment_target import _storage
from .filesystem import private_lock, nested_mounts
from .store import atomic_write,sync_directory

PROFILES=Path('/etc/NetworkManager/system-connections')
MAX_PROFILES=16
MAX_TOTAL=512*1024


class ReplayCleanupIncomplete(ContractError):
    """Native network ownership must not start after uncertain secret cleanup."""


def _name(name):
    if (not isinstance(name,str) or not name.endswith('.nmconnection') or name.startswith('.')
            or '/' in name or '\\' in name
            or any(unicodedata.category(char).startswith('C') for char in name)
            or len(name.encode('utf-8'))>255):
        raise ContractError('invalid selected connection filename')
    return name


def validate_profile(raw):
    """Bounded NM keyfiles, only nmtui's supported Ethernet/Wi-Fi connection types."""
    if not isinstance(raw,bytes) or not 0<len(raw)<=65536:raise ContractError('network profile exceeds byte limit')
    parser=configparser.ConfigParser(interpolation=None,strict=True);parser.optionxform=str
    try:
        parser.read_string(raw.decode('utf-8'))
        if parser.defaults() or parser['connection']['type'] not in ('ethernet','wifi','802-3-ethernet','802-11-wireless'):
            raise ValueError()
        value=parser['connection']['uuid']
        if str(uuid.UUID(value))!=value or uuid.UUID(value).int==0:raise ValueError()
    except (UnicodeError,ValueError,KeyError,configparser.Error) as exc:
        raise ContractError('selected connection is not a supported NetworkManager keyfile') from exc
    return raw


def _identity(control,binding_reader):
    from .retarget_local import require_runtime_available
    require_runtime_available(control)
    raw=_read(control,'runtime.json');runtime=_document(raw)
    if not isinstance(runtime,dict) or type(runtime.get('schema_version')) is not int or runtime['schema_version']!=1:
        raise ContractError('active network target configuration is invalid')
    verify_binding(runtime.get('target_binding'),reader=binding_reader)
    identifier(runtime.get('device_id'))
    media=_document(_read(control,'media-instance.json'))
    if (not isinstance(media,dict) or set(media)!={'schema_version','media_instance_id'}
            or type(media['schema_version']) is not int or media['schema_version']!=1):
        raise ContractError('private network media identity is unavailable')
    identifier(media['media_instance_id'])
    return {'device_id':runtime['device_id'],'target_binding':runtime['target_binding'],
            'media_instance_id':media['media_instance_id'],'runtime_sha256':digest(raw)}


def validate_generation(value):
    from .binding import system_uuid
    if (not isinstance(value,dict) or set(value)!={'schema_version','record_type','device_id','media_instance_id','target_binding','runtime_sha256','files'}
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['record_type']!='network-profile-generation'):
        raise ContractError('invalid private network generation')
    identifier(value['device_id']);identifier(value['media_instance_id']);sha256(value['runtime_sha256'])
    binding=value['target_binding']
    if (not isinstance(binding,dict) or set(binding)!={'schema_version','system_uuid'}
            or type(binding['schema_version']) is not int or binding['schema_version']!=1):
        raise ContractError('invalid private network target binding')
    system_uuid(binding['system_uuid'])
    if not isinstance(value['files'],dict) or not 1<=len(value['files'])<=MAX_PROFILES:
        raise ContractError('select between one and sixteen network profiles')
    for name,identity in value['files'].items():_name(name);sha256(identity)
    return value


def _ready(profiles,ready,names=()):
    from .console import network_profiles_ready
    if not (ready or network_profiles_ready)(profiles):
        raise ContractError('network profile storage must be verified private RAM')
    _managed_path(profiles)
    if any(Path(path)!=profiles for path in nested_mounts(profiles)):
        raise ContractError('RAM network profile storage contains nested mounts')
    device=profiles.stat().st_dev
    for name in names:
        path=profiles/_name(name)
        if (path.exists() or path.is_symlink()) and (path.is_symlink() or path.stat().st_dev!=device):
            raise ContractError('network profile artifact is outside the verified RAM device')
        # NetworkManager ignores keyfiles accessible by group/other users, even
        # inside its private tmpfs. Retained copies are not native keyfiles.
        if path.exists() and path.stat().st_mode & 0o077:
            raise ContractError('NetworkManager requires private keyfile permissions')


def _captured(directory,expected):
    _managed_path(directory)
    if (set(path.name for path in directory.iterdir())!=set(expected)
            or any(_read(directory,name)!=raw for name,raw in expected.items())):
        raise Conflict('retained private network generation changed before activation')


def save_selected(control,selected, *, verify_target,profiles=PROFILES,profiles_ready=None,
                  binding_reader=read_system_uuid,fault_hook=None):
    """Only explicitly selected RAM files enter a crash-safe private generation."""
    if not isinstance(selected,list) or not 1<=len(selected)<=MAX_PROFILES:
        raise ContractError('select between one and sixteen distinct connection filenames')
    for name in selected:_name(name)
    if len(set(selected))!=len(selected):raise ContractError('select distinct connection filenames')
    control,verify=_storage(control,verify_target);profiles=Path(profiles);_ready(profiles,profiles_ready,selected)
    fault_hook=fault_hook or (lambda _:None)
    with private_lock(control/'runtime-config.lock'):
        from .shutdown_local import require_available
        require_available(control)
        verify();identity=_identity(control,binding_reader)
        files={name:validate_profile(_read(profiles,name)) for name in sorted(selected)}
        if sum(map(len,files.values()))>MAX_TOTAL:raise ContractError('selected network profiles exceed total byte limit')
        manifest=validate_generation({'schema_version':1,'record_type':'network-profile-generation',
            **identity,'files':{name:digest(raw) for name,raw in files.items()}})
        raw=canonical(manifest);generation=digest(raw)
        base=_managed_path(control/'network');_durable_directory(base)
        parent=_managed_path(base/'generations');_durable_directory(parent)
        final=_managed_path(parent/generation);stage=_managed_path(parent/('.pending-'+generation))
        expected=files|{'manifest.json':raw}
        destination=final if final.exists() else stage
        _durable_directory(destination)
        if any(path.name not in expected for path in destination.iterdir()):
            raise Conflict('private network generation contains unexpected files')
        for name,value in expected.items():
            verify()
            if (destination/name).exists() or (destination/name).is_symlink():
                if _read(destination,name)!=value:raise Conflict('retained network generation differs from selected bytes')
            elif destination==final:raise Conflict('completed private network generation is incomplete')
            else:atomic_write(destination/name,value)
        sync_directory(destination);fault_hook('network_files_retained')
        verify()
        if identity!=_identity(control,binding_reader):raise Conflict('target configuration changed during network selection')
        _captured(destination,expected)
        _ready(profiles,profiles_ready,selected)
        if any(_read(profiles,name)!=value for name,value in files.items()):
            raise Conflict('selected RAM connection changed during capture; review the selection again')
        if destination==stage:os.rename(stage,final);sync_directory(parent)
        fault_hook('network_generation_published')
        verify()
        if identity!=_identity(control,binding_reader):raise Conflict('target configuration changed before network publication')
        _captured(final,expected);_ready(profiles,profiles_ready,selected)
        if any(_read(profiles,name)!=value for name,value in files.items()):
            raise Conflict('selected RAM connection changed before active publication')
        atomic_write(base/'active.json',raw);fault_hook('network_selection_activated')
        return {'saved':True,'generation':generation,'profiles':sorted(files)}


def replay_selected(control, *, verify_target,profiles=PROFILES,profiles_ready=None,binding_reader=read_system_uuid):
    """Validate active binding before reading profile secrets, copy only into RAM."""
    control,verify=_storage(control,verify_target);profiles=Path(profiles);_ready(profiles,profiles_ready)
    with private_lock(control/'runtime-config.lock'):
        from .shutdown_local import require_available
        require_available(control)
        verify()
        from .retarget_local import require_runtime_available
        require_runtime_available(control)
        if not (control/'network/active.json').exists() and not (control/'network/active.json').is_symlink():return {'replayed':False,'reason':'no_selected_profiles'}
        identity=_identity(control,binding_reader)
        base=_managed_path(control/'network');manifest=validate_generation(_document(_read(base,'active.json')))
        if any(manifest[name]!=value for name,value in identity.items()):
            raise Conflict('saved network selection belongs to another target/media/configuration')
        generation=digest(canonical(manifest));directory=_managed_path(base/'generations'/generation)
        if (set(path.name for path in directory.iterdir())!=set(manifest['files'])|{'manifest.json'}
                or _read(directory,'manifest.json')!=canonical(manifest)):
            raise Conflict('private network generation is incomplete or changed')
        files={name:validate_profile(_read(directory,name)) for name in manifest['files']}
        if sum(map(len,files.values()))>MAX_TOTAL or any(digest(raw)!=manifest['files'][name] for name,raw in files.items()):
            raise Conflict('private network profile differs from selected bytes')
        created=[]
        try:
            for name,raw in files.items():
                verify();_ready(profiles,profiles_ready,files)
                if identity!=_identity(control,binding_reader):raise Conflict('target binding changed before network replay')
                if (profiles/name).exists() or (profiles/name).is_symlink():
                    if _read(profiles,name)!=raw:raise Conflict('RAM network profile already has different local settings')
                else:created.append(name);atomic_write(profiles/name,raw)
            verify();_ready(profiles,profiles_ready,files)
            if identity!=_identity(control,binding_reader) or any(_read(profiles,name)!=raw for name,raw in files.items()):
                raise Conflict('target binding or RAM connection changed during replay')
        except BaseException as failure:
            # Only this invocation's unchanged files on verified RAM are ours to
            # remove. Preserve existing or subsequently edited local connections.
            incomplete=False
            for name in created:
                try:
                    _ready(profiles,profiles_ready,[name])
                    try:raw=_read(profiles,name)
                    except FileNotFoundError:continue
                    if raw==files[name]:(profiles/name).unlink();sync_directory(profiles)
                    else:incomplete=True
                except (OSError,ValueError):incomplete=True
            if incomplete:
                raise ReplayCleanupIncomplete('saved network replay cleanup incomplete; NetworkManager must remain blocked') from failure
            raise
        return {'replayed':True,'profiles':sorted(files)}


def save_attended_network(*,input_stream=None,output_stream=None,control=None,profiles=PROFILES,
                          verify_target=None,profiles_ready=None,binding_reader=read_system_uuid):
    from .runtime import CONTROL,boot_context
    from .enrollment_console import _answer
    control=control or CONTROL;source=input_stream or sys.stdin;output=output_stream or sys.stdout
    if verify_target is None:
        _,boot,verify_target=boot_context()
        if boot['quirkbench.mode']!='recovery':raise ContractError('network selection requires verified recovery')
    control,verify_target=_storage(control,verify_target);profiles=Path(profiles);_ready(profiles,profiles_ready)
    # Binding precedes even enumeration of the private selected state.
    _identity(control,binding_reader)
    names=sorted(_name(path.name) for path in profiles.iterdir() if path.name.endswith('.nmconnection'))
    if len(names)>MAX_PROFILES:raise ContractError('too many RAM connections; reduce them with Network setup')
    if not names:
        print('No RAM connections to save; configure Network first.',file=output);return None
    for number,name in enumerate(names,1):print(str(number)+') '+name,file=output)
    selection=_answer(source,output,'Select connection numbers to save, separated by commas (empty cancels): ',128)
    if selection is None or selection=='':return None
    try:
        numbers=[int(item.strip()) for item in selection.split(',')]
        if any(number<1 or number>len(names) for number in numbers):raise ValueError()
    except ValueError as exc:raise ContractError('invalid connection selection') from exc
    answer=save_selected(control,[names[number-1] for number in numbers],verify_target=verify_target,
        profiles=profiles,profiles_ready=profiles_ready,binding_reader=binding_reader)
    print('Selected private connections saved for this target. Replay occurs only after binding checks on the next boot.',file=output)
    return answer


def main():
    """Existing native network-state oneshot invokes this before NetworkManager."""
    from .runtime import CONTROL,boot_context
    try:
        _,_,verify=boot_context()
        answer=replay_selected(CONTROL,verify_target=verify)
        print('Selected network profiles restored to private RAM.' if answer['replayed'] else 'No saved network selection; local Network setup remains available.')
    except ReplayCleanupIncomplete:
        print('Saved network replay cleanup incomplete; NetworkManager remains blocked. Inspect private RAM profiles locally.')
        return 1
    except (OSError,ValueError,RuntimeError):
        # Missing initial setup/moved media must keep local nmtui usable. Do not
        # log private filenames, keyfile bytes or malformed config contents.
        print('Saved network selection blocked; local Network setup remains available.')
    return 0


if __name__=='__main__':raise SystemExit(main())
