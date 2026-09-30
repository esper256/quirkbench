#!/usr/bin/env python3
"""One-time, explicitly requested wipe of this checkout's legacy state only."""
from pathlib import Path
import os
import sys

sys.dont_write_bytecode = True
project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'src'))
from quirkbench.maintenance import remove_tree, nested_mounts

if sys.argv[1:] != ['--discard']:
    raise SystemExit('usage: discard-legacy-state.py --discard (permanent; no backup)')
root = project / '.quirkbench'
if not root.exists():
    print('Legacy state already absent.')
    raise SystemExit(0)
if root.is_symlink() or root.resolve() != root or nested_mounts(root):
    raise SystemExit('Refusing linked or mounted legacy state.')
identity = root.stat()
if identity.st_uid != os.getuid():
    raise SystemExit('Legacy state is not owned by this user.')
# Do not delete data still being consumed by a build/viewer in this namespace.
for process in Path('/proc').iterdir():
    if not process.name.isdecimal():
        continue
    try:
        if process.stat().st_uid != os.getuid():
            continue
        links = [process / 'cwd', *(process / 'fd').iterdir()]
        for link in links:
            try:
                target = Path(os.readlink(link))
                if target.is_absolute() and target.is_relative_to(root):
                    raise SystemExit('Legacy state remains open by process ' + process.name)
            except (FileNotFoundError,PermissionError):
                pass
    except (FileNotFoundError, PermissionError):
        continue
current=root.stat()
if (current.st_dev,current.st_ino)!=(identity.st_dev,identity.st_ino):
    raise SystemExit('Legacy directory identity changed.')
print('Discarding ' + str(root), flush=True)
remove_tree(root,project)
print('Legacy state permanently removed.', flush=True)
