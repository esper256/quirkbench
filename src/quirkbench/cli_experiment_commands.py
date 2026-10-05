"""Experiment command arguments; services own behavior and authorization."""
from pathlib import Path
from .cli_parser import action


def build(root):
    p = action(root, 'experiment show', 'Review an exact prepared experiment and its required approvals', command='experiment', internal_action='review')
    p.add_argument('experiment_id', help='Exact immutable experiment ID returned after preparation')
