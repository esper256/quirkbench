"""Stage the real candidate utility with executable substitutes for GPU and sound."""

from pathlib import Path
import shutil
import subprocess
import sys
import os

ASSETS = Path(__file__).resolve().parents[2] / "src/quirkbench/assets/load"


def load_tool(tmp_path, *, gpu_result="pass", audio_failure=False, audio_message="", paced=False):
    for name in ("quirkbench-load", "scene.shader_test", "crickets.wav"):
        shutil.copy2(ASSETS / name, tmp_path / name)
    runner = tmp_path / "vkrunner"
    runner.write_text(
        "#!"
        + sys.executable
        + '''
import sys
from pathlib import Path
if '--device-list' in sys.argv:
    print('Device 1: Test GPU\\n  Type: Integrated GPU')
else:
    text = Path(sys.argv[-1]).read_text()
    assert 'draw rect -1 -1 2 2' in text and 'push float 0' in text
    print('PIGLIT: {"result": "'''
        + gpu_result
        + """"}')
"""
    )
    player = tmp_path / "aplay"
    player.write_text(
        "#!"
        + sys.executable
        + """
import sys
import time
from pathlib import Path
"""
        + f"print({audio_message!r}, file=sys.stderr, flush=True)\n"
        + f"paced = {paced!r}\n"
        + (
            "sys.exit(7)\n"
            if audio_failure
            else """
with (Path(__file__).parent/'audio.raw').open('wb', buffering=0) as out:
    while chunk := sys.stdin.buffer.read(1920):
        if out.tell() < 200000:
            out.write(chunk)
        if paced:
            time.sleep(len(chunk) / 192000)
"""
        )
    )
    runner.chmod(0o755)
    player.chmod(0o755)

    def start(*args):
        return subprocess.Popen(
            [sys.executable, str(tmp_path / "quirkbench-load"), *args],
            env={**os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"]},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def invoke(*args):
        with start(*args) as process:
            output, error = process.communicate(timeout=5)
            return subprocess.CompletedProcess(process.args, process.returncode, output, error)

    invoke.start = start
    return invoke
