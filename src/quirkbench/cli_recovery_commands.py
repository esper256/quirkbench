"""Recovery command arguments; services own behavior and authorization."""
from pathlib import Path
from .cli_parser import action


def build(root):
    p = action(root, 'recovery download', 'Download the recovery image matching the signed installed controller', command='recovery', internal_action='download')
    p.add_argument('--request-id', help='Durable request identity for safe retries')
    p.add_argument('--trust-bundle', type=Path, help='Independently provisioned publisher trust bundle')
    p = action(root, 'recovery list', 'List published recovery images and their retained files', command='recovery-images', internal_action=None)
    p.add_argument('--limit', type=int, default=20, help='Maximum records to return')
    p.add_argument('--before', type=int, default=0, help='older image-operation cursor from the preceding page')
