import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from quirkbench.publication_setup_contracts import load,validate
from quirkbench.contracts import ContractError,canonical


def test_publication_schema_runtime_and_failures():
    root=Path(__file__).resolve().parents[1]
    example=json.loads((root/'examples/publication-setup.json').read_bytes())
    schema=Draft202012Validator(json.loads((root/'schemas/publication-setup.v1.schema.json').read_bytes()))
    schema.check_schema(schema.schema);schema.validate(example)
    assert load(canonical(example))==example
    for bad in ({**example,'extra':True},{**example,'schema_version':True},
            {**example,'intent':{**example['intent'],'signing_fingerprint':'bad'}},
            {**example,'request_digest':'0'*64},{**example,'completed_steps':['configuration_published']}):
        with pytest.raises(ContractError):validate(bad)
    with pytest.raises(ContractError):load(b'{"schema_version":1,"schema_version":1}')
