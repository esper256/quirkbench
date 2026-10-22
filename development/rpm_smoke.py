"""Run as an ordinary user inside the disposable RPM test container."""

import json
from pathlib import Path
import subprocess
import time


def main():
    home = Path.home()
    with (home / "controller-smoke.log").open("w+") as log:
        controller = subprocess.Popen(["quirkbench", "controller", "run"], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if controller.poll() is not None:
                    break
                status = subprocess.run(
                    ["quirkbench", "--json", "status"], capture_output=True, text=True, timeout=5
                )
                if status.returncode == 0 and json.loads(status.stdout)["directory_in_use"]:
                    subprocess.run(["quirkbench", "controller", "stop"], check=True, timeout=10)
                    assert controller.wait(timeout=10) == 0
                    print(
                        "Installed RPM: foreground controller started, answered status and stopped."
                    )
                    return
                time.sleep(0.1)
            log.seek(0)
            raise RuntimeError("Installed controller did not become ready:\n" + log.read())
        finally:
            if controller.poll() is None:
                controller.terminate()
                try:
                    controller.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    controller.kill()
                    controller.wait()


if __name__ == "__main__":
    main()
