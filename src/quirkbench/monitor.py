"""Human-readable monitoring with measured bars and explicit uncertainty."""
from datetime import datetime, timezone
import json
import re

def duration(seconds):
    if seconds is None:
        return 'unknown'
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f'{hours:d}h {minutes:02d}m {seconds:02d}s' if hours else f'{minutes:d}m {seconds:02d}s'

def bar(done, total, width=20):
    if not total:
        return 'no measured denominator'
    filled = min(width, int(width * done / total))
    return '[' + '#' * filled + '-' * (width-filled) + f'] {done}/{total} ({100*done/total:.0f}%)'


def render_operation(status, failure=None):
    """Render persisted operation facts without treating liveness as progress."""
    row = status['data']
    lines = [f"Operation {row['id']}  {row['kind']}  {row['state']}"]
    if row['stage']:
        lines.append('Stage: ' + row['stage'])
    if row['state'] == 'WAITING':
        lines.append('Waiting for: ' + (row['wait_event'] or 'unspecified event'))
    if row['deadline'] is not None and row['state'] in ('RUNNING', 'WAITING'):
        deadline = datetime.fromtimestamp(row['deadline'], timezone.utc).isoformat(timespec='seconds')
        lines.append('Deadline: ' + deadline)
    try:
        progress = json.loads(row['progress']) if isinstance(row['progress'], str) else row['progress']
    except (TypeError, ValueError):
        progress = None
    if isinstance(progress, dict):
        phase = progress.get('phase')
        state = progress.get('state')
        message = progress.get('message')
        if isinstance(phase, str) and isinstance(state, str) and isinstance(message, str):
            lines.append(f'{phase}: {state} | {message}')
        completed, total, unit = (progress.get(name) for name in ('completed', 'total', 'unit'))
        if (type(completed) is int and completed >= 0 and type(total) is int
                and total > 0 and completed <= total and isinstance(unit, str)):
            lines.append('Measured: ' + bar(completed, total) + ' ' + unit)
        elif type(completed) is int and completed >= 0 and isinstance(unit, str):
            lines.append(f'Measured: {completed} {unit}; total unknown')
        else:
            lines.append('Measured progress: unavailable')
    else:
        lines.append('Measured progress: unavailable')
    if row['state'] == 'FAILED':
        if failure is None:
            lines.append('Failure details unavailable; historical state remains FAILED.')
        else:
            lines.append(f"Failure {failure['code']}: {failure['message']}")
    if row['state'] == 'INTERRUPTED':
        lines.append('Worker ownership was interrupted; continuation requires reconciliation.')
    if row['references']['output']:
        lines.append(f"Public outputs: {len(row['references']['output'])}; inspect digests with --json")
    return '\n'.join(lines)


def render_operation_events(page):
    """Render known persisted event facts without dumping arbitrary documents."""
    lines = []
    for item in page['data']['items']:
        try:
            created = datetime.fromtimestamp(item['created'], timezone.utc).isoformat(timespec='seconds')
        except (OverflowError, OSError, TypeError, ValueError):
            created = 'time unavailable'
        kind = item['kind'] if (isinstance(item['kind'], str) and
                                re.fullmatch(r'[a-z][a-z_]{0,63}', item['kind'])) else 'invalid_event'
        document = item['document']
        fields = []
        if isinstance(document, dict):
            allowed = {
                'accepted': ('state',),
                'claimed': ('stage', 'worker_generation'),
                'resumed': ('worker_epoch',),
                'worker_stopped': ('proof',),
                'output': ('state',),
                'finished': ('state',),
            }.get(kind, ())
            for name in allowed:
                value = document.get(name)
                if ((type(value) is int and 0 <= value < 10**12)
                        or (isinstance(value, str) and value.isascii()
                            and 0 < len(value) <= 64 and value.isprintable())):
                    fields.append(f'{name}={value}')
            if kind in ('output', 'finished') and isinstance(document.get('outputs'), list):
                fields.append(f"outputs={len(document['outputs'])}")
        suffix = '  ' + ' '.join(fields) if fields else ''
        lines.append(f"{item['id']}  {created}  {kind}{suffix}")
    if page['data']['next_cursor'] is not None:
        lines.append(f"More events: --after {page['data']['next_cursor']}")
    return '\n'.join(lines)

