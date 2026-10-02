"""Additive M1a contract; retain the product CLI v1 specification unchanged."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from quirkbench.contracts import ContractError, canonical, digest
from quirkbench.setup_contracts import load_progress, validate_progress

ROOT = Path(__file__).resolve().parents[1]


def test_setup_progress_schema_and_example():
    schema = json.loads((ROOT / 'schemas/controller-setup-progress.v1.schema.json').read_text())
    value = json.loads((ROOT / 'examples/controller-setup-progress.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    assert validate_progress(value) == load_progress(canonical(value)) == value


@pytest.mark.parametrize('change', [
    {'extra': True}, {'schema_version': True}, {'schema_version': 2},
    {'completed_steps': ['preferences_recorded']}, {'request_digest': 'a' * 64},
    {'request_id': '../request'}, {'setup_id': 'other'},
])
def test_invalid_progress(change):
    value = json.loads((ROOT / 'examples/controller-setup-progress.json').read_text())
    with pytest.raises(ContractError):
        validate_progress({**value, **change})


@pytest.mark.parametrize('change', [
    {'extra': True}, {'cache_gib': True}, {'reserve_gib': float('inf')},
    {'port': 0}, {'host': 'example.test'}, {'host': '0.0.0.0'},
    {'state_root': '/tmp/../state'}, {'runtime_root': 'relative'}, {'logout_policy': 'enable_linger'},
])
def test_invalid_intent_matches_schema(change):
    value = json.loads((ROOT / 'examples/controller-setup-progress.json').read_text())
    value = copy.deepcopy(value)
    value['intent'].update(change)
    with pytest.raises(ContractError):
        validate_progress(value)
    schema = json.loads((ROOT / 'schemas/controller-setup-progress.v1.schema.json').read_text())
    with pytest.raises(ValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)


@pytest.mark.parametrize('raw', [b'{"schema_version":1,"schema_version":1}',
                               b'{"x":NaN}', b'{"x":1e999}', b'x' * 16385,
                               b'[' * 34 + b'0' + b']' * 34])
def test_bounded_parser(raw):
    with pytest.raises(ContractError):
        load_progress(raw)
