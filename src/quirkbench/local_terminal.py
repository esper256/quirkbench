"""Explicit human-operated root shell; never an agent execution adapter."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

BANNER = '''Root terminal: commands are unrestricted and can modify internal disks.
Type exit to return to Quirkbench. Ctrl+Alt+F2 also returns to the dashboard.
Boot logs: journalctl -b       Failed services: systemctl --failed
Kernel logs: dmesg            Boot output: Ctrl+Alt+F1
Offline report: python3 -m quirkbench.recovery_reports collect
Review/export/send: python3 -m quirkbench.recovery_reports --help
RAM reports survive UI restart, but are lost on reboot.
'''


def pin_boot_messages() -> None:
    """Keep printk on VT1; console=tty1 alone still follows the foreground VT.

    Linux TIOCL_SETKMSGREDIRECT changes only the virtual-console destination,
    leaving serial output, the kernel ring and journal collection intact.
    """
    fd = os.open('/dev/tty0', os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
    try:
        fcntl.ioctl(fd, 0x541C, bytes((11, 1)))  # TIOCLINUX, SETKMSGREDIRECT, VT1
    finally:
        os.close(fd)


def switch_vt(number: int) -> None:
    """Use the Linux VT ioctl directly; no shell or additional tool dependency."""
    if number not in (1, 2, 3):
        raise ValueError('invalid recovery console')
    fd = os.open('/dev/tty0', os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
    try:
        fcntl.ioctl(fd, 0x5606, number)  # Linux VT_ACTIVATE
    finally:
        os.close(fd)


def present_once(*, marker=Path('/run/quirkbench-console-presented'), switch=switch_vt) -> bool:
    """Activate only the first useful screen this boot, not each UI restart."""
    if marker.exists() or marker.is_symlink():
        return False
    try:
        switch(2)
        # Presentation hint only: it cannot grant readiness or execution.
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b'presented\n')
        finally:
            os.close(fd)
        return True
    except OSError:
        print('Automatic console switch unavailable; Ctrl+Alt+F2 opens Quirkbench.', file=sys.stderr, flush=True)
        return False


def dashboard_available(*, run=subprocess.run) -> bool:
    try:
        return run(['/usr/bin/systemctl', 'is-active', '--quiet', 'quirkbench-console.service'],
                   timeout=2, check=False, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def shell_once(*, run=subprocess.run, available=dashboard_available,
               switch=switch_vt, output=None) -> bool:
    """Run the actual unrestricted shell; failed UI leaves the terminal usable."""
    output = output or sys.stdout
    print(BANNER, file=output, flush=True)
    try:
        run(['/usr/bin/bash', '--noprofile', '--norc', '-i'], check=False)
    except OSError:
        print('The root shell could not start. Inspect the packaged bash dependency.', file=output, flush=True)
        raise RuntimeError('packaged root shell unavailable')
    if available():
        try:
            switch(2)
            return True
        except OSError:
            pass
    print('Dashboard unavailable. This terminal remains open; another shell follows.', file=output, flush=True)
    return False


def main() -> int:
    while True:
        shell_once()


if __name__ == '__main__':
    raise SystemExit(main())
