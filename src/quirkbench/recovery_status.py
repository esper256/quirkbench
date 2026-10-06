"""Bounded read-only recovery facts and attended action eligibility.

This model grants no authority. Each action repeats its service's live checks.
Missing observations remain distinct from failed checks and recorded pairing.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import os
import time


@dataclass(frozen=True)
class Facts:
    boot: str = 'missing'
    boot_detail: str = 'Recovery checks have not reported a result.'
    usb: str = 'unavailable'
    evidence: str = 'unavailable'
    experiments: str = 'unavailable'
    binding: str = 'unavailable'
    network: str = 'unavailable'
    network_detail: str = 'Checking local networking.'
    ram_network: bool = False
    network_service: bool = False
    paired: bool = False
    pairing_pending: bool = False
    prepared_trust: bool = False
    controller: str = 'unconfigured'
    target: str = ''
    endpoint: str = ''
    prepared_media_sha256: str = ''


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    enabled: bool = True
    reason: str = ''
    remedy: str = ''


def actions(f: Facts) -> tuple[Action, ...]:
    storage = f.boot == 'verified' and f.evidence != 'unavailable'
    writable = storage and f.evidence == 'ready'
    hardware = f.binding != 'unavailable'
    current = f.binding == 'current' and f.paired
    network = f.ram_network and f.network_service
    connected = f.network == 'connected'
    return (
        Action('network', 'Wi-Fi & Ethernet', network,
               'Private RAM profiles or NetworkManager are unavailable.', 'Retry local networking; inspect its service log.'),
        Action('controller', 'Connection details' if f.paired else 'Connect to controller'),
        Action('diagnostics', 'Troubleshooting'), Action('terminal', 'Open terminal'), Action('power', 'Power'),
        Action('retry_checks', 'Retry recovery checks'), Action('retry_network', 'Restart local networking'),
        Action('reset_network', 'Reset temporary connections (forget this session)'),
        Action('pair', ('Finish initial pairing' if f.pairing_pending and f.paired else
                       'Initial pairing complete' if f.paired else 'Connect using prepared controller trust'),
               writable and hardware and (not f.paired or f.pairing_pending) and f.prepared_trust and connected,
               'Pairing needs verified writable USB control storage, hardware identity, prepared trust and a network.',
               'Prepare on the controller, retry checks or configure temporary networking.'),
        Action('save_network', 'Remember selected connections', writable and current and network,
               'Saving requires verified writable storage and the current paired target binding.', 'Connect or repair this target first.'),
        Action('replay_network', 'Retry saved connections', storage and current and network,
               'Restoration requires verified media and the current paired target binding.', 'Use temporary networking until binding is valid.'),
        Action('retarget', 'Move this USB to this computer', writable and f.paired and hardware,
               'Retargeting needs paired media, verified writable storage and hardware identity.', 'Review recovery checks and the existing controller retarget authorization.'),
        Action('endpoint', 'Repair controller address', writable and current,
               'Endpoint repair requires verified storage and the current target binding.', 'Restore binding or review the controller endpoint plan.'),
        Action('drain', 'Review original evidence', storage and current,
               'Evidence maintenance requires verified storage and the original binding.', 'Restore binding and stage the exact controller evidence grant.'),
        Action('collect', 'Collect recovery report'), Action('export', 'Export recovery report locally'),
        Action('upload', 'Review and send recovery report', storage and current and connected,
               'Upload requires normal pairing, current binding and a network.', 'Collect/export offline or restore normal pairing.'),
        Action('shutdown', 'Prepare safe shutdown', storage and f.binding in ('current','unpaired'),
               'Evidence preservation cannot be confirmed.', 'Review the explicitly unconfirmed local OS power option.'),
        Action('restart', 'Prepare safe restart', storage and f.binding in ('current','unpaired'),
               'Evidence preservation cannot be confirmed.', 'Review the explicitly unconfirmed local OS power option.'),
        Action('local_power', 'Local OS shutdown/restart (preservation unconfirmed)'),
    )


def recommendation(f: Facts) -> tuple[str, str, str]:
    if f.boot == 'running':
        return 'Recovery checks are running', 'Networking, diagnostics and the terminal remain available.', 'diagnostics'
    if f.boot != 'verified':
        return 'Recovery checks need attention', f.boot_detail, 'diagnostics'
    if f.usb in ('unavailable','invalid','incomplete'):
        return 'USB preparation needs attention', 'Prepare the final USB layout on the controller. No target partition changes are performed.', 'diagnostics'
    if f.evidence == 'unavailable' or f.experiments == 'unavailable':
        return 'USB storage needs attention', 'Review storage errors and retry checks. Existing evidence is retained.', 'diagnostics'
    if 'full' in (f.evidence,f.experiments):
        return 'USB space needs attention', 'Upload/export retained evidence or use explicit eligible cleanup. Only the affected operation is blocked.', 'diagnostics'
    if f.binding in ('unavailable','moved','maintenance'):
        return 'Computer binding needs attention', 'Review binding or the existing explicit retarget/endpoint workflow.', 'diagnostics'
    if f.network != 'connected':
        return "Let's get this computer connected", f.network_detail, 'network'
    if f.controller == 'connected':
        return 'Connected — ready for your next step', 'Continue your investigation on the controller. Each target run requires its own approval.', 'controller'
    return 'Connect to your controller', ('Paired, but no current authenticated contact. Retry the connection.' if f.paired
                                            else 'Use the controller trust prepared on this USB.'), 'controller'


def text(value: str, maximum=160) -> str:
    """Display host labels as text, never terminal control sequences."""
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:maximum]


def space(path: Path) -> str:
    try:
        result = os.statvfs(path)
        return 'full' if result.f_bavail == 0 or result.f_favail == 0 else 'ready'
    except OSError:
        return 'unavailable'


def paired_session(control, verify_target, binding_reader, *, deadline,operation='controller connection check'):
    """Current authenticated read-only contact, without registration or claims."""
    import ssl
    from .enrollment_target import _storage
    from .filesystem import _read, _managed_path
    from .enrollment_records import _document
    from .binding import verify_binding
    from .contracts import canonical, Conflict, identifier
    from .retarget_local import require_runtime_available
    from .endpoint_local import require_available
    from .release_http import _response, _length, _remaining
    control, verify = _storage(control, verify_target)
    sources = {'runtime.json': _read(control,'runtime.json',limit=65536)}
    from .shutdown_local import _existing_json
    runtime = _existing_json(sources['runtime.json'])
    target = identifier(runtime['device_id'])
    verify();verify_binding(runtime['target_binding'],reader=binding_reader)
    require_runtime_available(control);require_available(control,binding_reader=binding_reader)
    for name in (runtime['ca'], runtime['token_file']):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.as_posix() != name:
            raise Conflict('paired contact trust paths must remain in control storage')
        sources[name] = _read(_managed_path(control/path.parent),path.name,limit=65536)
    def exact():
        verify();verify_binding(runtime['target_binding'],reader=binding_reader)
        require_runtime_available(control);require_available(control,binding_reader=binding_reader)
        if any(_read(_managed_path(control/Path(name).parent),Path(name).name,limit=65536)!=raw
               for name,raw in sources.items()):
            raise Conflict('paired contact configuration changed')
        _remaining(deadline,time.monotonic,operation=operation)
    exact()
    context=ssl.create_default_context(cadata=sources[runtime['ca']].decode('ascii'))
    context.hostname_checks_common_name=False
    token=sources[runtime['token_file']].decode('ascii').strip()
    return runtime,context,token,exact


def paired_contact(control,verify_target,binding_reader,*,deadline):
    from .contracts import canonical,Conflict
    from .enrollment_records import _document
    from .release_http import _response,_length,_remaining
    runtime,context,token,exact=paired_session(control,verify_target,binding_reader,deadline=deadline)
    target=runtime['device_id']
    with _response(runtime['controller_url'].rstrip('/')+'/v1/endpoint-check', deadline,time.monotonic,
                   method='POST',body=canonical({'schema_version':1}),context=context,
                   headers={'Content-Type':'application/json','X-Device-ID':target,'Authorization':'Bearer '+token},
                   operation='controller connection check',before_request=exact) as reply:
        size=_length(reply,1024); raw=bytearray()
        while len(raw)<size:
            _remaining(deadline,time.monotonic,operation='controller connection check')
            chunk=reply.read1(size-len(raw))
            if not chunk: raise Conflict('incomplete controller connection response')
            raw.extend(chunk)
        exact()
        expected={'schema_version':1,'data':{'value':{'device_id':target,'credential_accepted':True,'work_queued':False}}}
        if (reply.getheader('Content-Type','').split(';')[0]!='application/json'
                or _document(bytes(raw))!=expected):
            raise Conflict('controller credential acceptance reply differs')
    exact()
    return True


def read_status(*, boot_record=Path('/run/quirkbench-boot.json'),
                control=Path('/var/lib/quirkbench/evidence/control'),
                experiments=Path('/var/lib/quirkbench/experiments'),
                command=None, verify_boot=None, binding_reader=None, profiles_ready=None,
                contact=None, timeout_s=8) -> Facts:
    """Observe current files/services; no mkdir, lock creation, repair or enrollment."""
    from .console import network_profiles_ready
    from .binding import read_system_uuid, verify_binding
    from .filesystem import read_file
    from .prepared_enrollment import validate_metadata, METADATA
    from .enrollment_records import _document
    from .runtime import boot_context
    from .ostree import CommandRunner
    deadline = time.monotonic()+timeout_s
    def native(argv):
        remaining = deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError('recovery status deadline exceeded')
        return CommandRunner(lambda *a:None, lambda:None, timeout_s=min(2,remaining),
                             operation='recovery status', failure_guidance='status unavailable')(argv)
    command = command or native
    binding_reader = binding_reader or read_system_uuid
    profiles_ready = profiles_ready or network_profiles_ready
    value = dict(Facts().__dict__)
    try:
        state = command(['/usr/bin/systemctl','show','quirkbench-recovery.service','--property=ActiveState,SubState,Result'])
        properties = dict(line.split('=',1) for line in state.splitlines() if '=' in line)
        if properties.get('ActiveState') == 'activating': value.update(boot='running',boot_detail='Recovery checks are running.')
        elif properties.get('ActiveState') == 'failed': value.update(boot='failed',boot_detail='The recovery service failed. Review boot logs and retry its checks.')
    except (OSError,ValueError,RuntimeError): pass
    if boot_record.exists() or boot_record.is_symlink():
        try:
            config, boot, live_verify = (verify_boot or (lambda path:boot_context(path,native_runner=command)))(boot_record)
            if boot.get('quirkbench.mode') != 'recovery': raise ValueError('not recovery')
            if value['boot'] != 'failed': value.update(boot='verified',boot_detail='Current boot identity and evidence mount verified.')
            capacity = boot.get('quirkbench.capacity', {})
            value['usb'] = 'prepared' if capacity.get('record_type') == 'prepared-capacity' else 'historical'
            value['prepared_media_sha256'] = capacity.get('prepared_media_sha256','')
            value['evidence'] = space(control.parent)
            value['experiments'] = 'unavailable' if boot.get('quirkbench.experiments_unavailable') else space(experiments)
        except (OSError,ValueError,RuntimeError,TypeError,KeyError):
            value.update(boot='failed',boot_detail='The boot record or current USB verification failed. Review logs; prepare incomplete media on the controller.')
    try: value['binding'] = 'unpaired' if binding_reader() else 'unavailable'
    except (OSError,ValueError,RuntimeError): pass
    # No persistent secret store is read when its mounted identity is unverified.
    if value['boot'] == 'verified' and value['evidence'] != 'unavailable':
        try:
            metadata = validate_metadata(_document(read_file(control, METADATA, limit=65536)))
            value.update(prepared_trust=True, endpoint=metadata['invitation']['controller_url'],
                         pairing_pending=(control/'prepared-enrollment.code').exists())
        except (OSError,ValueError,RuntimeError): pass
        if (control/'runtime.json').exists() or (control/'runtime.json').is_symlink():
            try:
                from .shutdown_local import _existing_json
                runtime = _existing_json(read_file(control,'runtime.json',limit=65536))
                from .contracts import identifier
                value.update(paired=True,target=identifier(runtime['device_id']),endpoint=text(runtime['controller_url']))
                from .binding import BindingError, system_uuid
                try:
                    verify_binding(runtime['target_binding'],reader=binding_reader)
                except BindingError:
                    try:
                        expected=system_uuid(runtime['target_binding']['system_uuid'])
                        actual=system_uuid(binding_reader())
                        value['binding']='moved' if expected!=actual else 'unavailable'
                    except (ValueError,TypeError,KeyError):value['binding']='unavailable'
                else:
                    value['binding'] = 'current'
                    from .retarget_local import require_runtime_available
                    from .endpoint_local import require_available
                    try:
                        require_runtime_available(control);require_available(control,binding_reader=binding_reader)
                    except (OSError,ValueError,RuntimeError): value['binding'] = 'maintenance'
            except (OSError,ValueError,RuntimeError,TypeError,KeyError):
                value['binding'] = 'unavailable'
    try: value['ram_network'] = profiles_ready()
    except (OSError,ValueError,RuntimeError): pass
    try:
        from .network_profiles import replay_blocked
        blocked=replay_blocked()
        state = command(['/usr/bin/systemctl','is-active','NetworkManager.service']).strip()
        value['network_service'] = state == 'active'
        if blocked:
            value.update(network='cleanup-blocked',network_service=False,network_detail='Saved secret replay cleanup is incomplete. Reset temporary connections explicitly; remembered USB selections are retained.')
        elif not value['network_service']:
            value.update(network='failed',network_detail='NetworkManager is unavailable. Review its service log and retry local networking.')
        elif not value['ram_network']:
            value.update(network='blocked',network_detail='Private RAM profiles are unavailable. Retry networking and inspect secret cleanup errors.')
        else:
            rows = command(['/usr/bin/nmcli','--terse','--escape','no','--fields','TYPE,STATE','device','status']).splitlines()[:64]
            devices = [line.split(':',1) for line in rows if ':' in line]
            connected = any(kind != 'loopback' and state == 'connected' for kind,state in devices)
            wifi = command(['/usr/bin/nmcli','--terse','--fields','WIFI-HW,WIFI','general']).strip().split(':')
            if connected: value.update(network='connected',network_detail='Connected to a local network.')
            elif 'disabled' in wifi: value.update(network='radio-blocked',network_detail='Wi-Fi radio is blocked or disabled. Enable it in Network setup; Ethernet remains available.')
            elif not any(kind=='wifi' for kind,_ in devices): value.update(network='no-wifi',network_detail='No Wi-Fi device was reported. Use Ethernet or inspect the Wi-Fi driver in logs.')
            else: value.update(network='disconnected',network_detail='Connect this computer to your network.')
    except (OSError,ValueError,RuntimeError):
        value.update(network='failed',network_detail='Local network observations failed. Inspect NetworkManager and retry.')
    if value['paired']:
        value['controller'] = 'disconnected'
        if value['binding'] == 'current' and value['network'] == 'connected':
            try:
                check = contact or (lambda:paired_contact(control,live_verify,binding_reader,deadline=min(deadline,time.monotonic()+2)))
                if check(): value['controller'] = 'connected'
            except (OSError,ValueError,RuntimeError,TypeError,KeyError): pass
    elif value['prepared_trust']:
        value['controller'] = 'waiting-network' if value['network'] != 'connected' else 'configured'
    return Facts(**value)
