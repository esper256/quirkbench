"""Candidate identity follows installed bytes rather than controller source layout."""
from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from quirkbench import candidate_payload, compose
from quirkbench.boot import BootError
from quirkbench.target_install import install_candidate_runtime
from test_compose import inputs, fake_runner, reserve_for_small_tmpfs


@pytest.fixture
def sources(tmp_path):
    package=tmp_path/'package';assets=tmp_path/'assets'
    shutil.copytree(Path(candidate_payload.__file__).parent,package,ignore=shutil.ignore_patterns('__pycache__'))
    from quirkbench.package_resources import target_assets_dir
    shutil.copytree(target_assets_dir(),assets)
    return package,assets


def test_controller_and_unused_recovery_assets_do_not_change_identity(sources,tmp_path,monkeypatch):
    package,assets=sources
    payload=candidate_payload.capture(package,assets)
    value=inputs(tmp_path)
    monkeypatch.setattr(candidate_payload,'capture',lambda: candidate_payload_capture(package,assets))
    before=value.identity()
    (package/'controller.py').write_text('controller-only change')
    (assets/'quirkbench-recovery.service').write_text('recovery-only change')
    assert candidate_payload_capture(package,assets).identity()==payload.identity()
    assert value.identity()==before


candidate_payload_capture=candidate_payload.capture


@pytest.mark.parametrize('kind',['module','unit','recipe','generated','link','directory'])
def test_each_shipped_component_changes_identity(sources,kind):
    package,assets=sources
    before=candidate_payload.capture(package,assets)
    if kind=='module':
        with (package/'boot.py').open('a') as stream:stream.write('\n# payload edit\n')
    elif kind=='unit':
        with (assets/'quirkbench-candidate.service').open('a') as stream:stream.write('\n# selected unit edit\n')
    elif kind=='recipe':
        path=package/'recipes/system-observation.v1.json';path.write_bytes(path.read_bytes()+b'\n')
    after=candidate_payload.capture(package,assets)
    if kind=='generated':after.files['usr/etc/NetworkManager/conf.d/99-quirkbench-dns.conf']+=b'# generated change\n'
    if kind=='link':after.links['usr/etc/resolv.conf']='/run/other'
    if kind=='directory':after.directories['usr/etc/quirkbench']=0o700
    assert after.identity()!=before.identity()


def test_installation_matches_manifest_exactly_and_retains_empty_directories(tmp_path):
    payload=candidate_payload.capture();root=tmp_path/'root';root.mkdir()
    install_candidate_runtime(root,payload=payload)
    candidate_payload.audit(root,payload)
    actual={p.relative_to(root).as_posix() for p in root.rglob('*')}
    assert actual==set(payload.files)|set(payload.links)|set(payload.directories)
    spec=compose.rpm_spec('candidate','1',root)
    assert '%dir /usr/etc/NetworkManager/system-connections' in spec
    assert '%dir /usr/etc/quirkbench' in spec


@pytest.mark.parametrize('mutation',['missing','changed','extra','extra-unit','extra-dropin','linked-ancestor','link-target'])
def test_installed_mutations_fail_audit(tmp_path,mutation):
    root=tmp_path/'root';root.mkdir();payload=candidate_payload.capture()
    install_candidate_runtime(root,payload=payload)
    path=root/'usr/lib/quirkbench/quirkbench/boot.py'
    if mutation=='missing':path.unlink()
    if mutation=='changed':path.write_bytes(b'changed')
    if mutation=='extra':(path.parent/'extra.py').write_bytes(b'extra')
    if mutation=='extra-unit':(root/'usr/etc/systemd/system/quirkbench-unreviewed.service').write_bytes(b'extra')
    if mutation=='extra-dropin':(root/'usr/etc/systemd/system/quirkbench-supervisor.service.d/extra.conf').write_bytes(b'extra')
    if mutation=='linked-ancestor':
        directory=path.parent;outside=tmp_path/'outside';directory.rename(outside);directory.symlink_to(outside)
    if mutation=='link-target':
        link=root/'usr/etc/resolv.conf';link.unlink();link.symlink_to('/run/other')
    with pytest.raises(BootError):candidate_payload.audit(root,payload)


def test_installer_rejects_usr_etc_substitution_without_writing_outside(tmp_path):
    root=tmp_path/'root';root.mkdir();outside=tmp_path/'outside';outside.mkdir()
    (root/'usr').mkdir();(root/'usr/etc').symlink_to(outside)
    with pytest.raises(BootError):install_candidate_runtime(root)
    assert list(outside.iterdir())==[]


def test_policy_change_changes_build_identity_separately(tmp_path,monkeypatch):
    value=inputs(tmp_path);payload=candidate_payload.capture();before=value.identity()
    policy=compose.composition_policy()
    monkeypatch.setattr(compose,'composition_policy',lambda:{**policy,'target_install.py':'f'*64})
    assert candidate_payload.capture().identity()==payload.identity()
    assert value.identity()!=before


@pytest.mark.parametrize('mutation',['bytes','extra','policy-source'])
def test_final_tree_or_changed_policy_cannot_be_signed(tmp_path,monkeypatch,mutation):
    value=inputs(tmp_path);calls=[];fake_runner(monkeypatch,calls)
    adapter=compose.ComposeRunner.run
    policy=compose.composition_policy()
    def run(self,argv,**kwargs):
        result=adapter(self,argv,**kwargs)
        if kwargs['phase']=='checkout-candidate-runtime':
            root=Path(argv[-1])
            if mutation=='bytes':(root/'usr/lib/quirkbench/quirkbench/boot.py').write_bytes(b'changed')
            elif mutation=='extra':(root/'usr/lib/quirkbench/quirkbench/extra.py').write_bytes(b'extra')
            else:monkeypatch.setattr(compose,'composition_policy',lambda:{**policy,'target_install.py':'f'*64})
        return result
    monkeypatch.setattr(compose.ComposeRunner,'run',run)
    with pytest.raises((BootError,compose.BuildError)):
        compose.FedoraComposer(tmp_path/'workspace',tmp_path/'published').compose(value)
    assert 'sign-revision' not in [phase for phase,_ in calls]
    assert not (tmp_path/'published').exists()
