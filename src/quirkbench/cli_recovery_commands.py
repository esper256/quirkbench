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
    p = action(root, 'recovery prepare', 'Plan or explicitly write the final USB layout and controller enrollment', command='recovery', internal_action='prepare')
    p.add_argument('--image', type=Path, help='Current verified recovery image for planning or apply')
    p.add_argument('--device', type=Path, help='Explicit whole USB device; all existing contents will be erased on apply')
    p.add_argument('--target', help='Name for the computer that will first enroll from this USB')
    p.add_argument('--plan-out', type=Path, help='New device-bound planning output; writes no USB bytes')
    p.add_argument('--plan', type=Path, help='Apply this saved preparation plan')
    p.add_argument('--confirm', help='Exact confirmation SHA-256 returned by planning')
    p.add_argument('--erase', action='store_true', help='Acknowledge loss of all existing USB data and credentials')
    p.add_argument('--output', type=Path, help='New retained staging/diagnostics directory for apply')
    p.add_argument('--public-key', type=Path, help='Independent publisher public key for the selected signed image')
    p.add_argument('--fingerprint', help='Independently verified full publisher fingerprint')
    p.add_argument('--unsigned-development', action='store_true', help='Explicitly use an unsigned local development image; confers no publisher trust')
