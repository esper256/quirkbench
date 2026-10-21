"""Investigation controls shared by CLI and MCP. No target reasoning lives here."""
import asyncio

from .files import identifier

from .work_status import COMMAND_DONE, work_finished, command_finished


class InvestigationControls:
    def require_active(self, investigation):
        inv = self.store.get('investigation', investigation)
        if not inv['authorized'] or inv['status'] != 'active':
            raise ValueError('Investigation is paused or stopping. Resume it before starting new work.')
        return inv

    def problem_update(self, investigation, name, description=None, status=None, references=None):
        inv = self.store.get('investigation', investigation)
        identifier(name)
        if status is not None and status not in ('open', 'resolved', 'not-reproduced'):
            raise ValueError('Problem status must be open, resolved or not-reproduced.')
        if description is not None and not description.strip():
            raise ValueError('Provide a nonempty problem description.')
        if references is not None and (not isinstance(references, list) or not all(isinstance(r, str) and r for r in references)):
            raise ValueError('References must be nonempty strings naming related problems, runs or files.')
        problems = {p['name']: p for p in inv['problems']}
        if name not in problems and description is None:
            raise ValueError('Provide a description for a new problem.')
        problem = dict(problems.get(name, {'name': name, 'status': 'open', 'references': []}))
        problem.update({k: v for k, v in dict(description=description, status=status, references=references).items() if v is not None})
        problems[name] = problem
        self.store.change('investigation', investigation, problems=list(problems.values()))
        self.store.event('investigation', investigation, investigation, 'Problem updated', problem=problem)
        return problem

    async def investigation_control(self, investigation, action):
        inv = self.store.get('investigation', investigation)
        if action not in ('pause', 'resume', 'force-stop'):
            raise ValueError('Choose pause, resume or force-stop.')
        if action == 'resume':
            if inv['status'] in ('pausing', 'stopping'):
                raise ValueError('Wait for the pause or force stop to finish. Read investigation context for progress.')
            status = 'active'
        else:
            status = 'stopping' if action == 'force-stop' else 'pausing'
            if inv['status'] == 'paused':
                status = 'paused'
            elif inv['status'] == 'stopping':
                status = 'stopping'
        if status == inv['status']:
            return self.context(investigation)
        self.store.change('investigation', investigation, status=status)
        self.store.event('investigation', investigation, investigation, status)
        self.changed.set()
        return self.context(investigation)

    async def maintain_investigations(self):
        while True:
            for inv in self.store.find('investigation'):
                if inv['status'] not in ('pausing', 'stopping'):
                    continue
                try:
                    await self.advance_pause(inv)
                except (OSError, ValueError, TimeoutError) as error:
                    self.store.change('investigation', inv['id'], attention=str(error))
                except Exception as error:
                    self.store.change('investigation', inv['id'], attention=f'{type(error).__name__}: {error}')
            await asyncio.sleep(self.command_poll_interval)

    async def advance_pause(self, inv):
        name, force = inv['id'], inv['status'] == 'stopping'
        if force:
            for sequence in self.store.find('work'):
                if sequence['action'] == 'sequence' and sequence['scope'] == name and sequence['status'] in ('running', 'paused'):
                    await self.sequences.cancel(sequence['id'])
        works = self.store.find('work')
        runs = {r['id']: r for r in self.store.find('run') if r['investigation'] == name}
        pending = False
        for work in works:
            if work['scope'] != name and work.get('run_id') not in runs:
                continue
            if work['action'] == 'sequence':
                continue
            if work['action'] == 'ssh-command':
                if work.get('run_id') not in runs:
                    continue
                result = await self.command_read(work['id'], stop=force and work['status'] not in COMMAND_DONE)
                pending |= not command_finished(result)
            elif work['action'] == 'prepare' and not work_finished(work['status']):
                if force and work['id'] in self.preparations:
                    self.preparations[work['id']].cancel()
                pending = True
            elif work['action'] in ('deploy', 'boot', 'discard', 'return') and not work_finished(work['status']):
                if force and work['action'] == 'deploy' and not work.get('cancel_requested'):
                    await self.cancel_deployment(work['id'])
                pending = True
        target = self.targets.get(inv['target'], {})
        if target.get('mode') == 'candidate':
            if not force and pending:
                return
            run = runs.get(target.get('run_id'))
            if not run:
                raise ValueError('Another investigation owns the connected candidate; no return was requested.')
            result = await self.return_recovery(run['id'], 'pause-return-' + run['id'])
            if result['status'] in ('failed', 'interrupted'):
                self.store.change('investigation', name, attention=result['stage'] + ' Inspect the target and request return again explicitly.')
            return
        if pending or target.get('mode') != 'recovery':
            self.store.change('investigation', name, attention='Waiting for current work, recovery connection and evidence receipt.')
            return
        retained = target.get('retained_run') or {}
        if retained.get('launched') and not retained.get('evidence_received'):
            return
        self.store.change('investigation', name, status='paused', attention=None)
        self.store.event('investigation', name, name, 'paused')
        self.changed.set()

    def timeline(self, investigation, after=0, limit=200):
        self.store.get('investigation', investigation)
        if not isinstance(after, int) or after < 0 or not 1 <= limit <= 1000:
            raise ValueError('Use a nonnegative cursor and 1–1000 events.')
        events = self.store.events(investigation, after, limit)
        work = [w for w in self.store.find('work') if w['scope'] == investigation or
                (w.get('run_id') and self.store.get('run', w['run_id'])['investigation'] == investigation)]
        return {'events': events, 'next_cursor': events[-1]['cursor'] if events else after,
                'durations': [{'work_id': w['id'], 'action': w['action'], 'status': w['status'],
                               'seconds': w['completed_at'] - w['created_at']}
                              for w in work if w.get('completed_at') is not None],
                'note': 'Controller observations use its clock. External builds are not measured automatically.'}
