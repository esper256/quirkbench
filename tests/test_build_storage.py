"""User-selected build destinations do not require a special mount layout."""
from pathlib import Path
import pytest
from quirkbench.build import BuildError, _safe_build_path, user_build_path


@pytest.mark.parametrize('location',['checkout/build','volume/project','scratch/new/output'])
def test_explicit_user_build_path_preserves_existing_files_and_permissions(tmp_path,location):
    (tmp_path/'.git').mkdir();(tmp_path/'.git/HEAD').write_text('ref: refs/heads/main\n')
    parent=tmp_path/location;parent.mkdir(parents=True,mode=0o775)
    unrelated=parent/'important';unrelated.write_text('keep')
    assert user_build_path(parent/'new-build')==parent/'new-build'
    assert unrelated.read_text()=='keep'
    assert not (parent/'new-build').exists()


def test_ancestor_alias_is_resolved_before_work_starts(tmp_path):
    real=tmp_path/'volume';real.mkdir()
    alias=tmp_path/'storage';alias.symlink_to(real,target_is_directory=True)
    assert user_build_path(alias/'build')==real/'build'
    # The retained destination does not follow a subsequent alias replacement.
    selected=user_build_path(alias/'build')
    alias.unlink();alias.symlink_to('/etc',target_is_directory=True)
    assert selected==real/'build'
    with pytest.raises(BuildError):user_build_path(alias/'build')


def test_leaf_alias_cannot_select_an_existing_output(tmp_path):
    real=tmp_path/'real';real.mkdir()
    alias=tmp_path/'output';alias.symlink_to(real,target_is_directory=True)
    with pytest.raises(BuildError):user_build_path(alias)


@pytest.mark.parametrize('path',['/mnt/volume/project/build','/media/user/disk/build','/var/tmp/build','/srv/build/project'])
def test_admission_does_not_require_a_distribution_specific_mount_or_owner(path):
    _safe_build_path(Path(path))


@pytest.mark.parametrize('path',['/','/var','/mnt','/media','/var/lib/quirkbench',
    '/dev/shm/quirkbench','/proc/build','/sys/build','/run/build','/etc/build',
    '/usr/build','/boot/build','/lib/build','/mnt/../etc/build'])
def test_system_destinations_are_rejected(path):
    with pytest.raises(BuildError):_safe_build_path(Path(path))


def test_external_output_admission_does_not_grant_managed_cleanup(tmp_path):
    from quirkbench.retention import managed_path
    from quirkbench.contracts import ContractError
    root=tmp_path/'state';root.mkdir()
    output=user_build_path(tmp_path/'checkout/build')
    with pytest.raises(ContractError,match='beneath'):
        managed_path(root,output)


def test_image_export_never_overwrites_or_disposes_user_content(tmp_path):
    from quirkbench.image import export_image, ImageError
    source=tmp_path/'stage/image.raw';source.parent.mkdir()
    output=tmp_path/'checkout/build/image.raw';output.parent.mkdir(parents=True)
    keep=output.parent/'source.c';keep.write_text('keep')
    for suffix in ('', '.json', '.sha256'):
        Path(str(source)+suffix).write_text('artifact'+suffix)
    assert export_image(source,output)==Path(str(output)+'.json')
    assert output.read_text()=='artifact'
    with pytest.raises(ImageError,match='already exists'):export_image(source,output)
    assert keep.read_text()=='keep'


def test_kernel_build_resolves_selected_ancestor_alias_once(tmp_path):
    from quirkbench.build import KernelBuild
    real=tmp_path/'real';real.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(real,target_is_directory=True)
    build=KernelBuild(*(alias/name for name in ('source','objects','sysroot','output')))
    alias.unlink()
    assert build.source==real/'source' and build.output_dir==real/'output'


def test_image_cli_stages_separately_from_explicit_checkout_output(tmp_path, monkeypatch, capsys):
    import json
    from quirkbench.cli import main
    from quirkbench.controller import Controller
    checkout=tmp_path/'checkout';checkout.mkdir();(checkout/'.git').mkdir()
    state=checkout/'state';Controller(state,reserve_bytes=0)
    output=checkout/'image.raw'
    manifest=tmp_path/'inputs.json'
    manifest.write_text(json.dumps(dict(output=str(output),recovery_kernel='/unused/kernel',
        recovery_initramfs='/unused/initrd',rootfs_dir='/unused/root')))
    stages=[]
    def create(inputs):
        stages.append(inputs.output.parent)
        for suffix in ('', '.json', '.sha256'):
            Path(str(inputs.output)+suffix).write_text('artifact'+suffix)
        return Path(str(inputs.output)+'.json')
    monkeypatch.setattr('quirkbench.image.create_image',create)
    assert main(['dev', 'image', 'assemble', str(manifest), '--reserve-gib','0'], state_root=str(state))==0
    assert output.read_text()=='artifact'
    assert stages[0].is_relative_to(state/'workspaces')
    assert 'image.raw.json' in capsys.readouterr().out


def test_image_export_parent_swap_cannot_redirect_publication(tmp_path, monkeypatch):
    import os
    from quirkbench.image import export_image
    from quirkbench.contracts import ContractError
    source=tmp_path/'stage/image.raw';source.parent.mkdir()
    output=tmp_path/'destination/image.raw';output.parent.mkdir()
    moved=tmp_path/'original-destination'
    for suffix in ('', '.sha256', '.json'):Path(str(source)+suffix).write_text('artifact')
    link=os.link
    def swap(*args, **kwargs):
        output.parent.rename(moved);output.parent.mkdir()
        return link(*args, **kwargs)
    monkeypatch.setattr(os,'link',swap)
    with pytest.raises(ContractError,match='ancestor changed'):export_image(source,output)
    assert list(output.parent.iterdir())==[]
    assert (moved/'image.raw').read_text()=='artifact'
    assert list(moved.iterdir())==[moved/'image.raw']
