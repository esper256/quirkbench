"""One observable lifecycle contract, exercised by fake and OSTree adapters.

The OSTree cases use the existing filesystem service fixture. Real command and
VM acceptance remain separate gates; these tests never claim an OS has booted.
"""
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Callable

import pytest

from quirkbench.build import REQUIRED_CONFIG
from quirkbench.contracts import ContractError
from quirkbench.deployment import DeploymentBackend, DeploymentManifest
from quirkbench.ostree import OstreeBackend, Remote
from quirkbench.simulation import FakeDeploymentBackend
from test_ostree import Service


@dataclass
class BackendCase:
    backend: DeploymentBackend
    manifest: DeploymentManifest
    state_directory: Callable[[str], Path]
    set_active: Callable[[str | None], None]


@pytest.fixture(params=['fake', 'ostree'])
def deployment_case(request, tmp_path):
    root = tmp_path / 'target'
    root.mkdir()
    config = ''.join(f'{key}={value}\n' for key, value in REQUIRED_CONFIG.items())
    manifest = DeploymentManifest('ostree', 'a' * 64, 'lab', {
        'kernel_release': '6.12-test',
        'config_sha256': hashlib.sha256(config.encode()).hexdigest(),
        'artifact_sha256': {'kernel': hashlib.sha256(b'qualified kernel').hexdigest(),
                            'initramfs': hashlib.sha256(b'qualified initramfs').hexdigest()},
    }, 'usb-excluded-controllers-v1')
    if request.param == 'fake':
        adapter = FakeDeploymentBackend(root)
        return BackendCase(adapter, manifest, lambda attempt: root / attempt,
                           lambda attempt: setattr(adapter, 'active_attempt', attempt))
    files = []
    for name in ('ca', 'public-key', 'client-cert', 'client-key'):
        path = tmp_path / name
        path.write_text('test trust')
        files.append(path)
    service = Service(root)
    adapter = OstreeBackend(root, {'lab': Remote('https://controller.test/lab/', *files)},
                            verify_storage=lambda path: None, runner=service, reserve_bytes=0)
    # Fixture locators expose each adapter's simulated mutable state. Assertions
    # below concern contamination and lifecycle results, never command ordering.
    def mutable_state(attempt):
        return root / 'ostree/deploy' / ('qb-' + hashlib.sha256(attempt.encode()).hexdigest())
    return BackendCase(adapter, manifest, mutable_state,
                       lambda attempt: setattr(service, 'booted', attempt is not None))


def check_prepare_lifecycle(case):
    adapter, manifest = case.backend, case.manifest
    assert adapter.inspect('new-attempt') is None
    first = adapter.prepare(manifest, 'first-attempt')
    assert first.attempt_id == 'first-attempt'
    assert first.manifest_digest == manifest.sha256
    assert first.revision == manifest.revision
    assert first.boot_entry.is_file()
    assert adapter.inspect('first-attempt') == first
    assert adapter.prepare(manifest, 'first-attempt') == first
    with pytest.raises(ContractError):
        adapter.prepare(replace(manifest, revision='b' * 64), 'first-attempt')
    assert adapter.inspect('first-attempt') == first


def check_attempt_isolation(case):
    adapter, manifest = case.backend, case.manifest
    first = adapter.prepare(manifest, 'first-attempt')
    marker = case.state_directory('first-attempt') / 'var/retained-observation'
    marker.write_text('first physical execution')
    second = adapter.prepare(manifest, 'second-attempt')
    assert first.deployment_id != second.deployment_id
    assert first.revision == second.revision
    assert not (case.state_directory('second-attempt') / 'var/retained-observation').exists()
    assert marker.read_text() == 'first physical execution'
    assert adapter.prepare(manifest, 'first-attempt').deployment_id == first.deployment_id
    assert marker.read_text() == 'first physical execution'


def check_cleanup_guards(case):
    adapter, manifest = case.backend, case.manifest
    first = adapter.prepare(manifest, 'first-attempt')
    survivor = adapter.prepare(manifest, 'second-attempt')
    with pytest.raises(ContractError):
        adapter.retain(replace(first, deployment_id='c' * 64))
    with pytest.raises(ContractError):
        adapter.remove(replace(first, deployment_id='c' * 64))
    with pytest.raises(ContractError):
        adapter.remove(first)
    assert adapter.inspect(first.attempt_id) == first
    adapter.can_remove = lambda deployment: True
    case.set_active(first.attempt_id)
    with pytest.raises(ContractError):
        adapter.remove(first)
    case.set_active(None)
    adapter.remove(first)
    adapter.remove(first)
    assert adapter.inspect(survivor.attempt_id) == survivor
    with pytest.raises(ContractError):
        adapter.prepare(manifest, first.attempt_id)
    adapter.retain(survivor)
    with pytest.raises(ContractError):
        adapter.remove(survivor)
    assert adapter.inspect(survivor.attempt_id) == survivor


def test_shared_prepare_lifecycle(deployment_case):
    check_prepare_lifecycle(deployment_case)


def test_shared_attempt_isolation(deployment_case):
    check_attempt_isolation(deployment_case)


def test_shared_cleanup_guards(deployment_case):
    check_cleanup_guards(deployment_case)
