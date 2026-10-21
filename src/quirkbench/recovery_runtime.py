"""Small Linux controls which need no programs or files from recovery media."""
import ctypes
import fcntl
import os
from pathlib import Path
import socket
import struct

# Load this while the recovery drive is healthy, not on the emergency path.
_LIBC = ctypes.CDLL(None, use_errno=True)


def keep_resident():
    """Fault in and lock existing mappings; no unbounded locking of future memory."""
    # SSL loads this codec on its first connection, even for numeric addresses.
    # Load it before losing the drive, then lock its dependencies too.
    "controller.invalid".encode("idna")
    if _LIBC.mlockall(1) != 0:  # MCL_CURRENT
        return f'Emergency controls may need the drive: {os.strerror(ctypes.get_errno())}'
    return ''


class EmergencyPower:
    def __init__(self):
        self.fd = None
        self.error = 'Emergency power controls have not been initialized.'

    def prepare(self):
        try:
            self.fd = os.open('/proc/sysrq-trigger', os.O_WRONLY | os.O_CLOEXEC)
            self.error = ''
        except OSError as error:
            self.error = f'Emergency power unavailable: {error}'

    def trigger(self, action):
        if action not in ('restart', 'shutdown'):
            raise ValueError('Choose restart or shutdown.')
        if self.fd is None:
            raise OSError(self.error)
        # No sync, storage cleanup, boot edits or subprocesses on this path.
        os.write(self.fd, b'b' if action == 'restart' else b'o')


def network_facts():
    """Read interface addresses from the running kernel, independent of NetworkManager."""
    result = {'network': 'No address assigned', 'ip': 'Not assigned'}
    try:
        addresses = {}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for _, name in socket.if_nameindex():
                request = struct.pack('256s', name.encode()[:15])
                flags = struct.unpack_from('H', fcntl.ioctl(sock, 0x8913, request), 16)[0]  # SIOCGIFFLAGS
                if not flags & 1 or flags & 8:  # IFF_UP, IFF_LOOPBACK
                    continue
                addresses[name] = []
                try:
                    response = fcntl.ioctl(sock, 0x8915, request)  # SIOCGIFADDR
                    addresses[name].append(socket.inet_ntop(socket.AF_INET, response[20:24]))
                except OSError:
                    pass
        try:
            for line in Path('/proc/net/if_inet6').read_text().splitlines():
                address, _, _, _, _, name = line.split()
                if name in addresses:
                    addresses[name].append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(address)))
        except FileNotFoundError:
            pass  # IPv6 may be disabled.
        active = {name: ips for name, ips in addresses.items() if ips}
        if active:
            result.update(network=', '.join(active), ip=', '.join(ip for ips in active.values() for ip in ips))
    except (OSError, ValueError):
        result.update(network='Unavailable', ip='Unavailable')
    return result
