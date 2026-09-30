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
