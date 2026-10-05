"""Run command arguments; services own behavior and authorization."""
from pathlib import Path
from .cli_parser import action


def build(root):
    p = action(root, 'run show', 'Review an exact target run and its results', command='attempt', internal_action='show')
    p.add_argument('attempt_id', help='Exact run ID')
    p = action(root, 'run approve', 'Approve execution of this exact prepared run', command='attempt', internal_action='approve')
    p.add_argument('attempt_id', help='Exact run ID')
    p.add_argument('--request-id', help='durable unique decision ID; omission derives it from the complete exact binding')
    p.add_argument('--operator', help='local operator attribution; default current UID')
    p = action(root, 'run reject', 'Reject this exact target run', command='attempt', internal_action='reject')
    p.add_argument('attempt_id', help='Exact run ID')
    p.add_argument('--request-id', help='durable unique decision ID; omission derives it from the complete exact binding')
    p.add_argument('--operator', help='local operator attribution; default current UID')
    p = action(root, 'run resolve', 'Resolve an interrupted run without authorizing another execution', command='resolve', internal_action=None)
    p.add_argument('attempt_id', help='Exact run ID')
    p.add_argument('disposition', choices=['retry', 'abandon'], help='Retry only after reconciliation, or abandon this run')
    p.add_argument('--note', required=True, help='Reason for this action, retained with its record')
