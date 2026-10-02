"""Reader/asset compatibility with synthetic stock metadata; no image construction."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from quirkbench import controller_release
from quirkbench.contracts import ContractError, canonical, digest
from quirkbench.controller_release import INTERFACES, load_statement, validate_statement, verify_assets, verify_release
from quirkbench.recovery_release import FACTORY_IMAGE_FIELDS
from quirkbench.recovery_stock_release import FIELDS_V2, validate_candidate
from quirkbench.recovery_stock import POLICY
from quirkbench.image import partition_layout
from test_controller_release import inputs, fake_gpg, FINGERPRINT, ROOT
from test_recovery_podman import builder_archive, IMAGE


def test_successor_interfaces_are_exact_and_v1_remains_readable():
    value = json.loads((ROOT / 'examples/controller-release-set.v2.json').read_text())
    schema = json.loads((ROOT / 'schemas/controller-release-set.v2.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    assert load_statement(canonical(value) + b'\n') == value
    for key in INTERFACES:
        for version in (True, 99):
            patch = {**value, 'interfaces': {**INTERFACES, key: version}}
            with pytest.raises(ContractError, match='interfaces'):
                validate_statement(patch)
            with pytest.raises(ValidationError):
                Draft202012Validator(schema).validate(patch)
    with pytest.raises(ContractError):
        validate_statement({**value, 'interfaces': {**INTERFACES, 'new': 1}})
    assert load_statement(canonical(json.loads((ROOT / 'examples/controller-release-set.json').read_text())) + b'\n')['schema_version'] == 1


@pytest.fixture
def asset_set(tmp_path, monkeypatch, inputs):
    root = tmp_path / 'stock'; root.mkdir()
    candidate = {k: 'a' * 64 for k in FIELDS_V2}
    candidate.update(schema_version=2, record_type='recovery-release-candidate', qualification_status='unqualified',
        qualified_capabilities=[], architecture='x86_64', fedora_release='44', recipe_id='stock-fixture',
        kernel_origin='stock-rpm', kernel_release='6.18.0-1.fc44.x86_64', policy=POLICY,
        builder_image_digest='sha256:' + 'c' * 64,
        layout={'factory_size_mib':4096,'root_mib':2048,'experiment_mib':128,'library_mib':128,'log_budget_mib':64},
        image_size_bytes=4096*1024**2, rootfs_file_bytes=1024, rootfs_entry_count=1,
        rootfs_required_bytes=2048, esp_payload_bytes=1024, esp_required_bytes=1024+64*1024**2)
    identity = {'schema_version':2}
    for i, name in enumerate(('disk_guid','esp_partuuid','root_partuuid','state_partuuid','data_partuuid','library_partuuid','evidence_partuuid'), 1):
        identity[name] = f'00000000-0000-0000-0000-{i:012d}'
    parts = partition_layout(4096,2048)
    for part, key in zip(parts, ('esp_partuuid','root_partuuid','state_partuuid','data_partuuid')):
        part['partuuid'] = identity[key]
    manifest = {k:None for k in FACTORY_IMAGE_FIELDS}
    manifest.update(schema_version=2, layout_version=2, commissioned=False, smoke=False, deployment_backend='ostree',
        boot_policy='synthetic fixture', identity=identity, partitions=parts,
        image_sha256=candidate['image_sha256'],size_bytes=candidate['image_size_bytes'],
        recovery_kernel_sha256=candidate['kernel_sha256'], recovery_initramfs_sha256=candidate['initramfs_sha256'],
        recovery_profile_digest=candidate['profile_digest'], recovery_kernel_release=candidate['kernel_release'],
        builder_identity=candidate['image_builder_identity'], input_identity=candidate['image_input_identity'],
        commissioning={'schema_version':2,'disk_guid':identity['disk_guid'],
            'partition_uuids':[p['partuuid'] for p in parts]+[identity['library_partuuid'],identity['evidence_partuuid']],
            'partition_starts':[p['start'] for p in parts],'fixed_ends':[p['end'] for p in parts[:3]],
            'experiment_mib':128,'library_mib':128,'log_budget_mib':64})
    candidate['image_manifest_sha256'] = digest(canonical(manifest))
    validate_candidate(candidate)
    image_path = root/'factory.img'; image_path.write_bytes(b'synthetic image placeholder')
    manifest_path = root/'factory.json'; manifest_path.write_bytes(canonical(manifest))
    values = {'recovery_image': image_path, 'recovery_manifest': manifest_path}
    for name, raw in [('recovery_candidate', canonical(candidate)),
                      ('baseline_catalog', (ROOT / 'src/quirkbench/baselines/catalog.v1.json').read_bytes()),
                      ('builder_archive', builder_archive())]:
        path = tmp_path / (name + '.fixture'); path.write_bytes(raw); values[name] = path
    value = json.loads((ROOT / 'examples/controller-release-set.v2.json').read_text())
    value.update(builder_image_digest=candidate['builder_image_digest'], builder_config_digest=IMAGE,
                 controller_archive_sha256=digest(inputs[0].read_bytes()))
    for name, path in values.items():
        value[name + '_sha256'] = digest(path.read_bytes())
    # Synthetic fixture records a 4GiB image identity without writing/assembling an image.
    value['recovery_image_sha256'] = candidate['image_sha256']
    original = controller_release._asset_digest
    def measured(path, **kwargs):
        if Path(path) == image_path:
            return {'path': str(path), 'sha256': candidate['image_sha256'], 'size_bytes': candidate['image_size_bytes']}
        return original(path, **kwargs)
    monkeypatch.setattr(controller_release, '_asset_digest', measured)
    return value, values, candidate, manifest


def test_stock_factory_catalog_and_native_oci_readers_are_reused(asset_set, inputs):
    statement, assets, _, _ = asset_set
    raw = canonical(statement) + b'\n'
    receipt = verify_release(inputs[0], raw, digest(raw).encode(), inputs[1], FINGERPRINT,
                             run=fake_gpg, assets=assets)
    assert receipt['asset_compatibility_checked'] and receipt['other_release_assets_verified']
    assert not receipt['asset_compatibility_qualified'] and receipt['qualification_status'] == 'unqualified'
    assert set(receipt['assets']) == set(assets)


@pytest.mark.parametrize('change', ['empty_catalog', 'unknown_catalog', 'commissioned', 'geometry',
                                    'candidate_version', 'builder_config', 'builder_layer', 'manifest_identity'])
def test_signed_matching_bytes_still_reject_incompatible_native_assets(asset_set, change):
    statement, assets, candidate, manifest = asset_set
    if change in ('empty_catalog', 'unknown_catalog'):
        catalog = json.loads(assets['baseline_catalog'].read_bytes())
        if change == 'empty_catalog': catalog['entries'] = []
        else: catalog['schema_version'] = 99
        assets['baseline_catalog'].write_bytes(canonical(catalog))
    elif change in ('commissioned', 'geometry', 'manifest_identity'):
        if change == 'commissioned': manifest['commissioned'] = True
        elif change == 'geometry': manifest['partitions'][1]['start'] += 1
        else: manifest['image_sha256'] = '0' * 64
        assets['recovery_manifest'].write_bytes(canonical(manifest))
        candidate['image_manifest_sha256'] = digest(assets['recovery_manifest'].read_bytes())
        assets['recovery_candidate'].write_bytes(canonical(candidate))
    elif change == 'candidate_version':
        candidate['schema_version'] = 99
        assets['recovery_candidate'].write_bytes(canonical(candidate))
    elif change == 'builder_config': statement['builder_config_digest'] = 'sha256:' + '0' * 64
    elif change == 'builder_layer': assets['builder_archive'].write_bytes(builder_archive(changed_layer=True))
    for name, path in assets.items():
        if name != 'recovery_image': statement[name + '_sha256'] = digest(path.read_bytes())
    with pytest.raises((ContractError, ValueError)):
        verify_assets(statement, assets)


def test_missing_or_byte_changed_assets_rejected(asset_set):
    statement, assets, _, _ = asset_set
    with pytest.raises(ContractError, match='exact'):
        verify_assets(statement, {k: v for k, v in assets.items() if k != 'recovery_candidate'})
    assets['baseline_catalog'].write_bytes(b'changed')
    with pytest.raises(ContractError, match='differs'):
        verify_assets(statement, assets)
