"""Launch the real target app with harmless external tools in another process."""

import asyncio
from pathlib import Path
import sys
from quirkbench.target import Target
from tests.support.lab import ToolResults

if __name__ == "__main__":
    target = Target(
        Path(sys.argv[1]), Path(sys.argv[2]), boot_id="separate-process", linux=ToolResults()
    )
    try:
        asyncio.run(target.run())
    except KeyboardInterrupt:
        pass
