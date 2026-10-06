"""Fast geometry regressions; no physical disks, mounts or image builds."""
from copy import deepcopy

import pytest

from quirkbench.commission import CommissionError, CommissionIdentity
from quirkbench.prepared_media import record, validate, confirmation, plan_layout


@pytest.fixture
def factory():
    uuids = tuple(f'{i:08x}-2222-3333-4444-555555555555' for i in range(1, 7))
    return CommissionIdentity('11111111-1111-1111-1111-111111111111', uuids,
                              (2048, 526336, 4720640, 4786176),
                              (526335, 4720639, 4786175))


@pytest.mark.parametrize('size', [32_000_000_000, 64 * 1024**3, 128 * 1024**3])
def test_actual_bytes_split_without_ram_gate(factory, size):
    value = record(factory, 'a'*64, size, factory_data_end=8388574)
    assert value['complete'] is False
    assert value['library_payload_bytes'] == 0
    assert value['library_overhead_bytes'] == 16 * 1024**2
    geometry = value['geometry']
    assert geometry[:3] == [[a, b] for a, b in zip(factory.partition_starts, factory.fixed_ends)]
    assert geometry[4][1] - geometry[4][0] + 1 == 16 * 2048
    lengths = [b-a+1 for a, b in geometry]
    assert abs(lengths[3] - lengths[5]) <= 2048
    assert all(a % 2048 == 0 and (b+1) % 2048 == 0 for a, b in geometry)
    assert all(geometry[i][1] < geometry[i+1][0] for i in range(5))
    assert geometry[-1][1] < size // 512 - 33
    assert confirmation(value) == confirmation(deepcopy(value))
    assert confirmation(value) != confirmation({**value, 'complete': True})


def test_real_library_bytes_change_budget_only_by_payload(factory):
    empty = plan_layout(factory, 32_000_000_000, factory_data_end=8388574)
    loaded = plan_layout(factory, 32_000_000_000, factory_data_end=8388574, library_payload_bytes=20*1024**2)
    assert loaded[4][1]-loaded[4][0] == empty[4][1]-empty[4][0] + 20*2048
    assert loaded[3][1] < empty[3][1]


@pytest.mark.parametrize('field,value', [('device_bytes', True), ('complete', 1),
    ('artifact_sha256', 'not-a-digest'), ('library_payload_bytes', -1),
    ('library_overhead_bytes', 32*1024**3), ('schema_version', True)])
def test_record_substitution_rejected(factory, field, value):
    original = record(factory, 'a'*64, 32_000_000_000, factory_data_end=8388574)
    with pytest.raises((CommissionError, ValueError)):
        validate({**original, field:value}, factory=factory, expected_artifact_sha256='a'*64, factory_data_end=8388574)


def test_geometry_identity_and_unknown_field_substitution(factory):
    original = record(factory, 'a'*64, 32_000_000_000, factory_data_end=8388574)
    for key, replacement in [('geometry', [[0, 1]]*6), ('disk_guid', 'b'*36),
                             ('partition_uuids', ['b']*6), ('target_ram_mib', 8192)]:
        with pytest.raises(CommissionError):
            validate({**original, key:replacement}, factory=factory, expected_artifact_sha256='a'*64, factory_data_end=8388574)


def test_minimum_filesystems_and_alignment(factory):
    for size in (1024**3, 32_000_000_001):
        with pytest.raises(CommissionError):
            plan_layout(factory, size, factory_data_end=8388574)


def test_valid_digest_replacement_and_factory_shrink_rejected(factory):
    original = record(factory, 'a'*64, 32_000_000_000, factory_data_end=8388574)
    with pytest.raises(CommissionError, match='selected artifact'):
        validate({**original, 'artifact_sha256':'b'*64}, factory=factory,
                 expected_artifact_sha256='a'*64, factory_data_end=8388574)
    with pytest.raises(CommissionError, match='cannot be shrunk'):
        plan_layout(factory, 5*1024**3, factory_data_end=8388574)
