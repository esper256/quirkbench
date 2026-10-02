"""Bounded allowlisted evidence primitives shared by runner and pytest plugin."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

FILE_LIMIT = 64 * 1024
ATTACHMENT_LIMIT = 2 * 1024 * 1024
LOG_LIMIT = 1024 * 1024
REPORT_LIMIT = 1024 * 1024
JUNIT_LIMIT = 2 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    text = str(value)
    text = re.sub(r'-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)',
                  '[REDACTED PRIVATE KEY]', text, flags=re.S)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'''(?ix)(["']?(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[REDACTED]@', text)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[REDACTED]', text)
    return text


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    data = (json.dumps(value, indent=2) + '\n').encode()
    if len(data) > 256 * 1024:
        raise ValueError('JSON evidence exceeds 256 KiB')
    temporary.write_bytes(data)
    temporary.replace(path)


def append_report(bundle, value):
    path = Path(bundle) / 'tests.jsonl'
    data = (json.dumps(value) + '\n').encode()
    if (path.stat().st_size if path.exists() else 0) + len(data) <= REPORT_LIMIT:
        with path.open('ab') as out:
            out.write(data)
    else:
        (Path(bundle) / 'reports-truncated.txt').write_text('Test report byte budget exhausted.\n')


def capture_stage(bundle, stage, nodeid=None):
    """Capture only state/error fields; never copy worker inputs, keys, or result trees.

    Call BEFORE deleting a disposable stage. Missing/unsafe/oversize input is
    evidence incompleteness, never a replacement test outcome.
    """
    bundle, stage = Path(bundle), Path(stage).absolute()
    folder = bundle / 'workers'
    folder.mkdir(exist_ok=True)
    files = list(folder.iterdir())
    if len(files) >= 128 or sum(p.stat().st_size for p in files) >= ATTACHMENT_LIMIT - FILE_LIMIT:
        (bundle / 'workers-truncated.txt').write_text('Worker evidence count/byte budget exhausted.\n')
        return
    record = {'status': 'missing', 'captured_at': now(), 'nodeid': redact(nodeid)[:2048] if nodeid else None}
    try:
        path = stage / 'diagnostics/stage-result.json'
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('linked diagnostic path')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > FILE_LIMIT:
                raise ValueError('unsafe or oversized diagnostic')
            data = os.read(fd, FILE_LIMIT + 1)
        finally:
            os.close(fd)
        if len(data) > FILE_LIMIT:
            raise ValueError('oversized diagnostic')
        value = json.loads(data)
        record['status'] = 'captured'
        record['stage-result'] = {key: redact(value[key])[:4096] for key in ('state', 'error')
                                  if isinstance(value.get(key), str)}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        record.update(status='unavailable', reason=type(exc).__name__)
    write_json(folder / f'{len(files):04d}.json', record)
    return record
