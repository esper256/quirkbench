"""Mechanical v2 admission records precede their adapters; v1 stays distinct."""
import copy,json
from pathlib import Path
import pytest,jsonschema
from quirkbench import proposal_contracts as records
from quirkbench.contracts import ContractError,canonical,digest

ROOT=Path(__file__).resolve().parents[1]

def example():return json.loads((ROOT/'examples/agent-proposal.v2.json').read_bytes())

def test_examples_match_strict_schemas_and_runtime():
    for kind,version,file,validator in [('agent-proposal',2,'agent-proposal.v2',records.validate),
            ('proposal-context',1,'proposal-context',records.context)]:
        value=json.loads((ROOT/'examples'/f'{file}.json').read_bytes())
        jsonschema.Draft202012Validator(json.loads((ROOT/'schemas'/f'{kind}.v{version}.schema.json').read_bytes())).validate(value)
        assert validator(value)==value
        for change in ({'unknown':None},{'schema_version':True}):
            with pytest.raises(ContractError):validator({**value,**change})

@pytest.mark.parametrize('change',[
    {'base_oid':'f'*64+'1'}, {'source':None}, {'input_context_digest':'0'*64},
    {'usage':{'input_tokens':True,'output_tokens':None}}, {'experiment':None},
    {'rejected_approaches':['']}, {'schema_version':1}, {'base_revision':'f'*64},
])
def test_proposal_rejects_invalid_fields_and_legacy_reinterpretation(change):
    with pytest.raises(ContractError):records.validate({**example(),**change})

def test_nonexperiment_can_describe_preparation_failure_without_source_or_usage():
    value=example();value.update(action='needs_human',source=None,base_oid=None,experiment=None)
    value['input_context']['source']=None;value['input_context']['baseline_sha256']=None
    value['input_context_digest']=digest(canonical(value['input_context']))
    assert records.load(canonical(value))==value
    schema=json.loads((ROOT/'schemas/agent-proposal.v2.schema.json').read_bytes())
    jsonschema.Draft202012Validator(schema).validate(value)

@pytest.mark.parametrize('raw',[b'{"schema_version":2,"schema_version":2}',b'{"bad":NaN}',b'['*34+b'0'+b']'*34,b' '*((1<<20)+1)])
def test_parser_rejects_duplicate_nonfinite_deep_and_oversized_input(raw):
    with pytest.raises(ContractError):records.load(raw)
