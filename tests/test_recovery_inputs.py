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


def test_acquisition_wrapper_bootstraps_library_without_site_or_checkout(tmp_path):
    import os, shutil, subprocess, sys
    from quirkbench.recovery_inputs import acquisition_wrapper
    library=tmp_path/'installed/lib'
    source=Path(__file__).resolve().parents[1]/'src/quirkbench'
    shutil.copytree(source,library/'quirkbench',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    code=('import sys;sys.path.insert(0,sys.argv[1]);'
          'from quirkbench.recovery_inputs import acquisition_wrapper;'
          'import json;print(json.dumps(acquisition_wrapper(sys.argv[2],"unused-owner")))')
    planned=subprocess.run([sys.executable,'-S','-c',code,str(library),str(tmp_path/'unused-state')],
        cwd=tmp_path,env=env,capture_output=True,text=True,check=True,timeout=30)
    import json
    argv=json.loads(planned.stdout)
    assert argv[0]==sys.executable and argv[3]==str(library)
    # Probe the exact generated bootstrap with module help; no acquisition runs.
    argv=[argv[0],'-S',*argv[1:4],'--help']
    result=subprocess.run(argv,cwd=tmp_path,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert '--owner' in result.stdout and '--state' in result.stdout
    assert not (tmp_path/'unused-state').exists()
