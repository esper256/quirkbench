"""Recovery disarm uses only the verified state partition, even without p4/p5."""
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import pytest

from quirkbench import boot, runtime
from quirkbench.boot import BootError, clear_once
from test_boot import CONFIG


def fixture(tmp_path):
    state = tmp_path/'state'; state.mkdir(mode=0o700)
    directory = state/'quirkbench'; directory.mkdir(mode=0o700)
    env = directory/'next.env'; env.write_bytes(b' '*1024); env.chmod(0o600)
    devices = [tmp_path/str(index) for index in range(6)]
    for device in devices: device.touch()
    dev=(os.major(state.stat().st_dev),os.minor(state.stat().st_dev))
    layout = SimpleNamespace(partitions=[SimpleNamespace(path=device,major_minor=dev) for device in devices])
    mounts = f'1 0 {dev[0]}:{dev[1]} / {state} rw,nosuid,nodev,noexec - vfat {devices[2]} rw,umask=0077\n'
    calls = []; observed = []
    values = {'next_entry':'candidate','candidate_id':'a'*64,'target_uuid':CONFIG.esp_partuuid,'unrelated':'retained'}
    def verify(expected, **kwargs):
        assert expected.disk_guid == CONFIG.disk_guid and expected.partition_uuids[2] == CONFIG.state_partuuid
        assert kwargs == {'allow_data_mounted':True,'mode':'recovery'}
        observed.append(True); return layout
    def run(argv):
        calls.append(argv)
        assert argv[0] == 'grub2-editenv' and argv[1] == str(env)
        if argv[2] == 'unset':
            assert argv[3:] == ['next_entry','candidate_id','target_uuid']
            for key in argv[3:]: values.pop(key, None)
        else:
            assert argv[2:] == ['list']
            return '\n'.join(f'{key}={value}' for key,value in values.items())
        return ''
    return SimpleNamespace(state=state, env=env, layout=layout, mounts=mounts,
        values=values, calls=calls, observed=observed, run=run, verify=verify)


def clear(context, **kwargs):
    clear_once(CONFIG, state_mount=context.state, runner=kwargs.pop('runner',context.run),
        identity_verifier=kwargs.pop('identity_verifier',context.verify), mountinfo=kwargs.pop('mountinfo',context.mounts), **kwargs)


def test_clear_and_retry_require_fresh_verification_without_experiment_or_library(tmp_path):
    context = fixture(tmp_path)
    clear(context); assert context.values == {'unrelated':'retained'}
    first = len(context.observed)
    context.values['next_entry'] = 'candidate'
    clear(context)
    assert len(context.observed) > first and context.values == {'unrelated':'retained'}
    assert [call[2] for call in context.calls] == ['unset','list','unset','list']


@pytest.mark.parametrize('change', ['absent','readonly','executable','wrong-device','subroot','wrong-fs','duplicate','nested','layout','relative','major-minor'])
def test_bad_mounts_or_layout_never_invoke_grub(tmp_path, change):
    context=fixture(tmp_path); mounts=context.mounts
    if change=='absent': mounts=''
    elif change=='readonly': mounts=mounts.replace('rw,nosuid','ro,nosuid')
    elif change=='executable': mounts=mounts.replace(',noexec','')
    elif change=='wrong-device': mounts=mounts.replace(str(context.layout.partitions[2].path),str(context.layout.partitions[0].path))
    elif change=='subroot': mounts=mounts.replace(' / ', ' /subdir ')
    elif change=='wrong-fs': mounts=mounts.replace(' - vfat ', ' - ext4 ')
    elif change=='duplicate': mounts+=mounts
    elif change=='nested': mounts+=f'2 1 0:8 / {context.env.parent} rw - ext4 {context.layout.partitions[0].path} rw\n'
    elif change=='major-minor': mounts=mounts.replace(f'{context.layout.partitions[2].major_minor[0]}:{context.layout.partitions[2].major_minor[1]}','9:9')
    elif change=='layout': context.layout.partitions.pop()
    elif change=='relative': context.state=Path('relative-state')
    with pytest.raises(BootError): clear(context,mountinfo=mounts)
    assert not context.calls


