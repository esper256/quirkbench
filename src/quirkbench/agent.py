"""Provider-neutral agent invocation and explicit, credential-excluding snapshots."""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from .contracts import Checkpoint, ContractError, canonical

SENSITIVE = {'.git', '.ssh', '.aws', '.azure', '.config', '.codex', '.env', 'credentials', 'credentials.json', 'auth.json', 'token', 'tokens', 'id_rsa', 'id_ed25519'}

class AgentError(RuntimeError):
    pass

class ScriptedAgent:
    def decide(self, context):
        return {'hypothesis': 'Durable protocol execution survives controller restart.', 'summary': 'Run the simulation smoke recipe.', 'rejected_approaches': [], 'input_tokens': 0, 'output_tokens': 0, 'simulated': True}

class CommandAgent:
    """Local trusted adapter command, JSON stdin/stdout, never a shell string.

    Provider authentication is inherited from the controller environment. Raw output
    is private temporary data and is never automatically added to evidence.
    """
    def __init__(self, argv, timeout_s=600):
        if not argv or not all(isinstance(x, str) for x in argv):
            raise ContractError('agent command must be a nonempty argv list')
        self.argv = argv
        self.timeout_s = timeout_s

    def decide(self, context):
        import signal
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=output, stderr=errors, start_new_session=True)
            try:
                process.communicate(canonical(context), timeout=self.timeout_s)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise AgentError('agent decision timed out') from exc
            if process.returncode:
                raise AgentError('agent command failed; check authentication and adapter locally')
            output.seek(0)
            raw = output.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise AgentError('agent response exceeds 1 MiB')
        try:
            value = json.loads(raw)
            allowed = {'hypothesis', 'summary', 'rejected_approaches', 'input_tokens', 'output_tokens', 'experiment'}
            if not isinstance(value, dict) or set(value) - allowed or not {'hypothesis','summary','input_tokens','output_tokens'} <= set(value):
                raise ContractError('invalid decision fields')
            if any(type(value[key]) is not int or value[key] < 0 for key in ('input_tokens','output_tokens')):
                raise ContractError('agent must report nonnegative token usage')
            if not all(isinstance(value[key], str) and value[key].strip() for key in ('hypothesis','summary')):
                raise ContractError('agent must report hypothesis and summary')
            canonical(value)
        except (ValueError, TypeError) as exc:
            raise AgentError('invalid agent decision; raw output was not retained') from exc
        return value


def compact_context(controller, campaign_id):
    status = controller.status(campaign_id)
    attempts = []
    for row in status['attempts'][-20:]:
        result = json.loads(row['result']) if row['result'] else None
        attempts.append({'attempt_id': row['id'], 'state': row['state'], 'result': result})
    with controller.transaction() as db:
        ledger = [json.loads(row[0]) for row in db.execute('SELECT document FROM ledger WHERE campaign=? ORDER BY id DESC LIMIT 10', (campaign_id,))]
        checkpoints = [dict(row) for row in db.execute('SELECT id,document FROM checkpoints WHERE campaign=? ORDER BY rowid DESC LIMIT 5', (campaign_id,))]
    return {'schema_version': 1, 'campaign_id': campaign_id, 'state': status['state'], 'total_tokens': status['total_tokens'], 'jobs': status['jobs'], 'recent_attempts': attempts, 'recent_decisions': ledger, 'checkpoints': checkpoints}


def run_decision(controller, campaign_id, adapter, decision_id=None):
    from .investigations import require_managed_invocation
    require_managed_invocation(controller,campaign_id)
    status = controller.status(campaign_id)
    if status['state'] != 'RUNNING':
        raise AgentError('resume campaign before starting an agent decision')
    try:
        from .monitor import Activity
        with Activity(controller, campaign_id, 'agent-decision', 'Waiting for coding-agent output; intermediate token progress is unavailable.', state='WAITING', timeout_s=int(getattr(adapter, 'timeout_s', 600))):
            decision = adapter.decide(compact_context(controller, campaign_id))
        tokens = decision.get('input_tokens', 0) + decision.get('output_tokens', 0)
        controller.record_decision(campaign_id, decision, tokens, decision_id)
        if 'experiment' in decision:
            from .contracts import Experiment
            controller.submit_attended(campaign_id, Experiment.from_dict(decision['experiment']))
        return decision
    except Exception:
        controller.pause(campaign_id, 'agent decision failed; inspect authentication or adapter')
        raise


def snapshot_sources(controller, campaign_id, root, files, notes=None):
    """Capture unfinished edits from an explicit allowlist, never an entire home."""
    root = Path(root).resolve()
    entries = []
    for filename in files:
        path = Path(filename)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ContractError('snapshot requires safe relative filenames')
        if any(part.lower() in SENSITIVE or part.lower().endswith(('.pem', '.key', '.p12')) or part.lower().startswith('.env') for part in path.parts):
            raise ContractError('credential-like paths cannot be snapshotted')
        source = root / path
        if source.is_symlink() or not source.is_file() or root not in source.resolve().parents:
            raise ContractError('snapshot requires regular files within source root')
        entries.append((path, source))
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as archive:
        for path, source in sorted(entries):
            content = source.read_bytes()
            info = tarfile.TarInfo(str(path)); info.size = len(content); info.mode = 0o644; info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    artifact = controller.store.put(data.getvalue())
    return controller.checkpoint(Checkpoint(campaign_id, [artifact.sha256], notes or {}))
