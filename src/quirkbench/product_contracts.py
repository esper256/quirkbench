"""P0 product documents. These validate intent; they do not dispatch work.

The public Experiment and Result envelopes remain in ``contracts``. Persistence,
source capture, recipe eligibility and operation admission belong to later packets.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from .contracts import ContractError, canonical, identifier, sha256

MAX_DOCUMENT_BYTES = 1 << 20
MAX_DEPTH = 32


def _object(value: Any, names: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != names:
        raise ContractError(f"expected exactly {sorted(names)}")
    return value


def _text(value: Any, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f"{name} must be nonempty and at most {limit} characters")
    return value


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ContractError(f"{name} must be an integer in {low}..{high}")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or len(value) != 20 or not value.endswith("Z"):
        raise ContractError(f"{name} must be a UTC second timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ContractError(f"{name} must be a UTC second timestamp") from exc
    return parsed


def _depth(value: Any, level: int = 0) -> None:
    if level > MAX_DEPTH:
        raise ContractError("product document nesting exceeds 32 levels")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def load_document(raw: bytes, kind: str) -> dict:
    """Parse bounded JSON before validating a product document by kind."""
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ContractError("product document exceeds 1 MiB")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContractError("nonfinite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError("invalid product JSON") from exc
    _depth(value)
    return validate_document(kind, value)


def validate_document(kind: str, value: Any) -> dict:
    validators = {
        "session-intent": _session_intent,
        "agent-proposal": _agent_proposal,
        "observation-request": _observation_request,
        "observation-response": _observation_response,
    }
    if kind not in validators:
        raise ContractError("unknown product document kind")
    data = validators[kind](value)
    _depth(data)
    if len(canonical(data)) > MAX_DOCUMENT_BYTES:
        raise ContractError("product document exceeds 1 MiB")
    return data


def _version(data: dict) -> None:
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ContractError("unsupported schema_version")


def _session_intent(value: Any) -> dict:
    data = _object(value, {"schema_version", "session_id", "campaign_id", "device_id",
                           "driver", "execution_owner", "problem_digest", "workspace_id"})
    _version(data)
    for name in ("session_id", "campaign_id", "device_id", "workspace_id"):
        identifier(data[name])
    sha256(data["problem_digest"])
    if data["driver"] not in ("external", "managed"):
        raise ContractError("invalid session driver")
    owner = "external" if data["driver"] == "external" else "session_runner"
    if data["execution_owner"] != owner:
        raise ContractError("session driver and execution owner disagree")
    return data


def _source(value: Any) -> None:
    data = _object(value, {"kind", "digest"})
    if data["kind"] not in ("pinned_revision", "completed_capture"):
        raise ContractError("source must be a pinned revision or completed capture")
    sha256(data["digest"])


def _experiment_proposal(value: Any) -> None:
    data = _object(value, {"baseline_id", "build_recipe_id", "target_recipe_id",
                           "parameters", "repetitions", "deadline_s"})
    for name in ("baseline_id", "build_recipe_id", "target_recipe_id"):
        identifier(data[name])
    _integer(data["repetitions"], "repetitions", 1, 10000)
    _integer(data["deadline_s"], "deadline_s", 1, 604800)
    if not isinstance(data["parameters"], dict) or len(data["parameters"]) > 64:
        raise ContractError("parameters must be a bounded object")
    for key, item in data["parameters"].items():
        identifier(key)
        if type(item) not in (str, int, float, bool) and item is not None:
            raise ContractError("parameters must be typed scalar values")
    canonical(data["parameters"])


def _usage(value: Any) -> None:
    data = _object(value, {"input_tokens", "output_tokens"})
    for name in ("input_tokens", "output_tokens"):
        if data[name] is not None:
            _integer(data[name], name, 0, 1_000_000_000)


def _agent_proposal(value: Any) -> dict:
    data = _object(value, {"schema_version", "decision_id", "campaign_id", "input_context_digest",
                           "action", "hypothesis", "summary", "rejected_approaches", "workspace_id",
                           "base_revision", "change_intent", "source", "experiment", "usage"})
    _version(data)
    for name in ("decision_id", "campaign_id", "workspace_id"):
        identifier(data[name])
    for name in ("input_context_digest", "base_revision"):
        sha256(data[name])
    if data["action"] not in ("experiment", "needs_human", "conclude"):
        raise ContractError("invalid proposal action")
    for name in ("hypothesis", "summary", "change_intent"):
        _text(data[name], name)
    rejected = data["rejected_approaches"]
    if not isinstance(rejected, list) or len(rejected) > 32:
        raise ContractError("rejected_approaches must be a bounded list")
    for item in rejected:
        _text(item, "rejected approach", 1024)
    if data["source"] is not None:
        _source(data["source"])
    if data["experiment"] is not None:
        _experiment_proposal(data["experiment"])
    if data["action"] == "experiment" and (data["source"] is None or data["experiment"] is None):
        raise ContractError("experiment action requires immutable source and experiment proposal")
    if data["action"] != "experiment" and data["experiment"] is not None:
        raise ContractError("only experiment action may carry an experiment proposal")
    if data["usage"] is not None:
        _usage(data["usage"])
    return data


def _observation_request(value: Any) -> dict:
    data = _object(value, {"schema_version", "request_id", "session_id", "attempt_id",
                           "recipe_step_id", "kind", "prompt", "issued_at", "deadline_at"})
    _version(data)
    for name in ("request_id", "session_id", "recipe_step_id"):
        identifier(data[name])
    if data["attempt_id"] is not None:
        identifier(data["attempt_id"])
    if data["kind"] not in ("pre_test_readiness", "live_observation", "post_test_interpretation"):
        raise ContractError("invalid observation kind")
    if data["kind"] == "live_observation" and data["attempt_id"] is None:
        raise ContractError("live observation requires an attempt")
    _text(data["prompt"], "prompt")
    if _timestamp(data["deadline_at"], "deadline_at") <= _timestamp(data["issued_at"], "issued_at"):
        raise ContractError("observation deadline must follow issue time")
    return data


def _observation_response(value: Any) -> dict:
    data = _object(value, {"schema_version", "request_id", "session_id", "operator_id",
                           "answered_at", "answer", "note"})
    _version(data)
    for name in ("request_id", "session_id", "operator_id"):
        identifier(data[name])
    _timestamp(data["answered_at"], "answered_at")
    if data["answer"] not in ("observed", "not_observed", "uncertain", "declined"):
        raise ContractError("invalid observation answer")
    if data["note"] is not None:
        _text(data["note"], "note")
    return data
