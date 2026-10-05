"""Monitor command arguments; services own behavior and authorization."""
from pathlib import Path
from .cli_parser import action


def build(root):
    p = action(root, 'monitor', 'Watch investigation progress', command='monitor', internal_action=None)
    p.add_argument('investigation', nargs='?', help='filter existing investigation facts and actionable waits')
    p.set_defaults(run_id=None)
    p.add_argument('--once', action='store_true', help='Print one progress snapshot and exit')
