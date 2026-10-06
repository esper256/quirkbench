"""Only explicit verified RAM selections persist, and old-target secrets stay inert."""
import json
from pathlib import Path

import pytest

from quirkbench import network_profiles as network
from quirkbench.contracts import Conflict,ContractError,canonical
from quirkbench.store import atomic_write

UUID='12345678-1234-1234-1234-123456789abc'
PROFILE=b'[connection]\nid=Private Wi-Fi\nuuid=12345678-1234-1234-1234-123456789abc\ntype=wifi\n[wifi-security]\npsk=never-export-this-password\n'


@pytest.fixture
def local(tmp_path):
    control=tmp_path/'control';control.mkdir(mode=0o700)
    atomic_write(control/'runtime.json',canonical({'schema_version':1,'device_id':'target-local','target_binding':{'schema_version':1,'system_uuid':UUID}}))
    atomic_write(control/'media-instance.json',canonical({'schema_version':1,'media_instance_id':'media-local'}))
    profiles=tmp_path/'ram';profiles.mkdir(mode=0o700)
    atomic_write(profiles/'Home Wi-Fi.nmconnection',PROFILE)
    atomic_write(profiles/'Unselected.nmconnection',PROFILE.replace(b'Private Wi-Fi',b'Unselected'))
    return control,profiles,{'verify_target':lambda:True,'binding_reader':lambda:UUID,
                            'profiles':profiles,'profiles_ready':lambda path:True}


@pytest.mark.parametrize('boundary',['network_files_retained','network_generation_published','network_selection_activated'])
def test_selected_private_generation_resumes_every_boundary_without_saving_unselected(local,boundary):
    control,profiles,kw=local
    def fault(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):network.save_selected(control,['Home Wi-Fi.nmconnection'],fault_hook=fault,**kw)
    answer=network.save_selected(control,['Home Wi-Fi.nmconnection'],**kw)
    assert network.save_selected(control,['Home Wi-Fi.nmconnection'],**kw)==answer
    assert len(list((control/'network/generations').iterdir()))==1
    assert not list((control/'network').rglob('Unselected.nmconnection'))
    for path in (control/'network').rglob('*'):
        assert path.stat().st_mode&0o077==0
    assert b'never-export-this-password' not in (control/'network/active.json').read_bytes()
    (profiles/'Home Wi-Fi.nmconnection').unlink()
    assert network.replay_selected(control,**kw)['replayed']
    assert (profiles/'Home Wi-Fi.nmconnection').read_bytes()==PROFILE


@pytest.mark.parametrize('change',['uuid','media','runtime','private_file','symlink','ram','not_ram'])
def test_changed_binding_configuration_or_private_bytes_never_replay(local,change,monkeypatch):
    control,profiles,kw=local;network.save_selected(control,['Home Wi-Fi.nmconnection'],**kw)
    (profiles/'Home Wi-Fi.nmconnection').unlink()
    kw=kw.copy()
    if change=='uuid':kw['binding_reader']=lambda:'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    elif change=='media':atomic_write(control/'media-instance.json',canonical({'schema_version':1,'media_instance_id':'changed'}))
    elif change=='runtime':
        value=json.loads((control/'runtime.json').read_bytes());value['device_id']='changed';atomic_write(control/'runtime.json',canonical(value))
    elif change in ('private_file','symlink'):
        path=next((control/'network/generations').rglob('*.nmconnection'))
        if change=='private_file':atomic_write(path,PROFILE.replace(b'password',b'changed'))
        else:path.unlink();path.symlink_to(profiles/'Unselected.nmconnection')
    elif change=='ram':atomic_write(profiles/'Home Wi-Fi.nmconnection',PROFILE.replace(b'password',b'changed'))
    else:kw['profiles_ready']=lambda path:False
    original=network._read;secret_reads=[]
    def observe(directory,name):
        if name.endswith('.nmconnection') and Path(directory).is_relative_to(control):secret_reads.append(name)
        return original(directory,name)
    monkeypatch.setattr(network,'_read',observe)
    with pytest.raises((ValueError,OSError)):network.replay_selected(control,**kw)
    if change in ('uuid','media','runtime'):assert secret_reads==[]
    if change!='ram':assert not (profiles/'Home Wi-Fi.nmconnection').exists()


