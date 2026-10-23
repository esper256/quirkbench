"""Computer identity is independent of the removable recovery drive."""

import hashlib
from pathlib import Path
import uuid


def computer_identity(
    dmi=Path("/sys/class/dmi/id"),
    override=Path("/run/quirkbench-computer-id"),
    cmdline=Path("/proc/cmdline"),
):
    def read(name):
        try:
            return (dmi / name).read_text().strip()
        except OSError:
            return ""

    model = " ".join(filter(None, (read("sys_vendor"), read("product_name")))) or "Unknown computer"
    try:
        value = uuid.UUID(read("product_uuid"))
        identity = str(value) if value.int not in (0, (1 << 128) - 1) else ""
    except ValueError:
        identity = ""
    # An explicit identifier is useful on firmware with missing/duplicate UUIDs.
    # /run is deliberately not on the portable drive.
    manual = False
    try:
        choice = override.read_text().strip()
        if choice:
            identity, manual = choice, True
    except OSError:
        pass
    # Recovery carries an explicit human choice through this one candidate boot.
    # It is not saved on the drive as a default for the next recovery computer.
    try:
        args = dict(part.split("=", 1) for part in cmdline.read_text().split() if "=" in part)
    except OSError:
        args = {}
    if args.get("quirkbench.run") and args.get("quirkbench.computer-choice"):
        from .files import identifier

        return {
            "id": identifier(args["quirkbench.computer-choice"]),
            "model": model[:160],
            "manual": True,
        }
    if not identity:
        raise ValueError(
            "This computer has no usable firmware UUID. Open the terminal, write a unique "
            "computer name to /run/quirkbench-computer-id, then restart quirkbench-target. "
            "Use the same name on future boots of this computer."
        )
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest(),
        "model": model[:160],
        "manual": manual,
    }


def hardware_description(dmi=Path("/sys/class/dmi/id")):
    lines = ["Computer:"]
    for label, name in [
        ("Manufacturer", "sys_vendor"),
        ("Model", "product_name"),
        ("Firmware version", "bios_version"),
    ]:
        try:
            value = (dmi / name).read_text().strip()
        except OSError:
            value = "Unavailable"
        lines.append(f"{label}: {value}")
    return "\n".join(lines)
