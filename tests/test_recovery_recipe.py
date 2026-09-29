"""P3a1 recipe preflight is exact and inert over synthetic retained objects."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.contracts import canonical, digest
from quirkbench.hardware_plan import installed_profiles
from quirkbench.recovery_dracut import validate_recovery_dracut_config
from quirkbench.recovery_fragment import merge_recovery_config, validate_recovery_fragment
from quirkbench.recovery_module_audit import FINAL_CONFIG
from quirkbench.recovery_recipe import (
    POLICY, load_recipe, preflight_recipe, validate_recipe,
)
from quirkbench.recovery_runtime_revision import capture_runtime_revision
from test_recovery_rootfs import locked_fixture


ROOT = Path(__file__).resolve().parents[1]


def recipe_fixture(tmp_path):
    catalog, lock, reader, store, _ = locked_fixture(tmp_path)
    entry = catalog['entries'][0]
    entry['kernel_config_sha256'] = store.put(
        b'CONFIG_USB_STORAGE=m\nCONFIG_MODULE_COMPRESS=y\nCONFIG_IWLWIFI=m\n').sha256
    entry['dracut_config_sha256'] = store.put(
        (ROOT / 'target-assets/dracut.conf').read_bytes()).sha256
    lock['recovery_fragment_sha256'] = store.put(
        (ROOT / 'target-assets/recovery-kernel.fragment').read_bytes()).sha256
    lock['baseline_digest'] = digest(canonical(entry))
    unit_document = {'schema_version': 1, 'units': [
        'quirkbench-console.service', 'quirkbench-network-state.service',
        'quirkbench-recovery.service',
        'quirkbench-supervisor-failure.service', 'quirkbench-supervisor.service',
        'tmp.mount', 'var.mount']}
    recipe = {
        'schema_version': 1, 'recipe_id': 'generic-recovery-v1',
        'baseline_id': lock['baseline_id'], 'baseline_digest': lock['baseline_digest'],
        'rootfs_lock_sha256': store.put(canonical(lock)).sha256,
        'kernel_srpm_sha256': entry['kernel_srpm_sha256'],
        'kernel_config_sha256': entry['kernel_config_sha256'],
        'recovery_fragment_sha256': lock['recovery_fragment_sha256'],
        'dracut_config_sha256': entry['dracut_config_sha256'],
        'builder_image_digest': entry['builder_image_digest'],
        'runtime_revision_sha256': store.put(canonical(capture_runtime_revision(
            ROOT / 'src/quirkbench', ROOT / 'target-assets'))).sha256,
        'unit_allowlist_sha256': store.put(canonical(unit_document)).sha256,
        'source_date_epoch': 1_700_000_000,
        'layout': {'factory_size_mib': 4096, 'root_mib': 2048,
                   'experiment_mib': 32768, 'library_mib': 32768,
                   'log_budget_mib': 4096},
        'policy': POLICY.copy(),
    }
    return catalog, recipe, reader, store


def test_recipe_schema_and_replayed_retained_closure(tmp_path):
    catalog, recipe, reader, _ = recipe_fixture(tmp_path)
    schema = json.loads((ROOT/'schemas/recovery-recipe.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(recipe)
    assert load_recipe(canonical(recipe)) == recipe
    result = preflight_recipe(recipe, catalog, reader)
    assert result['entry'] == catalog['entries'][0]
    assert result['rootfs_lock']['baseline_id'] == recipe['baseline_id']
    assert 'quirkbench-console.service' in result['unit_allowlist']
    assert result['dracut_policy']['hostonly'] is False
    assert 'xhci_hcd' in result['dracut_policy']['add_drivers']
    assert result['recovery_fragment_symbols'] == len(FINAL_CONFIG)
    staged = merge_recovery_config(
        reader.get(recipe['kernel_config_sha256']),
        reader.get(recipe['recovery_fragment_sha256']))
    assert result['staged_kernel_config_sha256'] == digest(staged)
    assert b'CONFIG_USB_STORAGE=y\n' in staged
    assert b'CONFIG_MODULE_COMPRESS=y\n' in staged
    assert b'CONFIG_IWLWIFI=m\n' in staged


@pytest.mark.parametrize('field', ['baseline_digest', 'rootfs_lock_sha256',
                                   'kernel_srpm_sha256', 'kernel_config_sha256',
                                   'recovery_fragment_sha256', 'dracut_config_sha256',
                                   'runtime_revision_sha256', 'unit_allowlist_sha256'])
def test_changed_or_missing_recipe_input_fails_closed(tmp_path, field):
    catalog, recipe, reader, _ = recipe_fixture(tmp_path)
    recipe[field] = '0' * 64
    with pytest.raises((BuildError, ValueError)):
        preflight_recipe(recipe, catalog, reader)


def test_changed_builder_and_weakened_policy_fail_before_synthesis(tmp_path):
    catalog, recipe, reader, _ = recipe_fixture(tmp_path)
    recipe['builder_image_digest'] = 'sha256:' + '0' * 64
    with pytest.raises(BuildError, match='builder_image_digest'):
        preflight_recipe(recipe, catalog, reader)
    recipe['builder_image_digest'] = catalog['entries'][0]['builder_image_digest']
    for key, value in [('root_read_only', 1), ('firmware_writes', True),
                       ('internal_storage_access', True)]:
        changed = copy.deepcopy(recipe)
        changed['policy'][key] = value
        with pytest.raises(BuildError, match='policy'):
            validate_recipe(changed)
    for value in (-1, True, 1.5):
        changed = copy.deepcopy(recipe)
        changed['source_date_epoch'] = value
        with pytest.raises(BuildError, match='SOURCE_DATE_EPOCH'):
            validate_recipe(changed)


def test_missing_reviewed_build_recipe_object_blocks_recovery_preflight(tmp_path):
    catalog, recipe, reader, _ = recipe_fixture(tmp_path)
    reader.path(catalog['entries'][0]['build_recipe']['digest']).unlink()
    with pytest.raises(BuildError, match='retained CAS object'):
        preflight_recipe(recipe, catalog, reader)


@pytest.mark.parametrize('layout', [
    {'factory_size_mib': 2048, 'root_mib': 2048, 'experiment_mib': 32768,
     'library_mib': 32768, 'log_budget_mib': 4096},
    {'factory_size_mib': 4096, 'root_mib': 2048, 'experiment_mib': 100,
     'library_mib': 32768, 'log_budget_mib': 4096},
])
def test_factory_layout_cannot_exceed_commissioning_layout(tmp_path, layout):
    _, recipe, _, _ = recipe_fixture(tmp_path)
    recipe['layout'] = layout
    with pytest.raises(BuildError, match='layout'):
        validate_recipe(recipe)


def test_recipe_rejects_duplicate_json_and_unreviewed_units(tmp_path):
    catalog, recipe, reader, store = recipe_fixture(tmp_path)
    raw = canonical(recipe).decode().replace('"schema_version":1', '"schema_version":1,"schema_version":1', 1)
    with pytest.raises(BuildError, match='invalid recovery recipe JSON'):
        load_recipe(raw.encode())
    recipe['unit_allowlist_sha256'] = store.put(canonical({
        'schema_version': 1, 'units': ['quirkbench-recovery.service', 'quirkbench-recovery.service']})).sha256
    with pytest.raises(BuildError, match='unit allowlist'):
        preflight_recipe(recipe, catalog, reader)


@pytest.mark.parametrize('line', [
    'hostonly="yes"',
    'hostonly_cmdline="$(touch /tmp/unsafe)"',
    'source /etc/dracut.conf',
    'drivers+=" nvme "',
    'add_drivers+=" usb_storage sd_mod ext4 vfat "',
    'omit_drivers+=" ahci nvme vmd megaraid_sas mpt3sas virtio_blk mmc_block ext4 "',
    'omit_drivers+=" ahci nvme virtio_blk mmc_block "',
])
def test_recovery_dracut_rejects_host_or_protection_changes(line):
    profile = installed_profiles()[0]
    valid = (ROOT / 'target-assets/dracut.conf').read_text()
    key = line.split('=', 1)[0]
    lines = [item for item in valid.splitlines() if not item.startswith(key)]
    with pytest.raises(BuildError, match='recovery dracut'):
        validate_recovery_dracut_config(('\n'.join(lines + [line]) + '\n').encode(), profile)


def test_recovery_dracut_rejects_duplicate_setting_and_shell_suffix():
    profile = installed_profiles()[0]
    valid = (ROOT / 'target-assets/dracut.conf').read_bytes()
    with pytest.raises(BuildError, match='duplicate recovery dracut'):
        validate_recovery_dracut_config(valid + b'hostonly="no"\n', profile)
    with pytest.raises(BuildError, match='unsupported setting'):
        validate_recovery_dracut_config(valid + b'echo unsafe\n', profile)


def test_retained_dracut_config_content_checked_by_recipe_preflight(tmp_path):
    catalog, recipe, reader, store = recipe_fixture(tmp_path)
    entry = catalog['entries'][0]
    entry['dracut_config_sha256'] = store.put(b'hostonly="yes"\n').sha256
    recipe['dracut_config_sha256'] = entry['dracut_config_sha256']
    recipe['baseline_digest'] = digest(canonical(entry))
    lock = json.loads(store.get(recipe['rootfs_lock_sha256']))
    lock['baseline_digest'] = recipe['baseline_digest']
    recipe['rootfs_lock_sha256'] = store.put(canonical(lock)).sha256
    with pytest.raises(BuildError, match='recovery dracut'):
        preflight_recipe(recipe, catalog, reader)


def test_reviewed_recovery_fragment_contains_only_protected_overrides():
    raw = (ROOT / 'target-assets/recovery-kernel.fragment').read_bytes()
    assert validate_recovery_fragment(raw) == FINAL_CONFIG
    assert b'CONFIG_MODULE_COMPRESS' not in raw


@pytest.mark.parametrize('change', [
    lambda text: text.replace('# CONFIG_ATA is not set', 'CONFIG_ATA=y'),
    lambda text: text.replace('CONFIG_USB_STORAGE=y\n', ''),
    lambda text: text + 'CONFIG_NEW_DRIVER=y\n',
    lambda text: text + 'CONFIG_USB_STORAGE=y\n',
    lambda text: text + 'CONFIG_ATA=$(touch /tmp/unsafe)\n',
])
def test_recovery_fragment_rejects_changed_or_unreviewed_symbols(change):
    raw = (ROOT / 'target-assets/recovery-kernel.fragment').read_text()
    with pytest.raises(BuildError, match='recovery kernel fragment'):
        validate_recovery_fragment(change(raw).encode())


def test_recipe_preflight_checks_retained_fragment_content(tmp_path):
    catalog, recipe, reader, store = recipe_fixture(tmp_path)
    lock = json.loads(store.get(recipe['rootfs_lock_sha256']))
    lock['recovery_fragment_sha256'] = store.put(b'CONFIG_ATA=y\n').sha256
    recipe['recovery_fragment_sha256'] = lock['recovery_fragment_sha256']
    recipe['rootfs_lock_sha256'] = store.put(canonical(lock)).sha256
    with pytest.raises(BuildError, match='recovery kernel fragment'):
        preflight_recipe(recipe, catalog, reader)


@pytest.mark.parametrize('base', [
    b'CONFIG_USB_STORAGE=m\nCONFIG_USB_STORAGE=y\n',
    b'CONFIG_USB_STORAGE=$(touch /tmp/unsafe)\n',
    b'CONFIG_USB_STORAGE=m\r\n',
    b'source /etc/kernel.conf\n',
])
def test_recovery_config_merge_rejects_malformed_fedora_base(base):
    fragment = (ROOT / 'target-assets/recovery-kernel.fragment').read_bytes()
    with pytest.raises(BuildError, match='Fedora kernel base config'):
        merge_recovery_config(base, fragment)


def test_recovery_config_merge_is_deterministic_and_keeps_unrelated_settings():
    fragment = (ROOT / 'target-assets/recovery-kernel.fragment').read_bytes()
    base = (b'# Fedora baseline fixture\nCONFIG_MODULE_COMPRESS=y\n'
            b'CONFIG_ATH9K=m\nCONFIG_I2C_MUX_PCA954x=m\n'
            b'# CONFIG_TESTx is not set\n# CONFIG_ATA is not set\n')
    first = merge_recovery_config(base, fragment)
    assert merge_recovery_config(first, fragment) == first
    assert b'CONFIG_MODULE_COMPRESS=y\n' in first
    assert b'CONFIG_ATH9K=m\n' in first
    assert b'CONFIG_I2C_MUX_PCA954x=m\n' in first
    assert b'# CONFIG_TESTx is not set\n' in first
    assert b'# CONFIG_ATA is not set\n' in first
    for name in ('KEXEC_HANDOVER', 'NVME_RDMA', 'NVME_FC',
                 'NVME_TCP', 'NVME_TARGET_LOOP'):
        assert f'# CONFIG_{name} is not set\n'.encode() in first
