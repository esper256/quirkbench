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