def test_source_changed_or_configuration_moved_before_publication_keeps_selection_inactive(local):
    control,profiles,kw=local
    def changed(stage):
        if stage=='network_files_retained':atomic_write(profiles/'Home Wi-Fi.nmconnection',PROFILE.replace(b'password',b'changed'))
    with pytest.raises(Conflict):network.save_selected(control,['Home Wi-Fi.nmconnection'],fault_hook=changed,**kw)
    assert not (control/'network/active.json').exists()


@pytest.mark.parametrize('selected',[[],['../escape.nmconnection'],['same.nmconnection','same.nmconnection'],[{}],['newline\n.nmconnection']])
def test_invalid_selection_is_bounded_and_has_no_private_write(local,selected):
    control,profiles,kw=local
    with pytest.raises(ContractError):network.save_selected(control,selected,**kw)
    assert not (control/'network').exists()


def test_unknown_network_kind_and_duplicate_key_are_specific_blockers():
    for raw in (PROFILE.replace(b'type=wifi',b'type=vpn'),PROFILE+b'\n[connection]\ntype=wifi\n'):
        with pytest.raises(ContractError):network.validate_profile(raw)


def test_private_generation_schema_and_strict_reader():
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/network-profile-generation.v1.schema.json').read_bytes())
    value=json.loads((root/'examples/network-profile-generation.json').read_bytes())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(network.validate_generation(value))
    for patch in ({'schema_version':True},{'files':{}},{'files':{'../escape.nmconnection':'b'*64}},
                  {'password':'private'},{'runtime_sha256':'not-a-digest'}):
        with pytest.raises(ContractError):network.validate_generation(value|patch)


def test_selected_file_mount_is_rejected_before_any_secret_read(local,monkeypatch):
    control,profiles,kw=local;reads=[]
    monkeypatch.setattr(network,'nested_mounts',lambda root:[str(profiles),str(profiles/'Home Wi-Fi.nmconnection')] if root==profiles else [])
    original=network._read
    def read(directory,name):reads.append((directory,name));return original(directory,name)
    monkeypatch.setattr(network,'_read',read)
    with pytest.raises(ContractError,match='nested mounts'):network.save_selected(control,['Home Wi-Fi.nmconnection'],**kw)
    assert reads==[] and not (control/'network').exists()


@pytest.mark.parametrize('change',['stage','final','source'])
def test_final_capture_fence_preserves_previous_active_selection(local,change):
    control,profiles,kw=local;network.save_selected(control,['Unselected.nmconnection'],**kw)
    before=(control/'network/active.json').read_bytes()
    def changed(stage):
        if change=='stage' and stage=='network_files_retained':
            path=next((control/'network/generations').glob('.pending-*/Home Wi-Fi.nmconnection'))
            atomic_write(path,PROFILE.replace(b'password',b'changed'))
        elif stage=='network_generation_published':
            path=profiles/'Home Wi-Fi.nmconnection' if change=='source' else next((control/'network/generations').glob('*/Home Wi-Fi.nmconnection'))
            atomic_write(path,PROFILE.replace(b'password',b'changed'))
    with pytest.raises(Conflict):network.save_selected(control,['Home Wi-Fi.nmconnection'],fault_hook=changed,**kw)
    assert (control/'network/active.json').read_bytes()==before


def test_attended_selection_copies_only_explicit_connection_numbers(local):
    from io import StringIO
    control,profiles,kw=local;output=StringIO()
    result=network.save_attended_network(control=control,input_stream=StringIO('1\n'),output_stream=output,**kw)
    assert result['profiles']==['Home Wi-Fi.nmconnection']
    assert 'never-export-this-password' not in output.getvalue()


def test_console_save_requires_both_verified_recovery_and_private_ram(tmp_path):
    from io import StringIO
    from quirkbench.console import run_console
    from test_console import boot_record
    record=tmp_path/'boot.json';calls=[];output=StringIO()
    run_console(boot_record=record,input_stream=StringIO('6\n'),output_stream=output,profiles_ready=lambda:True,
                run_network_save=lambda **kw:calls.append(kw))
    assert calls==[]
    boot_record(record)
    run_console(boot_record=record,input_stream=StringIO('6\n'),output_stream=output,profiles_ready=lambda:False,
                run_network_save=lambda **kw:calls.append(kw))
    assert calls==[]
    run_console(boot_record=record,input_stream=StringIO('6\n'),output_stream=output,profiles_ready=lambda:True,
                run_network_save=lambda **kw:calls.append(kw))
    assert len(calls)==1 and calls[0]['output_stream'] is output


