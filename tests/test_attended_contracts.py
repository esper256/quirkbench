import json
from pathlib import Path
import pytest,jsonschema
from quirkbench.attended_contracts import validate
from quirkbench.contracts import ContractError

ROOT=Path(__file__).resolve().parents[1]

def test_attended_example_and_schema_are_strict_before_admission():
    value=json.loads((ROOT/'examples/attended-baseline-input.json').read_bytes())
    schema=json.loads((ROOT/'schemas/attended-baseline-input.v1.schema.json').read_bytes())
    jsonschema.Draft202012Validator(schema).validate(value)
    assert validate(value)==value
    for change in ({'schema_version':True},{'unknown':None},{'base_oid':'f'*39},{'recipe_id':'arbitrary-shell'},{'deployment_sha256':'x'*64}):
        with pytest.raises(ContractError):validate({**value,**change})
