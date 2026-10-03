"""Versioned shutdown records never invent upload or physical-power proof."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.contracts import ContractError,canonical
from quirkbench.shutdown_local import validate_record
from quirkbench.target_shutdown import validate_intent,validate_preparation,validate_public

ROOT=Path(__file__).resolve().parents[1]
EXAMPLES=json.loads((ROOT/'examples/shutdown.json').read_bytes())
SCHEMA=json.loads((ROOT/'schemas/shutdown.v1.schema.json').read_bytes())


def validate(value):
    kind=value.get('record_type')
    return (validate_intent(value) if kind=='target-shutdown-intent' else
        validate_preparation(value) if kind=='target-shutdown-preparation' else
        validate_record(value) if kind=='recovery-shutdown' else validate_public(value))


@pytest.mark.parametrize('example',EXAMPLES,ids=lambda item:item['record_type'])
def test_examples_and_unknown_fields_match_runtime_and_schema(example):
    Draft202012Validator.check_schema(SCHEMA);Draft202012Validator(SCHEMA).validate(example)
    assert validate(example)==example
    for patch in ({'schema_version':True},{'unexpected':'never an authority'},{'request_id':'../escape'}):
        with pytest.raises(ContractError):validate(example|patch)
        assert not Draft202012Validator(SCHEMA).is_valid(example|patch)
    assert b'device_token' not in canonical(example)


@pytest.mark.parametrize('patch',[{'sealed_records':True},{'pending_upload_records':3},{'one_shot_cleared':False},
    {'local_evidence_durable':False},{'physical_poweroff_verified':True},{'safe_removal_verified':True}])
def test_invalid_preparation_and_fabricated_physical_proof_are_rejected(patch):
    with pytest.raises(ContractError):validate_preparation(EXAMPLES[1]|patch)


@pytest.mark.parametrize('change',['request','boot','progress','intent','path','source'])
def test_changed_continuation_is_not_a_retry(change):
    value=copy.deepcopy(EXAMPLES[-1])
    if change=='request':value['request_id']='other'
    elif change=='boot':value['boot_id']='other'
    elif change=='progress':value['completed_steps']=['retained','evidence_sealed']
    elif change=='intent':value['preparation']['intent_sha256']='f'*64
    elif change=='path':value['control_root']='/a/../b'
    else:value['source_sha256']={'../runtime.json':'f'*64}
    with pytest.raises(ContractError):validate_record(value)
