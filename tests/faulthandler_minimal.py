"""Issue #8 reproducer without Quirkbench imports or repository fixtures.

Copy outside the checkout and run explicitly with pytest -c /dev/null and
-o faulthandler_timeout=0.1. This is an opt-in crash diagnostic, not an
application regression or a reason to disable traceback/assertion diagnostics.
"""
from pathlib import Path
import time


def test_delayed_traceback():
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        Path('/tmp').resolve(strict=True)
