"""Human-readable monitoring with measured bars and explicit uncertainty."""
from datetime import datetime, timezone

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
    for attempt in status['attempts']:
        if attempt['state'] in ('CLAIMED','RUNNING','UNCERTAIN'):
            lease = attempt['lease_until']-progress['sampled_at']
            lines.append(f"Attempt {attempt['id']}: {attempt['state']}; lease {'expired' if lease <= 0 else 'expires in '+duration(lease)}")
            if attempt['state']=='UNCERTAIN':
                lines.append('  Execution needs reconciliation. Lost contact does not prove a kernel crash.')
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
    def __init__(self, controller, campaign_id, phase, message, *, total=None, timeout_s=3600, state='ACTIVE', interval_s=10):
        from .contracts import Progress
        from .controller import uid
        import threading
        self.controller = controller
        self.report = Progress(uid(),campaign_id,phase,state,message,0,completed=0 if total else None,total=total,timeout_s=timeout_s)
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
