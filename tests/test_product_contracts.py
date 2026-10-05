"""Public contracts and executable syntax reject unsafe documents."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from quirkbench.contracts import ContractError, canonical, digest
from quirkbench.cli import parser
from quirkbench.product_contracts import load_document, validate_document

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("session-intent", "agent-proposal", "observation-request", "observation-response")
SCHEMA = json.loads((ROOT / "schemas/product-contracts.v1.schema.json").read_text())


def example(kind):
    return json.loads((ROOT / "examples" / f"{kind}.json").read_text())


@pytest.mark.parametrize("kind", KINDS)
def test_published_example_has_matching_runtime_and_json_schema(kind):
    Draft202012Validator.check_schema(SCHEMA)
    value = example(kind)
    validator = Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{kind}"}, format_checker=FormatChecker())
    validator.validate(value)
    assert validate_document(kind, value) == value
    assert load_document(canonical(value), kind) == value
    assert digest(canonical(value)) == digest(canonical(load_document(canonical(value), kind)))
    assert set(value) == set(SCHEMA["$defs"][kind]["required"])


@pytest.mark.parametrize("kind", KINDS)
def test_new_records_reject_unknown_missing_and_unsupported_versions(kind):
    value = example(kind)
    for changed in ({**value, "extra": "unsafe"},
                    {key: item for key, item in value.items() if key != "schema_version"},
                    {**value, "schema_version": 2}, {**value, "schema_version": True}):
        with pytest.raises(ContractError):
            validate_document(kind, changed)


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"schema_version":1}',
    b'{"value":NaN}',
    b'{"value":Infinity}',
    b'{"value":1e999}',
    b'{' + b'"x":' * 33 + b'0' + b'}' * 33,
    b'x' * (1024 * 1024 + 1),
])
def test_bounded_parser_rejects_ambiguous_or_excessive_input(raw):
    with pytest.raises(ContractError):
        load_document(raw, "agent-proposal")


@pytest.mark.parametrize("patch", [
    {"driver": "managed"},
    {"problem_digest": "not-a-digest"},
    {"device_id": "../target"},
])
def test_session_intent_does_not_infer_authority(patch):
    with pytest.raises(ContractError):
        validate_document("session-intent", {**example("session-intent"), **patch})


@pytest.mark.parametrize("change", [
    lambda d: d.update(action="experiment", source=None),
    lambda d: d.update(action="conclude"),
    lambda d: d["source"].update(kind="mutable_branch"),
    lambda d: d["source"].update(path="/etc/shadow"),
    lambda d: d["experiment"].update(command="sh -c something"),
    lambda d: d["experiment"]["parameters"].update(shell=["sh", "-c"]),
    lambda d: d.update(usage={"input_tokens": 0, "output_tokens": -1}),
    lambda d: d.update(rejected_approaches=[""] * 33),
])
def test_proposal_is_intent_not_command_or_authorization(change):
    value = example("agent-proposal")
    change(value)
    with pytest.raises(ContractError):
        validate_document("agent-proposal", value)


@pytest.mark.parametrize("kind,patch", [
    ("observation-request", {"attempt_id": None}),
    ("observation-request", {"deadline_at": "2026-09-27T11:59:59Z"}),
    ("observation-request", {"issued_at": "2026-09-27T12:00:00+00:00"}),
    ("observation-response", {"answer": "yes"}),
    ("observation-response", {"operator_id": ""}),
])
def test_observation_types_and_deadline_fail_closed(kind, patch):
    with pytest.raises(ContractError):
        validate_document(kind, {**example(kind), **patch})


@pytest.mark.parametrize("argv,expected", [
    (["investigation","observation","answer", "session-01", "--request", "o1", "--file", "answer.json", "--request-id", "r3"], {"action": "respond", "request": "o1", "request_id": "r3"}),
    (["experiment", "list", "investigation-01", "--json"], {"command": "submission", "action": "list", "json": True}),
    (["investigation","evidence","read", "a" * 64, "investigation-01", "--length", "4096"], {"command": "evidence", "action": "read", "length": 4096}),
    (['admin', 'backup', '--output', 'backup-dir'], {"command": "backup", "destination": Path("backup-dir")}),
    (['admin', 'backup', 'backup-dir'], {"command": "backup", "destination": Path("backup-dir")}),
])
def test_executable_cli_argument_contract(argv, expected):
    args = vars(parser().parse_args(argv))
    assert {key: args[key] for key in expected} == expected


@pytest.mark.parametrize("argv", [
    ["session", "start"],
    ["session", "propose", "session-01", "--file", "proposal.json"],
    ["investigation","observation","answer", "session-01", "--request", "o1", "--file", "answer.json"],
    ["session", "start", "--device", "target-01", "--driver", "shell"],
    ['admin', 'operation', 'show'],
])
def test_executable_cli_rejects_missing_or_unsupported_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(argv)
    assert exc.value.code == 2


def test_existing_executable_does_not_claim_planned_session_works(tmp_path):
    state = tmp_path / "state"
    run = subprocess.run([sys.executable, "-m", "quirkbench", "--state", str(state),
                          "session", "start", "--device", "target-01"],
                         capture_output=True, text=True)
    assert run.returncode != 0
    assert not state.exists()


def test_executable_help_keeps_evidence_scope_visible():
    help_text = " ".join(parser().format_help().split())
    assert "investigation" in help_text and "experiment" in help_text
    assert "session" not in help_text and "job" not in help_text
