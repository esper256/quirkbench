"""Stock image input handoff: no image commands or media writes."""
from dataclasses import replace
import pytest
from quirkbench.build import BuildError
from quirkbench.boot import BootError
from quirkbench.image import ImageError, _input_identity
from quirkbench.recovery_image_plan import prepare_recovery_image_inputs


def prepared(tmp_path, monkeypatch):
    from test_stock_recovery_flow import prepared as stock_prepared
    recipe,lock,store,stage,result,_=stock_prepared(tmp_path)
    return None,recipe,store,stage,result['initramfs']


def plan(catalog, recipe, store, stage, record, output):
    return prepare_recovery_image_inputs(recipe,catalog,store,stage,record,output)


@pytest.mark.parametrize('change', ['recipe','kernel','initramfs','runtime','unit','enrolled',
    'network','machine','private_state','credential','oversize_root','missing_network_tool','release'])
def test_bad_staged_inputs_cannot_be_published(tmp_path,monkeypatch,change):
    catalog,recipe,store,stage,record=prepared(tmp_path,monkeypatch)
    inputs=plan(catalog,recipe,store,stage,record,tmp_path/'factory.img')
    root=stage/'rootfs'
    if change=='recipe':record={**record,'recipe_digest':'0'*64}
    elif change=='kernel':inputs.recovery_kernel.write_bytes(b'changed')
    elif change=='initramfs':inputs.recovery_initramfs.write_bytes(b'changed')
    elif change=='runtime':(root/'usr/lib/quirkbench/quirkbench/runtime.py').write_text('changed')
    elif change=='unit':(root/'etc/systemd/system/extra.service').write_text('extra')
    elif change=='enrolled':(root/'etc/quirkbench/runtime.json').write_text('{}')
    elif change=='network':
        profiles=root/'etc/NetworkManager/system-connections';profiles.mkdir(exist_ok=True)
        (profiles/'saved.nmconnection').write_text('secret')
    elif change=='machine':(root/'etc/machine-id').write_text('persistent')
    elif change=='private_state':
        state=root/'var/lib/quirkbench';state.mkdir(parents=True,exist_ok=True)
        (state/'credential').write_text('secret')
    elif change=='credential':
        keys=root/'etc/ssh';keys.mkdir(exist_ok=True);(keys/'ssh_host_rsa_key').write_text('secret')
    elif change=='oversize_root':
        with (root/'large').open('wb') as stream:stream.truncate(1800*1024**2)
    elif change=='missing_network_tool':(root/'usr/bin/nmtui').unlink()
    elif change=='release':record={**record,'kernel_stage':{**record['kernel_stage'],'kernel_release':'changed'}}
    with pytest.raises((BuildError,ImageError,BootError,ValueError)):
        plan(catalog,recipe,store,stage,record,tmp_path/'blocked.img')
    assert not (tmp_path/'blocked.img').exists()


def test_image_adapter_rechecks_profile_module_identity_and_capacity(tmp_path,monkeypatch):
    import quirkbench.image as image
    catalog,recipe,store,stage,record=prepared(tmp_path,monkeypatch)
    inputs=plan(catalog,recipe,store,stage,record,tmp_path/'factory.img')
    with pytest.raises(ImageError,match='incomplete reviewed recovery profile identity'):
        replace(inputs,recovery_module_files_digest=None).validate()
    with pytest.raises(ImageError):replace(inputs,recovery_profile_digest='0'*64).validate()
    assert _input_identity(inputs)!=_input_identity(replace(inputs,recovery_module_files_digest='0'*64))
    with (inputs.rootfs_dir/'large').open('wb') as stream:stream.truncate(1800*1024**2)
    monkeypatch.setattr(image,'_tool',lambda _:pytest.fail('image tool reached'))
    with pytest.raises(BuildError,match='root partition capacity'):image.create_image(inputs)
    assert not inputs.output.exists()
