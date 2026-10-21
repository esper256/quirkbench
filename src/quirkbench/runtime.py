"""Explicit target files shared by image staging and package/cache identities."""
import hashlib
from pathlib import Path
import shutil

from .files import atomic_write, save_json

ROOT = Path(__file__).parent
_LOADED_MANIFEST = (ROOT / "runtime.py").read_bytes()
# target.py currently serves both modes and imports these modules in deferred paths.
# Keep that real dependency shared; do not infer dependencies from import syntax.
COMMON = ('__init__.py', 'target.py', 'files.py', 'linux.py', 'diagnostics.py',
          'recovery_runtime.py', 'candidates.py', 'commands.py', 'transfer.py', 'console.py',
          'assets/quirkbench-prepare.service', 'assets/quirkbench-target.service',
          'assets/quirkbench-console.service')
RECOVERY = ('assets/build-recovery.sh', 'assets/configure-recovery.sh', 'assets/build-efi.sh')


def files_hash(names, root=None):
    root = root or ROOT
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        digest.update(name.encode() + b'\0' + (root / name).read_bytes() + b'\0')
    return digest.hexdigest()


def runtime_hash():
    return files_hash((*COMMON, 'runtime.py'))


def recovery_hash():
    return files_hash((*COMMON, 'runtime.py', *RECOVERY))


def check_manifest():
    if (ROOT / "runtime.py").read_bytes() != _LOADED_MANIFEST:
        raise ValueError("Target packaging instructions changed while this process was running. "
                         "Ask the controller operator to coordinate a restart before preparing software. "
                         "Existing target inspection and command access remain available.")


def stage_runtime(destination):
    """Stage an installable /usr tree; both builders consume this exact layout."""
    check_manifest()
    package = destination / 'usr/lib/quirkbench/quirkbench'
    for name in COMMON:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    save_json(package / 'source.json', {'source': runtime_hash()})
    for item in ('target', 'console'):
        atomic_write(destination / f'usr/bin/quirkbench-{item}',
                     "#!/usr/bin/python3\nimport sys\nsys.path.insert(0, '/usr/lib/quirkbench')\n"
                     f'from quirkbench.{item} import main\nmain()\n', mode=0o755)
    for name in COMMON:
        if name.endswith('.service'):
            target = destination / 'usr/lib/systemd/system' / Path(name).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
    configs = {
        'modprobe.d/quirkbench.conf': 'blacklist ahci\nblacklist mmc_block\n',
        'sysctl.d/90-quirkbench.conf': 'kernel.panic = 10\n',
        'systemd/system.conf.d/quirkbench.conf': '[Manager]\nRuntimeWatchdogSec=60s\nRebootWatchdogSec=2min\n',
    }
    for name, content in configs.items():
        atomic_write(destination / 'usr/lib' / name, content, mode=0o644)
