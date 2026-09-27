"""Version 1 wire contracts. Unknown fields and unsupported versions fail closed."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any

VERSION = 1


class ContractError(ValueError):
    pass


class Conflict(ContractError):
    pass


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    INFRA_FAILURE = "INFRA_FAILURE"
    NEEDS_HUMAN = "NEEDS_HUMAN"


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ContractError("not canonical JSON") from exc


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ContractError("invalid identifier")
    return value


def sha256(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError("invalid SHA256")
    return value


def positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ContractError(f"{name} must be finite and positive")


def fields(data: dict, allowed: set[str], required: set[str]) -> None:
    if not isinstance(data, dict) or set(data) - allowed or required - set(data):
        raise ContractError(f"expected fields {sorted(allowed)}; required {sorted(required)}")
    if type(data.get("schema_version", VERSION)) is not int or data.get("schema_version", VERSION) != VERSION:
        raise ContractError("unsupported schema_version")


@dataclass(frozen=True)
class Artifact:
    sha256: str
    size: int
    schema_version: int = VERSION

    def __post_init__(self):
        sha256(self.sha256)
        if type(self.size) is not int or self.size < 0:
            raise ContractError("size must be a nonnegative integer")
        if type(self.schema_version) is not int or self.schema_version != VERSION:
            raise ContractError("unsupported schema_version")

    @classmethod
    def from_dict(cls, data: dict) -> Artifact:
        fields(data, {"sha256", "size", "schema_version"}, {"sha256", "size"})
        return cls(**data)


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    hypothesis: str
    recipe: str
    artifacts: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[str] = field(default_factory=list)
    repetitions: int = 1
    timeout_s: int = 300
    baseline_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    success_criteria: str = "Recipe reports the expected observations"
    schema_version: int = VERSION

    def __post_init__(self):
        identifier(self.experiment_id)
        identifier(self.recipe)
        if not isinstance(self.hypothesis, str) or not self.hypothesis.strip():
            raise ContractError("hypothesis required")
        if not isinstance(self.success_criteria, str) or not self.success_criteria.strip():
            raise ContractError("success_criteria required")
        if type(self.repetitions) is not int or not 1 <= self.repetitions <= 10000:
            raise ContractError("repetitions must be 1..10000")
        if type(self.timeout_s) is not int or not 1 <= self.timeout_s <= 604800:
            raise ContractError("timeout_s must be 1..604800")
        if not isinstance(self.artifacts, dict):
            raise ContractError("artifacts must be an object")
        for role, value in self.artifacts.items():
            identifier(role)
            sha256(value)
        if not isinstance(self.required_capabilities, list):
            raise ContractError("capabilities must be a unique list")
        for capability in self.required_capabilities:
            identifier(capability)
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ContractError("capabilities must be unique")
        if self.baseline_id is not None:
            identifier(self.baseline_id)
        if not isinstance(self.parameters, dict) or not isinstance(self.provenance, dict):
            raise ContractError("parameters and provenance must be objects")
        if type(self.schema_version) is not int or self.schema_version != VERSION:
            raise ContractError("unsupported schema_version")
        canonical(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> Experiment:
        fields(data, set(cls.__dataclass_fields__), {"experiment_id", "hypothesis", "recipe"})
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityReport:
    device_id: str
    boot_id: str
    capabilities: list[str]
    mode: str = "recovery"
    inventory: dict[str, Any] = field(default_factory=dict)
    schema_version: int = VERSION

    def __post_init__(self):
        identifier(self.device_id)
        identifier(self.boot_id)
        if self.mode not in {"recovery", "experiment", "simulation"}:
            raise ContractError("invalid target mode")
        if not isinstance(self.capabilities, list):
            raise ContractError("capabilities must be a list")
        for capability in self.capabilities:
            identifier(capability)
        if not isinstance(self.inventory, dict):
            raise ContractError("inventory must be an object")
        if type(self.schema_version) is not int or self.schema_version != VERSION:
            raise ContractError("unsupported schema_version")
        canonical(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> CapabilityReport:
        fields(data, set(cls.__dataclass_fields__), {"device_id", "boot_id", "capabilities"})
        return cls(**data)


@dataclass(frozen=True)
class Result:
    attempt_id: str
    outcome: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    schema_version: int = VERSION

    def __post_init__(self):
        identifier(self.attempt_id)
        try:
            Outcome(self.outcome)
        except ValueError as exc:
            raise ContractError("invalid outcome") from exc
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ContractError("summary required")
        if not isinstance(self.evidence, list):
            raise ContractError("evidence must be a list")
        for value in self.evidence:
            sha256(value)
        if not isinstance(self.measurements, dict) or not isinstance(self.limitations, list) or not all(isinstance(x, str) for x in self.limitations):
            raise ContractError("invalid measurements or limitations")
        if type(self.schema_version) is not int or self.schema_version != VERSION:
            raise ContractError("unsupported schema_version")
        canonical(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> Result:
        fields(data, set(cls.__dataclass_fields__), {"attempt_id", "outcome", "summary"})
        return cls(**data)


@dataclass(frozen=True)
class Checkpoint:
    campaign_id: str
    artifacts: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = VERSION

    def __post_init__(self):
        identifier(self.campaign_id)
        if not isinstance(self.artifacts, list) or not isinstance(self.notes, dict):
            raise ContractError("invalid checkpoint")
        for value in self.artifacts:
            sha256(value)
        if type(self.schema_version) is not int or self.schema_version != VERSION:
            raise ContractError("unsupported schema_version")
        canonical(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> Checkpoint:
        fields(data, set(cls.__dataclass_fields__), {"campaign_id"})
        return cls(**data)

@dataclass(frozen=True)
class Progress:
    """Producer sequence fences retries; wall times are assigned by the controller."""
    activity_id: str
    campaign_id: str
    phase: str
    state: str
    message: str
    sequence: int
    completed: int | None = None
    total: int | None = None
    unit: str = 'items'
    expected_update_s: int = 30
    stall_after_s: int = 300
    timeout_s: int = 3600
    schema_version: int = VERSION

    def __post_init__(self):
        identifier(self.activity_id); identifier(self.campaign_id); identifier(self.phase)
        if self.state not in ('ACTIVE', 'WAITING', 'COMPLETE', 'FAILED'):
            raise ContractError('invalid progress state')
        if not isinstance(self.message, str) or not self.message.strip() or len(self.message) > 1000:
            raise ContractError('progress requires a message of at most 1000 characters')
        if type(self.sequence) is not int or self.sequence < 0:
            raise ContractError('progress sequence must be a nonnegative integer')
        identifier(self.unit)
        for name in ('completed', 'total'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ContractError('progress counters must be nonnegative integers')
        if self.total is not None and (self.total == 0 or self.completed is None or self.completed > self.total):
            raise ContractError('total requires completed <= total and total > 0')
        for name in ('expected_update_s', 'stall_after_s', 'timeout_s'):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 604800:
                raise ContractError('progress timing must be 1..604800 seconds')
        if type(self.schema_version) is not int or self.schema_version != VERSION:
            raise ContractError('unsupported schema_version')

    @classmethod
    def from_dict(cls, data):
        fields(data, set(cls.__dataclass_fields__), {'activity_id','campaign_id','phase','state','message','sequence'})
        return cls(**data)