def render(status):
    progress = status['progress']
    sampled = datetime.fromtimestamp(progress['sampled_at'], timezone.utc).isoformat(timespec='seconds')
    lines = [f"Campaign {status['id']}  {status['state']}  | snapshot {sampled}",
             f"Jobs {bar(progress['completed_jobs'], progress['total_jobs'])}  | cumulative tokens {status['total_tokens']}"]
    if status['reason']:
        lines.append('Reason: ' + status['reason'])
    target = status['target']
    age = target['contact_age_s']
    contact = 'NO CONTACT RECORDED' if age is None else 'CONTACT LATE' if age > 60 else 'recent contact'
    lines.append(f"Target {status['device']} ({target['mode']}, boot {target['boot_id']}): {contact}; last contact {duration(age)} ago")
    inventory = target.get('inventory', {})
    if inventory.get('boot_stage'):
        lines.append('Last reported boot stage: ' + inventory['boot_stage'])
    watchdog = inventory.get('watchdog')
    if watchdog:
        armed = {True: 'armed', False: 'inactive', None: 'unknown'}.get(watchdog.get('armed'), 'unknown')
        timeout = watchdog.get('actual_timeout_s')
        lines.append(f"Watchdog: {watchdog.get('identity') or 'unidentified'}; {armed}; observed timeout {str(timeout)+'s' if timeout is not None else 'unknown'}; coverage starts {watchdog.get('earliest_covered_stage', 'unqualified')} (last target report)")
    for name, values in inventory.get('partition_capacity', {}).items():
        lines.append(f"Storage {name}: {values['available_bytes']/1024**3:.1f} GiB available / {values['total_bytes']/1024**3:.1f} GiB (last target report)")
    for attempt in status['attempts']:
        if attempt['state'] in ('CLAIMED','RUNNING','BOOT_PENDING','UNCERTAIN'):
            lease = attempt['lease_until']-progress['sampled_at']
            lines.append(f"Attempt {attempt['id']}: {attempt['state']}; lease {'expired' if lease <= 0 else 'expires in '+duration(lease)}")
            if attempt['state']=='UNCERTAIN':
                lines.append('  Execution needs reconciliation. Lost contact does not prove a kernel crash.')
    observations = status.get('observations')
    if observations:
        for item in observations['items']:
            if item['state'] in ('pending', 'overdue'):
                request = item['request']
                lines.append(f"Human request {request['request_id']} ({request['kind']}): {item['state']}; deadline {request['deadline_at']}")
                lines.append('  ' + request['prompt'])
                if item.get('truncated'):
                    lines.append(f"  Full record: quirkbench session observation {observations['session_id']} --request {request['request_id']}")
        if observations['next_cursor'] is not None:
            lines.append(f"More human requests: quirkbench session observations {observations['session_id']} --after {observations['next_cursor']}")
    active = [a for a in progress['activities'] if a['state'] not in ('COMPLETE','FAILED')]
    recent = [a for a in progress['activities'] if a['state'] in ('COMPLETE','FAILED')][-10:]
    for activity in active+recent:
        measure = bar(activity['completed'],activity['total'])+' '+activity['unit'] if activity['total'] else (str(activity['completed'])+' '+activity['unit'] if activity['completed'] is not None else 'percentage unknown')
        lines.append(f"{activity['phase']}: {activity['health']} | {measure} | elapsed {duration(activity['elapsed_s'])}")
        lines.append(f"  {activity['message']}")
        if activity['state'] not in ('COMPLETE','FAILED'):
            lines.append(f"  Report {duration(activity['last_report_age_s'])} ago; advancement {duration(activity['last_advance_age_s'])} ago; deadline {'OVERDUE' if activity['deadline_in_s'] <= 0 else 'in '+duration(activity['deadline_in_s'])}")
    if not active:
        if status['state']=='RUNNING' and progress['completed_jobs'] < progress['total_jobs']:
            lines.append('Waiting for target claim; check target contact, required capabilities, and outstanding attempts.')
        elif status['state']=='RUNNING':
            lines.append('Queue drained; waiting for the next agent decision or submission.')
        else:
            lines.append('Scheduling paused; use explicit resume when ready.')
    lines.append('A responsive monitor proves the controller database is reachable; a heartbeat alone does not prove useful work is advancing.')
    return '\n'.join(lines)

class Activity:
    """Small adapter helper: periodic liveness is distinct from useful advancement."""
    def __init__(self, controller, campaign_id, phase, message, *, total=None, timeout_s=3600, state='ACTIVE', interval_s=10, unit='items'):
        from .contracts import Progress
        from .controller import uid
        import threading
        self.controller = controller
        self.report = Progress(uid(),campaign_id,phase,state,message,0,completed=0 if total else None,total=total,timeout_s=timeout_s,unit=unit)
        self.interval_s = interval_s
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.error = None

    def _emit(self, **changes):
        from dataclasses import replace
        with self.lock:
            self.report = replace(self.report, sequence=self.report.sequence+1, **changes)
            self.controller.progress(self.report)

    def update(self, message, *, completed=None, phase=None, state=None):
        changes = {'message': message}
        if completed is not None:
            changes['completed'] = completed
        if phase is not None:
            changes['phase'] = phase
        if state is not None:
            changes['state'] = state
        self._emit(**changes)

    def __enter__(self):
        import threading
        self.controller.progress(self.report)
        def pulse():
            while not self.stop.wait(self.interval_s):
                try:
                    self._emit()
                except Exception as exc:
                    self.error = type(exc).__name__
                    return
        self.thread = threading.Thread(target=pulse,daemon=True)
        self.thread.start()
        return self

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        self.thread.join(timeout=35)
        changes = {'state': 'FAILED' if kind else 'COMPLETE'}
        if kind:
            changes['message'] = 'Operation failed; durable state retained. Error class: '+kind.__name__
        elif self.report.total:
            changes['completed'] = self.report.total
        try:
            self._emit(**changes)
        except Exception:
            if kind is None:
                raise
        return False


class PhaseReporter:
    """Persist bounded command progress without counting heartbeat time as work."""
    def __init__(self, controller=None, campaign_id=None, output=None):
        self.controller, self.campaign_id, self.output = controller, campaign_id, output
        self.activity = None

    def __call__(self, record):
        if self.output:
            self.output(record)
        if not self.controller or not self.campaign_id:
            return
        phase = record['phase']
        waiting = record['status'] == 'waiting'
        state = 'WAITING' if waiting else 'ACTIVE'
        message = ('Waiting for output or completion: ' if waiting else 'Running ') + phase
        if self.activity is None:
            self.activity = Activity(self.controller, self.campaign_id, phase,
                                     message, state=state, timeout_s=record.get('timeout_s', 3600), unit='output-bytes')
            self.activity.__enter__()
        elif self.activity.report.phase != phase:
            raise ValueError('operation changed phase before completing its previous phase')
        self.activity.update(message, completed=record.get('output_bytes', 0), state=state)
        if record['status'] == 'complete':
            self.activity.__exit__(None, None, None)
            self.activity = None

    def fail(self, error):
        if self.activity:
            self.activity.__exit__(type(error), error, error.__traceback__)
            self.activity = None
