import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.contracts import ContractError, canonical, digest
from quirkbench.deployment import DeploymentManifest, PreparedDeployment


def sample():
    return json.loads(Path('examples/deployment.json').read_text())


def test_manifest_contract_matches_schema_and_identity():
    value = sample()
    Draft202012Validator(json.loads(Path('schemas/deployment.v1.schema.json').read_text())).validate(value)
    manifest = DeploymentManifest.from_dict(value)
    assert manifest.to_dict() == value
    assert manifest.sha256 == digest(canonical(value))


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'backend': 'legacy-bundle'}, {'revision': 'latest'},
    {'repository': 'https://untrusted.invalid'}, {'provenance': {}},
    {'protection_profile': '../unsafe'}, {'surprise': 'field'},
])
def test_unusable_or_ambiguous_manifests_rejected(change):
    with pytest.raises(ContractError):
        DeploymentManifest.from_dict({**sample(), **change})


def test_prepared_identity_is_local_and_binds_attempt(tmp_path):
    manifest = DeploymentManifest.from_dict(sample())
    prepared = PreparedDeployment('b'*64, 'attempt-1', manifest.sha256, manifest.revision, tmp_path / 'entry.conf')
    assert prepared.attempt_id == 'attempt-1'
    with pytest.raises(ContractError):
        PreparedDeployment('b'*64, 'attempt-1', manifest.sha256, manifest.revision, Path('relative'))
