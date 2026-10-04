"""Manually opened monitor; closing it never signals an execution service."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil
import sqlite3
import sys
import time

from .contracts import ContractError, canonical
from .monitor import duration, render_operation, render_operation_events
from .operations import operation_response
from .state_reader import StateReader, development_run, safe_text
from .filesystem import read_file


def timing(row, now):
    lines = []
    if row.get('started') is not None:
        end = row.get('updated', now) if row.get('state') in ('SUCCEEDED', 'FAILED') else now
        lines.append('Elapsed: ' + duration(end - row['started']))
    if row.get('heartbeat') is not None:
        lines.append('Last heartbeat: ' + duration(now - row['heartbeat']) + ' ago (liveness only)')
    progress = row.get('progress')
    if isinstance(progress, str):
        progress = json.loads(progress)
    if isinstance(progress, dict) and progress.get('advanced_at') is not None:
        lines.append('Last measured advancement: ' + duration(now - progress['advanced_at']) + ' ago')
    else:
        lines.append('Last measured advancement: unavailable')
    return lines


def summary_lines(snapshot):
    lines = ['Quirkbench — persisted execution facts', 'State: ' + snapshot['state_root']]
    usage = snapshot.get('storage', {})
    if usage:
        lines.append(f"Free space: {usage['free_gib']:.1f} GiB | optional cache limit: {usage['cache_gib']} GiB")
        if usage.get('last_maintenance'):
            lines.append('Last maintenance: ' + json.dumps(usage['last_maintenance'], sort_keys=True))
    for row in snapshot.get('operations', []):
        lines.append(f"Operation {row['id']} {row['kind']} {row['state']} {row.get('stage') or ''}")
    for row in snapshot.get('investigations', []):
        lines.append(f"Investigation {row['id']} {row['state']} | {row.get('reason') or 'no pause reason'}")
    if snapshot.get('investigation_facts'):
        from .investigation_monitor import lines as fact_lines
        lines+=fact_lines(snapshot['investigation_facts'])
    if snapshot.get('run'):
        row = snapshot['run']
        lines.extend([f"Development run {row['run_id']} {row['state']}",
                      'Service: ' + row['unit'], 'Exit status: ' + str(row['exit_status']),
                      'Intermediate progress: unmeasured; inspect logs.'])
    if not snapshot.get('operations') and not snapshot.get('investigations') and not snapshot.get('run'):
        lines.append('No recorded work.')
    return lines


def snapshot(reader, run_id=None, investigation=None):
    root = reader.root
    if run_id and investigation:raise ContractError('choose investigation or development run filtering')
    if run_id:
        answer = {'sampled_at': time.time(), 'operations': [], 'investigations': [],
                  'run': development_run(root, run_id)}
    else:
        with reader.connection() as db:
            db.execute('BEGIN')
            view=reader.on_connection(db)
            answer = view.snapshot(investigation)
            if investigation is not None:
                from .investigation_monitor import facts
                answer['investigation_facts']=facts(view,investigation)
    answer['state_root'] = str(root)
    from .retention_settings import settings
    answer['storage'] = {'free_gib': shutil.disk_usage(root).free / 1024**3, 'cache_gib':settings(root)['cache_gib']}
    try:
        answer['storage']['last_maintenance'] = json.loads(read_file(root, 'maintenance-status.json', limit=8192))
    except FileNotFoundError:
        pass
    if len(canonical(operation_response(data=answer)))>64*1024:
        raise ContractError('monitor summary exceeds query budget')
    return answer


def monitor(root, *, run_id=None, investigation=None, once=False, json_output=False):
    reader = StateReader(root)
    if once or json_output:
        data = snapshot(reader, run_id, investigation)
        print(json.dumps(operation_response(data=data), sort_keys=True) if json_output else '\n'.join(safe_text(line) for line in summary_lines(data)))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ContractError('interactive monitor requires a terminal; use --once or --json')
    import curses

    def draw(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(200)
        selected, details, logs, scroll = 0, False, False, 0
        next_refresh, data, error = 0, None, None
        cursors, events = {}, {}
        detail_lines = []
        while True:
            now = time.time()
            if now >= next_refresh:
                try:
                    data = snapshot(reader, run_id, investigation)
                    rows = ([('run', data['run'])] if run_id else
                            [('operation', row) for row in data['operations']] +
                            [('investigation', row) for row in data['investigations']])
                    selected = min(selected, max(0, len(rows) - 1))
                    detail_lines = []
                    if rows and (details or logs):
                        kind, row = rows[selected]
                        if kind == 'run':
                            record = development_run(reader.root, run_id, logs=logs)
                            detail_lines = summary_lines({'state_root': str(reader.root), 'run': record})
                            if logs:
                                detail_lines += record['log_tail'].splitlines()
                        elif kind == 'operation':
                            op_id = row['id']
                            answer = reader.operation_status(op_id)
                            failure = reader.operation_failure(op_id) if row['state'] == 'FAILED' else None
                            detail_lines = render_operation(answer, failure).splitlines() + timing(answer['data'], now)
                            page = reader.operation_events(op_id, after=cursors.get(op_id, 0), limit=20)
                            new = page['data']['items']
                            if new:
                                cursors[op_id] = new[-1]['id']
                            events[op_id] = (events.get(op_id, []) + new)[-20:]
                            if logs:
                                detail_lines += reader.logs(op_id).splitlines()
                            else:
                                detail_lines += render_operation_events(operation_response(data={'items': events[op_id], 'next_cursor': None})).splitlines()
                        else:
                            record = reader.investigation_detail(row['id'])
                            detail_lines = [f"Investigation {row['id']} {row['state']}", row.get('reason') or 'No pause reason.']
                            for activity in record['activities']:
                                report = json.loads(activity['document'])
                                detail_lines.append(f"{report.get('phase')} {report.get('state')}: {report.get('message')}")
                                detail_lines.append('Last measured advancement: ' + duration(now - activity['advanced']) + ' ago')
                            for attempt in record['attempts']:
                                detail_lines.append(f"Attempt {attempt['id']} {attempt['state']} deadline {attempt['deadline']}")
                            if logs:
                                detail_lines.append('Select an operation to inspect its diagnostic logs.')
                    error = None
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                    error = f'Status unavailable: {safe_text(exc)}'
                next_refresh = now + 2
            screen.erase()
            height, width = screen.getmaxyx()
            lines = ['Quirkbench | arrows/j/k select | Enter details | l logs | PgUp/PgDn scroll | q exit',
                     'Closing this monitor leaves execution running.']
            if error:
                lines.append(error)
            if data:
                lines += summary_lines(data)[:4]
                if details or logs:
                    lines += detail_lines
                else:
                    for index, (kind, row) in enumerate(rows):
                        ident = row.get('id', row.get('run_id'))
                        lines.append(('> ' if index == selected else '  ') + f"{kind} {ident} {row['state']} {row.get('stage') or ''}")
                    if not rows:
                        lines.append('No recorded work.')
            scroll=min(scroll,max(0,len(lines)-max(3,height-1)))
            visible=lines[:2]+lines[2+scroll:]
            for index, line in enumerate(visible[:max(0, height - 1)]):
                try:
                    screen.addnstr(index, 0, safe_text(line).replace('\n', ' '), max(0, width - 1))
                except curses.error:
                    pass
            screen.refresh()
            key = screen.getch()
            if key in (ord('q'), 27):
                return 0
            if key in (curses.KEY_DOWN, ord('j')):
                selected += 1
                scroll=0
                next_refresh = 0
            elif key in (curses.KEY_UP, ord('k')):
                selected = max(0, selected - 1)
                scroll=0
                next_refresh = 0
            elif key in (10, 13, curses.KEY_ENTER):
                details = not details
                next_refresh = 0
            elif key == ord('l'):
                logs = not logs
                scroll=0
                next_refresh = 0
            elif key == curses.KEY_NPAGE:
                scroll+=max(1,height-4)
            elif key == curses.KEY_PPAGE:
                scroll=max(0,scroll-max(1,height-4))
            elif key == curses.KEY_RESIZE:
                next_refresh = 0
    try:
        return curses.wrapper(draw)
    except curses.error as exc:
        raise ContractError('terminal cannot initialize the TUI; use --once') from exc
