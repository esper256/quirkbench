"""Resolve runtime assets from the installed package or source checkout."""
from __future__ import annotations

from importlib import resources
from pathlib import Path


def target_assets_dir() -> Path:
    """Return the packaged asset tree; source fallback is only for development."""
    try:
        installed = Path(resources.files("quirkbench.assets"))
    except ModuleNotFoundError:
        installed = None
    if installed is not None and installed.is_dir():
        return installed
    source = Path(__file__).resolve().parents[2] / "target-assets"
    if source.is_dir():
        return source
    raise FileNotFoundError("installed Quirkbench target assets are unavailable")


def _resource_dir(package: str, source_name: str, required_name: str) -> Path:
    """Find a packaged resource tree, falling back to the development checkout."""
    try:
        installed = Path(resources.files(package))
    except ModuleNotFoundError:
        installed = None
    if installed is not None and (installed / required_name).is_file():
        return installed
    source = Path(__file__).resolve().parents[2] / source_name
    if (source / required_name).is_file():
        return source
    raise FileNotFoundError(f"Quirkbench {source_name} resources are unavailable")


def schemas_dir() -> Path:
    return _resource_dir("quirkbench.schemas", "schemas", "experiment.v1.schema.json")


def examples_dir() -> Path:
    return _resource_dir("quirkbench.examples", "examples", "experiment.json")


def agent_guide_path() -> Path:
    return _resource_dir("quirkbench.guide", "docs", "agent-guide.md") / "agent-guide.md"
