"""Status command arguments; services own behavior and authorization."""
from pathlib import Path
from .cli_parser import action


def build(root):
    p = action(root, 'status', 'Show controller readiness and work needing attention', command='status', internal_action=None)
