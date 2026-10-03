"""Mechanical dispatch contracts precede service wiring."""
import json
from pathlib import Path
import pytest
import jsonschema
from quirkbench.proposal_dispatch_contracts import validate,load
from quirkbench.contracts import ContractError,canonical

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('kind',['proposal-dispatch-input','proposal-experiment-input'])
def test_strict_dispatch_examples_and_versioned_schemas(kind):
    value=json.loads((ROOT/'examples'/f'{kind}.json').read_bytes())
    schema=json.loads((ROOT/'schemas'/f'{kind}.v1.schema.json').read_bytes())
    checker=jsonschema.Draft202012Validator(schema)
    checker.validate(value);assert load(canonical(value))==value
    for bad in ({**value,'schema_version':True},{**value,'unknown':None},{**value,'proposal_sha256':'main'}):
        with pytest.raises(ContractError):validate(bad)
        with pytest.raises(jsonschema.ValidationError):checker.validate(bad)
    if kind=='proposal-dispatch-input':
        human={**value,'action':'needs_human','candidate_operation_id':None,'repository':None,'signing_fingerprint':None}
        checker.validate(human);validate(human)
        with pytest.raises(ContractError):validate({**human,'candidate_operation_id':'unwanted'})
