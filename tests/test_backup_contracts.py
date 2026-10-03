"""Mechanical coverage contract precedes the backup adapter."""
import json
from pathlib import Path
from copy import deepcopy
import jsonschema
import pytest
from quirkbench.backup_contracts import validate,load
from quirkbench.contracts import ContractError,canonical

ROOT=Path(__file__).resolve().parents[1]


def test_backup_coverage_schema_runtime_and_failure_fixtures():
    value=json.loads((ROOT/'examples/backup-coverage.json').read_bytes())
    schema=json.loads((ROOT/'schemas/backup-coverage.v1.schema.json').read_bytes())
    checker=jsonschema.Draft202012Validator(schema)
    checker.validate(value);assert load(canonical(value))==value
    variants=[]
    for key,item in [('unknown',None),('schema_version',True)]:variants.append({**value,key:item})
    bad=deepcopy(value);bad['contents']['whole_session_complete']=True;variants.append(bad)
    bad=deepcopy(value);bad['contents']['pending_upload_count']=False;variants.append(bad)
    bad=deepcopy(value);bad['source_workspaces'][0]['base_oid']='main';variants.append(bad)
    bad=deepcopy(value);bad['targets'][0]['target_only_backlog']='none';variants.append(bad)
    for bad in variants:
        with pytest.raises(ContractError):validate(bad)
        with pytest.raises(jsonschema.ValidationError):checker.validate(bad)
    with pytest.raises(ContractError):load(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ContractError):load(b'{"value":NaN}')
    with pytest.raises(ContractError):load(canonical({'nested':[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]}))
