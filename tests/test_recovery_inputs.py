"""Exact acquisition planning and signed input retention; no package download."""
from pathlib import Path

import pytest
from quirkbench.build import BuildError
from quirkbench.contracts import canonical,digest
from quirkbench.recovery_inputs import acquisition_command,generate_recipe,retain_packages
from quirkbench.recovery_stock import preflight_recipe
from test_recovery_stock import stock_fixture


def test_acquisition_uses_recorded_nevras_and_exact_stock_kernel(tmp_path):
    argv=acquisition_command(tmp_path/'download')
    assert 'kernel-core-7.2.7-200.fc44.x86_64' in argv
    assert 'kernel-modules-extra-7.2.7-200.fc44.x86_64' in argv
    assert 'systemd-0:259.9-1.fc44.x86_64' in argv
    assert 'systemd' not in argv and '--setopt=install_weak_deps=False' in argv
    with pytest.raises(BuildError): acquisition_command(tmp_path/'download',kernel='7.2.8-200.fc44.x86_64')


def test_default_recipe_is_v2_and_independent_of_candidate_sources(tmp_path):
    recipe,lock,_,store=stock_fixture(tmp_path)
    generated=generate_recipe(recipe['rootfs_lock_sha256'],store,recipe_id='new-stock',
        builder_image_digest=lock['builder_image_digest'],source_date_epoch=recipe['source_date_epoch'],layout=recipe['layout'])
    assert generated['schema_version']==2 and 'baseline_id' not in generated
    assert preflight_recipe(generated,store)['rootfs_lock']==lock


def test_omitted_recipe_identity_comes_from_selected_lock(tmp_path):
    from quirkbench.cli import parser
    recipe, lock, _, store = stock_fixture(tmp_path)
    args = parser().parse_args(['recovery-inputs', 'recipe', '--lock', recipe['rootfs_lock_sha256'],
                               '--builder-image-digest', lock['builder_image_digest'], '--epoch', '0'])
    assert args.id is None
    generated = generate_recipe(recipe['rootfs_lock_sha256'], store, recipe_id=args.id,
        builder_image_digest=lock['builder_image_digest'], source_date_epoch=0, layout=recipe['layout'])
    assert generated['recipe_id'] == 'stock-recovery-' + recipe['rootfs_lock_sha256']
    assert preflight_recipe(generated, store)['rootfs_lock'] == lock


def retained(tmp_path,monkeypatch,*,bad_signature=False):
    _,lock,_,store=stock_fixture(tmp_path)
    directory=tmp_path/'download'; directory.mkdir()
    names=['kernel-core','kernel-modules-core','kernel-modules','linux-firmware']
    for name in names: (directory/(name+'.rpm')).write_bytes(name.encode())
    key=tmp_path/'key'; key.write_bytes(b'pinned key')
    import quirkbench.recovery_inputs as inputs
    monkeypatch.setattr(inputs,'recorded_packages',lambda:[{'name':'linux-firmware',
        'nevra':'linux-firmware-0:'+lock['kernel_release'],'sha256':digest(b'linux-firmware')}])
    def query(argv):
        name=Path(argv[-1]).read_text()
        return name+'\t'+name+'-0:'+lock['kernel_release']+'\n'
    def signature(argv,timeout):
        if argv[0]=='gpg': return 'pub:-:4096:1:KEY:0:0::-:::scESC:\nfpr:::::::::'+('A'*40)+':\n'
        if argv[0]=='rpmkeys' and '--checksig' in argv:
            return 'NOKEY' if bad_signature else 'digests signatures OK'
        return ''
    return retain_packages(directory,key,store,tmp_path/'diagnostics',
                           builder_image_digest=lock['builder_image_digest'],fingerprint='A'*40,
                           query=query,signature_runner=signature)


def test_retained_lock_only_published_after_signature_validation(tmp_path,monkeypatch):
    lock=retained(tmp_path,monkeypatch)
    assert lock['schema_version']==2
    assert (tmp_path/'diagnostics/verified-lock.json').is_file()


def test_unsigned_inputs_retain_diagnostics_without_usable_lock(tmp_path,monkeypatch):
    with pytest.raises(BuildError,match='signature'): retained(tmp_path,monkeypatch,bad_signature=True)
    assert (tmp_path/'diagnostics').is_dir()
    assert not (tmp_path/'diagnostics/verified-lock.json').exists()


@pytest.mark.parametrize('error', [BuildError('selected inputs unavailable'),
                                  TimeoutError('Recovery package download deadline exceeded; diagnostic=/retained/log')])
