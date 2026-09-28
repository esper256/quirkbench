"""Accidental target-movement guards, not hardware authentication or attestation."""
from pathlib import Path
import re

from .contracts import ContractError


class BindingError(ContractError):
    pass


def system_uuid(value: str) -> str:
    if (not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)
            or value.replace("-", "") in {"0" * 32, "f" * 32}):
        raise BindingError("No usable system UUID; attended target setup required.")
    return value


def read_system_uuid(path: Path = Path("/sys/class/dmi/id/product_uuid")) -> str:
    try:
        with path.open("r", encoding="ascii") as stream:
            value = stream.read(128).strip().lower()
    except (OSError, UnicodeError) as exc:
        raise BindingError("System identity unavailable; attended target setup required.") from exc
    return system_uuid(value)


def verify_binding(binding, *, reader=None) -> str:
    if (not isinstance(binding, dict) or set(binding) != {"schema_version", "system_uuid"}
            or type(binding["schema_version"]) is not int or binding["schema_version"] != 1):
        raise BindingError("Missing or unsupported target binding; attended target setup required.")
    expected = system_uuid(binding["system_uuid"])
    if expected != system_uuid((reader or read_system_uuid)()):
        raise BindingError("Target identity changed; explicit retargeting required. Prior evidence retained.")
    return expected
