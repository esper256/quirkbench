"""Wire contracts reject ambiguity before work or evidence enters the controller."""
from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path

import pytest

from quirkbench.contracts import (
    Artifact,
    CapabilityReport,
    Checkpoint,
    ContractError,
    Experiment,
    Result,
    Progress,
    canonical,
    digest,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = {
    "artifact": Artifact,
    "experiment": Experiment,
    "capability-report": CapabilityReport,
    "result": Result,
    "checkpoint": Checkpoint,
    "progress": Progress,
}
REQUIRED = {
    "artifact": {"sha256", "size"},
    "experiment": {"experiment_id", "hypothesis", "recipe"},
    "capability-report": {"device_id", "boot_id", "capabilities"},
    "result": {"attempt_id", "outcome", "summary"},
    "checkpoint": {"campaign_id"},
    "progress": {"activity_id","campaign_id","phase","state","message","sequence"},
}


@pytest.mark.parametrize("name", CONTRACTS)
def test_published_examples_are_accepted_by_runtime_contract(name):
    data = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    instance = CONTRACTS[name].from_dict(data)
    assert instance.schema_version == 1


@pytest.mark.parametrize("name", CONTRACTS)
def test_published_schema_field_set_matches_runtime_contract(name):
    schema = json.loads((ROOT / "schemas" / f"{name}.v1.schema.json").read_text())
    example = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    runtime_fields = {item.name for item in fields(CONTRACTS[name])}
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == runtime_fields
    assert set(schema["required"]) == REQUIRED[name]
    assert set(example) == runtime_fields
    assert schema["properties"]["schema_version"]["const"] == 1


@pytest.mark.parametrize("name", CONTRACTS)
def test_unknown_fields_and_versions_fail_closed(name):
    data = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    for patch in ({"new_field": "future"}, {"schema_version": 2}, {"schema_version": True}):
        with pytest.raises(ContractError):
            CONTRACTS[name].from_dict({**data, **patch})


@pytest.mark.parametrize("name", CONTRACTS)
def test_missing_required_field_rejected(name):
    data = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    schema = json.loads((ROOT / "schemas" / f"{name}.v1.schema.json").read_text())
    for field_name in schema["required"]:
        with pytest.raises(ContractError):
            CONTRACTS[name].from_dict({key: value for key, value in data.items() if key != field_name})


@pytest.mark.parametrize(
    "patch",
    [
        {"experiment_id": "../escape"},
        {"hypothesis": "  "},
        {"recipe": ""},
        {"artifacts": {"kernel": "not-a-sha"}},
        {"required_capabilities": ["network", "network"]},
        {"repetitions": 0},
        {"repetitions": True},
        {"timeout_s": 604801},
        {"parameters": {"unsafe": float("nan")}},
    ],
)
def test_invalid_experiment_rejected(patch):
    data = json.loads((ROOT / "examples" / "experiment.json").read_text())
    with pytest.raises(ContractError):
        Experiment.from_dict({**data, **patch})


@pytest.mark.parametrize(
    "name, patch",
    [
        ("artifact", {"size": -1}),
        ("artifact", {"size": True}),
        ("capability-report", {"mode": "controller"}),
        ("capability-report", {"capabilities": ["bad name"]}),
        ("result", {"outcome": "UNKNOWN"}),
        ("result", {"summary": " \t "}),
        ("result", {"evidence": ["not-a-sha"]}),
        ("checkpoint", {"artifacts": ["not-a-sha"]}),
    ],
)
def test_invalid_evidence_contracts_rejected(name, patch):
    data = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    with pytest.raises(ContractError):
        CONTRACTS[name].from_dict({**data, **patch})


def test_canonical_digest_is_independent_of_object_key_order():
    assert canonical({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert digest(canonical({"b": 2, "a": 1})) == digest(canonical({"a": 1, "b": 2}))
    with pytest.raises(ContractError):
        canonical({"measurement": float("inf")})


@pytest.mark.parametrize('name',CONTRACTS)
def test_examples_validate_against_json_schema(name):
    from jsonschema import Draft202012Validator
    schema=json.loads((ROOT/'schemas'/f'{name}.v1.schema.json').read_text())
    example=json.loads((ROOT/'examples'/f'{name}.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)
