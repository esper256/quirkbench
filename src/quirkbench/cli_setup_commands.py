"""Setup command arguments; services own behavior and authorization."""
from pathlib import Path
from .cli_parser import action


def build(root):
    p = action(root, 'setup', 'Configure this controller', command='setup', internal_action=None)
    p.add_argument('--request-id', help='durable retry identity')
    p.add_argument('--runtime', type=Path, help='verified immutable installed runtime root')
    p.add_argument('--cache-gib', type=int, help='Maximum intermediate build cache size in GiB')
    p.add_argument('--reserve-gib', dest='setup_reserve_gib', type=float, help='Minimum free storage to preserve in GiB')
    p.add_argument('--host', help='record the literal controller bind IP')
    p.add_argument('--port', type=int, help='Controller HTTPS listening port')
    p.add_argument('--allow-lan', action='store_true', default=None, help='Allow binding to the explicitly selected LAN address')
    p.add_argument('--logout-policy', choices=['session'], help='foreground session lifetime; daemon packaging is separate')
    p.add_argument('--builder-archive', type=Path, help='admit signed OCI capture/import to the existing worker')
    p.add_argument('--builder-request-id', help='builder retry identity; defaults to the setup request ID plus -builder')
    p.add_argument('--configure-controller', dest='start_service', action='store_true', help='Configure controller TLS and connection settings; leaves your CLI link unchanged; start with admin controller run')
