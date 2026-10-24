"""A local message and an optional single short sound; no mixer changes."""

import io
import math
import struct
import tempfile
import wave
from pathlib import Path

from .files import disk


async def chime(linux):
    def sound():
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(
                b"".join(
                    struct.pack("<h", int(4000 * math.sin(2 * math.pi * 660 * i / 22050)))
                    for i in range(11025)
                )
            )
        return output.getvalue()

    with tempfile.TemporaryDirectory(prefix="quirkbench-chime-", dir="/run") as folder:
        path = Path(folder) / "chime.wav"
        await disk(path.write_bytes, sound())
        try:
            result = await linux.command("aplay", "-q", path, timeout=3, check=False)
            return "played" if result["exit_code"] == 0 else "unavailable"
        except (OSError, RuntimeError, TimeoutError):
            return "unavailable"


async def desktop(linux, session, message, previous=None):
    """Use libnotify in an existing user's bus; no desktop service of our own."""
    from .sessions import execution_context

    try:
        context = await execution_context(linux, session, None, None)
        env = context["environment"]
        args = [
            "runuser",
            "-u",
            env["USER"],
            "--",
            "env",
            *[f"{k}={v}" for k, v in env.items()],
            "notify-send",
            "--print-id",
            "--app-name=Quirkbench",
            "--hint=boolean:suppress-sound:true",
        ]
        if previous:
            args += ["--replace-id", str(previous)]
        args += ["Quirkbench", message]
        result = await linux.command(*args, timeout=10, check=False)
        if result["exit_code"] != 0 or not result["output"].strip().isdigit():
            return dict(
                status="unavailable",
                message="Desktop notification unavailable; use target menu or controller CLI.",
            )
        return dict(
            status="delivered",
            notification_id=int(result["output"].strip()),
            message="Notification submitted; human acknowledgment is separate.",
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        return dict(status="unavailable", message=str(error)[:500])