def test_native_network_oneshot_orders_binding_replay_before_networkmanager(tmp_path):
    from quirkbench.boot import install_runtime,install_candidate_runtime
    from test_console import CONFIG
    for name,installer,prerequisite in [('recovery',install_runtime,'quirkbench-recovery.service'),
                                        ('candidate',install_candidate_runtime,'quirkbench-candidate.service')]:
        root=tmp_path/name;(root/'etc').mkdir(parents=True)
        (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1')
        installer(root,CONFIG) if name=='recovery' else installer(root)
        configuration=root/('etc' if name=='recovery' else 'usr/etc')
        text=(configuration/'systemd/system/quirkbench-network-state.service').read_text()
        if name == 'candidate':
            assert 'After='+prerequisite in text and 'Requires='+prerequisite in text
            assert 'ExecStartPost=/usr/bin/python3 -m quirkbench.network_profiles' in text
        else:
            assert prerequisite not in text and 'ExecStartPost=' not in text
            replay=(configuration/'systemd/system/NetworkManager.service.d/quirkbench.conf').read_text()
            assert 'ExecStartPre=/usr/bin/python3 -m quirkbench.network_profiles' in replay
            assert 'Environment=PYTHONPATH=/usr/lib/quirkbench' in replay
        assert 'Before=NetworkManager.service' in text
        assert 'network-online.target' not in text


def test_replay_final_binding_failure_removes_only_new_unchanged_ram_files(local):
    control,profiles,kw=local;network.save_selected(control,['Home Wi-Fi.nmconnection'],**kw)
    (profiles/'Home Wi-Fi.nmconnection').unlink();original=(profiles/'Unselected.nmconnection').read_bytes()
    def verify():
        if (profiles/'Home Wi-Fi.nmconnection').exists():raise Conflict('binding changed after RAM copy')
    with pytest.raises(Conflict):network.replay_selected(control,**(kw|{'verify_target':verify}))
    assert not (profiles/'Home Wi-Fi.nmconnection').exists()
    assert (profiles/'Unselected.nmconnection').read_bytes()==original


@pytest.mark.parametrize('cleanup',['complete','unlink_failure','unsafe_ram'])
def test_native_oneshot_blocks_networkmanager_if_saved_copies_cannot_be_cleared(local,monkeypatch,cleanup,capsys):
    from quirkbench import runtime
    control,profiles,kw=local;network.save_selected(control,['Home Wi-Fi.nmconnection'],**kw)
    path=profiles/'Home Wi-Fi.nmconnection';path.unlink()
    def verify():
        if path.exists():raise Conflict('final binding rejection')
    original=Path.unlink
    if cleanup=='unlink_failure':
        def unlink(file,*a,**opts):
            if file==path:raise OSError('cannot unlink RAM fixture')
            return original(file,*a,**opts)
        monkeypatch.setattr(Path,'unlink',unlink)
    if cleanup=='unsafe_ram':kw=kw|{'profiles_ready':lambda _:not path.exists()}
    real=network.replay_selected
    monkeypatch.setattr(runtime,'CONTROL',control)
    monkeypatch.setattr(runtime,'boot_context',lambda:(None,{},verify))
    monkeypatch.setattr(network,'replay_selected',lambda root,**opts:real(root,**(kw|opts)))
    assert network.main()==(0 if cleanup=='complete' else 1)
    assert path.exists()==(cleanup!='complete')
    assert 'never-export-this-password' not in capsys.readouterr().out



def test_native_keyfile_modes_are_required_without_normalizing_user_files(local):
    control, profiles, kw = local
    network.save_selected(control, ['Home Wi-Fi.nmconnection'], **kw)
    path = profiles / 'Home Wi-Fi.nmconnection'
    path.chmod(0o644)
    with pytest.raises(ContractError, match='NetworkManager requires'):
        network.replay_selected(control, **kw)
    assert path.stat().st_mode & 0o777 == 0o644