def test_acquisition_cli_reports_expected_failure_without_traceback(tmp_path, monkeypatch, capsys, error):
    import quirkbench.recovery_inputs as inputs
    def fail(root, owner):
        raise error
    monkeypatch.setattr(inputs, 'download', fail)
    assert inputs.main(['--state', str(tmp_path/'state'), '--owner', 'selected']) == 2
    output = capsys.readouterr()
    assert not output.out
    assert str(error) in output.err and 'Traceback' not in output.err


@pytest.mark.parametrize('failure', ['missing', 'deadline'])
@pytest.mark.parametrize('argv,phase', [
    (['rpm', '-qp'], 'RPM header query'),
    (['gpg', '--import'], 'signing-key fingerprint inspection'),
    (['rpmkeys', '--import'], 'private RPM database key import'),
    (['rpmkeys', '--checksig'], 'RPM signature verification'),
])
def test_lock_cli_reports_native_phase_and_retained_log(tmp_path, monkeypatch, capsys, failure, argv, phase):
    import sys
    import quirkbench.recovery_inputs as inputs
    import quirkbench.recovery_acquisition as acquisition
    import quirkbench.retention as retention
    import quirkbench.ostree as ostree
    from quirkbench.cli import main
    from quirkbench.controller import Controller
    root = tmp_path/'state'
    Controller(root, reserve_bytes=0)
    # Isolate CLI/native-runner error handling after acquisition has been admitted.
    monkeypatch.setattr(retention, 'verified_acquisition', lambda *args: None)
    monkeypatch.setattr(acquisition, 'completed_spec', lambda *args: None)
    def retain(*args, query, signature_runner, **kwargs):
        if argv[0] == 'rpm':
            return query(argv)
        return signature_runner(argv, 30)
    monkeypatch.setattr(inputs, 'retain_packages', retain)
    tools = tmp_path/'tools'; tools.mkdir()
    monkeypatch.setenv('PATH', str(tools))
    if failure == 'deadline':
        tool = tools/argv[0]
        tool.write_text('#!'+sys.executable+'\nimport sys,time\nsys.stderr.write("private failure detail")\nsys.stderr.flush()\ntime.sleep(30)\n')
        tool.chmod(0o755)
        runner = ostree.CommandRunner
        def short_runner(*args, **kwargs):
            kwargs['timeout_s'] = 0.1
            return runner(*args, **kwargs)
        monkeypatch.setattr(ostree, 'CommandRunner', short_runner)
    diagnostics = root/'inputs/verification'
    assert main(['--state', str(root), 'recovery-inputs', 'lock', str(root/'inputs/rpms'),
                 '--public-key', str(tmp_path/'key'), '--builder-image-digest', 'sha256:'+'a'*64,
                 '--diagnostics', str(diagnostics)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert phase in output.err and str(diagnostics/'package-verification.log') in output.err
    assert 'private failure detail' not in output.err and 'NameError' not in output.err
    assert 'deadline exceeded' in output.err if failure == 'deadline' else 'could not start' in output.err
    assert (diagnostics/'package-verification.log').is_file()
    assert not list(diagnostics.rglob('verified-lock.json'))


def test_acquisition_wrapper_bootstraps_library_without_site_or_checkout(tmp_path):
    import os, shutil, subprocess, sys
    from quirkbench.recovery_inputs import acquisition_wrapper
    library=tmp_path/'installed/lib'
    source=Path(__file__).resolve().parents[1]/'src/quirkbench'
    shutil.copytree(source,library/'quirkbench',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    env=dict(os.environ);env.pop('PYTHONPATH',None);env.pop('PYTHONDONTWRITEBYTECODE',None)
    code=('import sys;sys.dont_write_bytecode=True;sys.path.insert(0,sys.argv[1]);'
          'from quirkbench.recovery_inputs import acquisition_wrapper;'
          'import json;print(json.dumps(acquisition_wrapper(sys.argv[2],"unused-owner")))')
    planned=subprocess.run([sys.executable,'-S','-c',code,str(library),str(tmp_path/'unused-state')],
        cwd=tmp_path,env=env,capture_output=True,text=True,check=True,timeout=30)
    import json
    argv=json.loads(planned.stdout)
    assert argv[0]==sys.executable and argv[3]==str(library)
    # Probe the exact generated bootstrap with module help; no acquisition runs.
    before={p.relative_to(library):p.read_bytes() for p in library.rglob('*') if p.is_file()}
    argv=[argv[0],'-S',*argv[1:4],'--help']
    result=subprocess.run(argv,cwd=tmp_path,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert '--owner' in result.stdout and '--state' in result.stdout
    assert {p.relative_to(library):p.read_bytes() for p in library.rglob('*') if p.is_file()} == before
    assert not any(library.rglob('__pycache__'))
    assert not (tmp_path/'unused-state').exists()