@pytest.mark.parametrize('change',['file-link','dir-link','hardlink','short','fifo'])
def test_unsafe_state_never_mutates(tmp_path,change):
    context=fixture(tmp_path)
    if change=='file-link':
        original=context.env.with_name('original'); context.env.rename(original);context.env.symlink_to(original)
    elif change=='dir-link':
        original=context.env.parent.with_name('original'); context.env.parent.rename(original);context.env.parent.symlink_to(original,target_is_directory=True)
    elif change=='hardlink': os.link(context.env,context.env.with_name('alias'))
    elif change=='short':context.env.write_bytes(b'bad')
    elif change=='fifo':context.env.unlink();os.mkfifo(context.env,0o600)
    with pytest.raises((BootError,OSError)):clear(context)
    assert not context.calls


@pytest.mark.parametrize('raw',['next_entry=','candidate_id=stale','target_uuid=stale','unrelated=one\nunrelated=two','malformed','x=\x00','x=é','x='+'z'*4097])
def test_uncleared_or_invalid_native_response_is_not_success(tmp_path,raw):
    context=fixture(tmp_path)
    def run(argv):
        if argv[2]=='list':return raw
        return context.run(argv)
    with pytest.raises(BootError):clear(context,runner=run)


@pytest.mark.parametrize('phase',['unset','list'])
def test_replaced_state_blocked_after_native_command(tmp_path,phase):
    context=fixture(tmp_path)
    def run(argv):
        result=context.run(argv)
        if argv[2]==phase:
            staged=context.env.with_name('replacement');staged.write_bytes(b' '*1024);staged.chmod(0o600);staged.replace(context.env)
        return result
    with pytest.raises(BootError,match='replaced'):clear(context,runner=run)


def test_identity_loss_after_unset_blocks_receipt(tmp_path):
    context=fixture(tmp_path)
    def verify(*args,**kwargs):
        if context.calls:raise BootError('changed device')
        return context.verify(*args,**kwargs)
    with pytest.raises(BootError,match='changed device'):clear(context,identity_verifier=verify)
    assert [call[2] for call in context.calls]==['unset']


def test_native_failure_and_sync_failure_remain_unconfirmed(tmp_path,monkeypatch):
    context=fixture(tmp_path)
    def fail(argv):raise subprocess.CalledProcessError(1,argv)
    with pytest.raises(subprocess.CalledProcessError):clear(context,runner=fail)
    def sync_fail(_):raise OSError('sync failure')
    monkeypatch.setattr(boot.os,'fsync',sync_fail)
    with pytest.raises(OSError,match='sync failure'):clear(context)
    assert [call[2] for call in context.calls]==['unset']


def test_runtime_disarm_reuses_helper_between_storage_fences(monkeypatch):
    events=[]
    monkeypatch.setattr(runtime,'clear_once',lambda config,runner:events.append(('clear',config,runner)))
    command=lambda argv:None
    runtime.UsbBootControl(CONFIG,'recovery',runner=command,verify_storage=lambda:events.append('verify')).recover()
    assert events==['verify',('clear',CONFIG,command),'verify']
    events.clear()
    def failed():raise OSError('evidence changed')
    with pytest.raises(OSError):runtime.UsbBootControl(CONFIG,'recovery',runner=command,verify_storage=failed).recover()
    assert not events


def test_descriptor_device_must_match_native_partition(tmp_path):
    context=fixture(tmp_path);dev=context.layout.partitions[2].major_minor
    context.layout.partitions[2].major_minor=(9,9)
    with pytest.raises(BootError,match='verified p3'):clear(context,mountinfo=context.mounts.replace(f'{dev[0]}:{dev[1]}','9:9'))
    assert not context.calls


def test_native_list_cannot_hide_in_place_environment_change(tmp_path):
    context=fixture(tmp_path)
    def run(argv):
        result=context.run(argv)
        if argv[2]=='list':context.env.write_bytes(b'x'*1024)
        return result
    with pytest.raises(BootError,match='changed during confirmation'):clear(context,runner=run)


def test_separate_private_vfat_masks_supported(tmp_path):
    context=fixture(tmp_path)
    clear(context,mountinfo=context.mounts.replace('umask=0077','dmask=0077,fmask=0177'))
    assert context.values=={'unrelated':'retained'}
