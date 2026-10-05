"""Real producer/consumer checks, no PID 1, block devices, image build or VM."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from ci.native_recovery import verify
from quirkbench.image import grub_config
from quirkbench.recovery_storage import ROOT_GENERATOR, install_guard

VENDOR = '/usr/lib/systemd/system-generators/systemd-fstab-generator'
WRAPPER = '/etc/systemd/system-generators/systemd-fstab-generator'
OUTPUTS = ['/run/systemd/generator', '/run/systemd/generator.early', '/run/systemd/generator.late']


@pytest.fixture(scope='module')
def dependency_root():
    cache = os.environ.get('QB_NATIVE_RECOVERY_CACHE')
    if not cache or not shutil.which('bwrap'):
        pytest.fail('Native dependencies unavailable: prepare ci.native_recovery cache and install bubblewrap; no tests skipped')
    try:
        root = verify(Path(cache))
        print(json.dumps({'verified_packages': json.loads((Path(cache)/'manifest.json').read_text())['packages']}))
        return root
    except (OSError, ValueError, KeyError) as exc:
        pytest.fail(f'Native dependencies unavailable: {exc}')


@pytest.fixture
def native(tmp_path, dependency_root):
    root = tmp_path/'overlay'; root.mkdir()
    install_guard(root)
    wrapper = root/WRAPPER.lstrip('/')
    wrapper.write_text(ROOT_GENERATOR); wrapper.chmod(0o755)
    ids = [str(uuid.uuid4()) for _ in range(6)]
    grub = grub_config(ids[1], esp_uuid=ids[0], root_uuid=ids[1], state_uuid=ids[2],
                       data_uuid=ids[3], library_uuid=ids[4], evidence_uuid=ids[5], stock_recovery=True)
    # Consume actual production GRUB arguments, not a second handwritten policy.
    line = next(line.strip() for line in grub.splitlines() if line.strip().startswith('linux ') and 'quirkbench.mode=recovery' in line)
    cmdline = tmp_path/'cmdline'; cmdline.write_text(line.split(' ', 2)[2])
    run = tmp_path/'run'
    for name in OUTPUTS:
        (run/Path(name).relative_to('/run')).mkdir(parents=True)
    out = tmp_path/'output'; out.mkdir()
    for name in ('normal', 'early', 'late'):
        (out/name).mkdir()
    def execute(argv, *, extra=(), env=None):
        command = ['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', str(dependency_root), '/',
                   '--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp',
                   '--ro-bind', str(root/'etc'), '/etc', '--ro-bind', str(run), '/run',
                   '--ro-bind', str(root/'usr/lib/quirkbench'), '/usr/lib/quirkbench',
                   '--ro-bind', str(cmdline), '/proc/cmdline']
        for source, target in zip(('normal', 'early', 'late'), OUTPUTS):
            command += ['--bind', str(out/source), target]
        command += list(extra) + ['--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                   '--setenv', 'SYSTEMD_IN_INITRD', '1', '--setenv', 'LC_ALL', 'C']
        for key, value in (env or {}).items():
            command += ['--setenv', key, value]
        result = subprocess.run(command + ['--', *argv], capture_output=True, text=True, timeout=10)
        # Log the actual consumer diagnostics, even for intentional failures.
        print(json.dumps({'argv': argv, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}))
        return result
    return execute, out, cmdline, root


def check_units(execute):
    return execute(['/usr/bin/systemd-analyze', '--generators=no', '--man=no', 'verify', 'sysroot.mount'],
                   env={'SYSTEMD_UNIT_PATH': '/etc/systemd/system:/run/systemd/generator:/usr/lib/systemd/system'})


def test_stock_conflict_and_production_adapter(native):
    execute, out, cmdline, _ = native
    raw = execute([VENDOR, *OUTPUTS], env={'SYSTEMD_PROC_CMDLINE': cmdline.read_text(),
                  'SYSTEMD_FSTAB': '/dev/null', 'SYSTEMD_SYSROOT_FSTAB': '/dev/null'})
    assert raw.returncode == 0, raw.stderr
    conflict = check_units(execute)
    assert conflict.returncode != 0 and 'systemd-fsck-root.service' in conflict.stderr
    # Clear generated files only; preserve the actual Quirkbench service masks.
    for name in ('normal', 'early', 'late'):
        shutil.rmtree(out/name); (out/name).mkdir()
    fixed = execute([WRAPPER, *OUTPUTS])
    assert fixed.returncode == 0, fixed.stderr
    checked = check_units(execute)
    assert checked.returncode == 0, checked.stderr
    mount = (out/'normal/sysroot.mount').read_text()
    print(json.dumps({'verified_sysroot_mount': mount}))
    assert 'Options=noload,ro' in mount and 'Type=ext4' in mount
    assert (out/'normal/initrd-root-fs.target.requires/sysroot.mount').resolve() == out/'normal/sysroot.mount'
    # Mutation proves we detect a prerequisite conflict, not just an old filename.
    masked = native[3]/'etc/systemd/system/required-helper.service'
    masked.symlink_to('/dev/null')
    path = out/'normal/sysroot.mount'
    path.write_text(mount.replace('[Unit]', '[Unit]\nRequires=required-helper.service'))
    result = check_units(execute)
    assert result.returncode != 0 and 'required-helper.service' in result.stderr


def test_generator_sandbox_is_enforced(native):
    execute, _, _, _ = native
    denied = execute(['/usr/bin/python3', '-I', '-S', '-c', 'import os; os.mkdir("/run/systemd/forbidden-staging")'])
    assert denied.returncode != 0 and 'Read-only file system' in denied.stderr
    valid = execute([WRAPPER, *OUTPUTS])
    assert valid.returncode == 0, valid.stderr


@pytest.mark.parametrize('fault', ['exit', 'malformed', 'extra'])
def test_real_subprocess_failure_never_publishes_partial_mount(native, tmp_path, fault):
    execute, out, _, _ = native
    fake = tmp_path/'fault-generator'
    fake.write_text('#!/bin/sh\n' + '/tmp/vendor-real "$@"\n' + {
        'exit': 'exit 7\n',
        'malformed': 'echo "ExecStart=/usr/bin/false" >> "$1/sysroot.mount"\n',
        'extra': 'echo unexpected > "$2/extra.mount"\n',
    }[fault])
    fake.chmod(0o755)
    # Run the real vendor first, then inject failure at its subprocess boundary.
    root = Path(os.environ['QB_NATIVE_RECOVERY_CACHE'])/'root'
    failed = execute([WRAPPER, *OUTPUTS], extra=('--ro-bind', str(root/VENDOR.lstrip('/')), '/tmp/vendor-real',
                     '--ro-bind', str(fake), VENDOR))
    assert failed.returncode != 0
    assert os.readlink(out/'normal/sysroot.mount') == '/dev/null'
    assert os.readlink(out/'normal/initrd-root-fs.target.requires/sysroot.mount') == '../sysroot.mount'
    assert not list((out/'early').iterdir()) and not list((out/'late').iterdir())


def test_packaged_failure_entrypoint_handles_unpublished_boot(native):
    import shlex
    from ci.native_recovery import ROOT
    from quirkbench.target_payload import TARGET_MODULES
    execute, _, _, overlay = native
    package = overlay/'usr/lib/quirkbench/quirkbench'; package.mkdir()
    for name in TARGET_MODULES:
        shutil.copyfile(ROOT/'src/quirkbench'/f'{name}.py', package/f'{name}.py')
    unit = (ROOT/'target-assets/quirkbench-supervisor-failure.service').read_text().splitlines()
    argv = shlex.split(next(line.removeprefix('ExecStart=') for line in unit if line.startswith('ExecStart=')))
    environment = dict(item.split('=', 1) for line in unit if line.startswith('Environment=')
                       for item in shlex.split(line.removeprefix('Environment=')))
    result = execute(argv, env=environment)
    assert result.returncode == 0, result.stderr
    assert 'verification is incomplete' in result.stderr and 'Traceback' not in result.stderr
    # The fixture cannot accidentally import omitted modules from the checkout.
    (package/'boot.py').unlink()
    missing = execute(argv, env=environment)
    assert missing.returncode != 0 and 'quirkbench.boot' in missing.stderr
