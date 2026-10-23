"""Save ordinary Wi-Fi profiles without binding them to one computer's adapter."""

from configparser import ConfigParser


def portable_wifi(contents):
    profile = ConfigParser(interpolation=None, strict=False)
    profile.read_string(contents)
    if profile.get("connection", "type", fallback="") not in ("wifi", "802-11-wireless"):
        return contents
    wifi = "wifi" if profile.has_section("wifi") else "802-11-wireless"
    if profile.get(wifi, "mode", fallback="infrastructure") != "infrastructure":
        return contents
    # Filter only adapter bindings. Preserve the exact secret/escape bytes.
    section = ""
    lines = []
    for line in contents.splitlines(keepends=True):
        text = line.strip()
        if text.startswith("[") and text.endswith("]"):
            section = text[1:-1]
        key = text.split("=", 1)[0].strip()
        if (section, key) in {
            ("connection", "interface-name"),
            ("wifi", "mac-address"),
            ("802-11-wireless", "mac-address"),
        }:
            continue
        lines.append(line)
    return "".join(lines)


def copy_profiles(source, destination):
    from .files import atomic_write

    destination.mkdir(parents=True, exist_ok=True)
    for profile in source.glob("*"):
        if profile.is_file():
            atomic_write(destination / profile.name, portable_wifi(profile.read_text()))


def profile_summary(folder):
    lines = []
    for path in sorted(folder.glob("*")):
        if not path.is_file():
            continue
        try:
            profile = ConfigParser(interpolation=None, strict=False)
            profile.read_string(path.read_text())
            if profile.get("connection", "type", fallback="") not in ("wifi", "802-11-wireless"):
                continue
            # Only an explicit allowlist leaves this function. Never dump the keyfile.
            lines.append(
                "; ".join(
                    f"{name}={profile.get(section, key, fallback='not set')}"
                    for name, section, key in [
                        ("Name", "connection", "id"),
                        ("Adapter", "connection", "interface-name"),
                        ("MAC binding", "wifi", "mac-address"),
                        ("Auto-connect", "connection", "autoconnect"),
                        ("Security", "wifi-security", "key-mgmt"),
                    ]
                )
            )
        except (OSError, __import__("configparser").Error):
            lines.append("A profile could not be read.")
    return "\n".join(lines) or "None"
