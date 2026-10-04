"""Host-only kernel qualification configuration gate."""
from pathlib import Path

def validate_watchdog_kernel(path: str | Path, *, driver: str, lockup_detection: bool = True) -> None:
    """Optional post-olddefconfig build gate, in addition to storage protection."""
    from .build import BuildError, _parse_config, validate_kernel_config
    path = Path(path)
    validate_kernel_config(path)
    if driver not in {"CONFIG_WDAT_WDT", "CONFIG_SP5100_TCO", "CONFIG_ITCO_WDT"}:
        raise ValueError("watchdog driver needs profile review")
    required = {"CONFIG_WATCHDOG", "CONFIG_WATCHDOG_CORE", "CONFIG_WATCHDOG_SYSFS", driver}
    if lockup_detection:
        required.update({"CONFIG_SOFTLOCKUP_DETECTOR", "CONFIG_HARDLOCKUP_DETECTOR"})
    values = _parse_config(path)
    missing = sorted(key for key in required if values.get(key) != "y")
    if missing:
        raise BuildError("watchdog qualification kernel requires built-in: " + ", ".join(missing))

