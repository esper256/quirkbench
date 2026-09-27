"""Versioned deployment identity and replaceable, side-effect-free interfaces.

An experiment references the manifest as a regular content-addressed artifact.
Backend-native revision IDs never replace the artifact's own digest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .contracts import ContractError, canonical, digest, identifier, sha256


@dataclass(frozen=True)
class DeploymentManifest:
    backend: str
    revision: str
    repository: str
    provenance: dict
    protection_profile: str
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError('unsupported deployment manifest version')
        identifier(self.backend)
        # Other backends may add validators without changing experiment records.
        if self.backend != 'ostree':
            raise ContractError('unsupported deployment backend')
        sha256(self.revision)
        identifier(self.repository)
        identifier(self.protection_profile)
        if not isinstance(self.provenance, dict) or not self.provenance:
            raise ContractError('deployment provenance required')
        canonical(self.provenance)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError('invalid deployment manifest fields')
        return cls(**value)

    def to_dict(self):
        return asdict(self)

    @property
    def sha256(self):
        return digest(canonical(self.to_dict()))


@dataclass(frozen=True)
class PreparedDeployment:
    deployment_id: str
    attempt_id: str
    manifest_digest: str
    revision: str
    boot_entry: Path

    def __post_init__(self):
        for value in (self.deployment_id, self.manifest_digest, self.revision):
            sha256(value)
        identifier(self.attempt_id)
        if not isinstance(self.boot_entry, Path) or not self.boot_entry.is_absolute():
            raise ContractError('prepared deployment needs absolute boot entry')


class Composer(Protocol):
    def compose(self, inputs) -> DeploymentManifest: ...


class DeploymentBackend(Protocol):
    def prepare(self, manifest: DeploymentManifest, attempt_id: str) -> PreparedDeployment: ...
    def inspect(self, attempt_id: str) -> PreparedDeployment | None: ...
    def running_revision(self) -> str | None: ...
    def retain(self, deployment: PreparedDeployment) -> None: ...
    def remove(self, deployment: PreparedDeployment) -> None: ...


class BootControl(Protocol):
    def arm_once(self, deployment: PreparedDeployment, attempt_id: str) -> None: ...
    def reboot_to_candidate(self) -> None: ...
    def recover(self) -> None: ...
