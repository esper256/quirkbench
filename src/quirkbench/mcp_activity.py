"""Small local MCP call records, without command text, environment or output copies."""
import asyncio
import hashlib
import json
import logging
import math
import time

from mcp.server.fastmcp import FastMCP


class RecordedMCP(FastMCP):
    def __init__(self, controller, *args, **kwargs):
        self.controller = controller
        super().__init__(*args, **kwargs)

    def scope(self, arguments):
        store = self.controller.store
        arguments = {k:v for k,v in arguments.items() if isinstance(v, str)}
        try:
            if arguments.get('investigation'):
                return store.get('investigation', arguments['investigation'])['id']
            run = arguments.get('run_id')
            key = arguments.get('command_id') or arguments.get('work_id')
            if key:
                work = store.get('work', key)
                if work['action'] != 'ssh-command':
                    return work['scope']
                run = work['run_id']
            if run:
                return store.get('run', run)['investigation']
        except (ValueError, TypeError):
            pass
        return ''

    async def call_tool(self, name, arguments):
        started = time.time()
        clock = time.monotonic()
        result, outcome = None, 'error'
        try:
            result = await super().call_tool(name, arguments)
            outcome = 'returned'
            return result
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        finally:
            # Recording must never turn a successfully admitted action into a lost reply.
            try:
                body = result[1] if isinstance(result, tuple) else {}
                observed = {k:body[k] for k in ('status', 'complete', 'outcome', 'next_offset',
                            'output_bytes', 'more', 'saving_pending', 'evidence') if k in body}
                if 'stage' in body:
                    observed['stage_digest'] = hashlib.sha256(str(body['stage']).encode()).hexdigest()[:24]
                refs = {k:arguments[k] for k in ('investigation', 'run_id', 'command_id', 'work_id', 'target', 'view', 'saved_file', 'evidence_file') if k in arguments}
                key = hashlib.sha256(json.dumps([name, refs], sort_keys=True).encode()).hexdigest()[:24]
                scope = self.scope(arguments)
                previous = self.controller.store.db.execute(
                    "SELECT data FROM events WHERE kind='mcp-call' AND id=? ORDER BY cursor DESC LIMIT 1", (key,)).fetchone()
                unchanged = outcome == 'returned' and bool(observed) and previous is not None and json.loads(previous[0]).get('observed') == observed
                sizes = [len(block.text.encode()) for block in result[0] if getattr(block, 'type', None) == 'text'] if isinstance(result, tuple) else []
                self.controller.store.event('mcp-call', key, scope, outcome, tool=name[:200],
                    started_at=started, elapsed_seconds=round(time.monotonic()-clock, 6),
                    response_text_bytes=sum(sizes) if isinstance(result, tuple) else None, observed=observed, unchanged=unchanged,
                    **{k:arguments[k] for k in ('offset', 'after', 'limit', 'timeout_seconds', 'wait_seconds')
                       if isinstance(arguments.get(k), (int, float)) and (not isinstance(arguments[k], float) or math.isfinite(arguments[k]))})
            except Exception:
                logging.getLogger(__name__).exception('Could not record MCP call statistics')


def activity(store, investigation=None, after=0, limit=100):
    if investigation is not None:
        store.get('investigation', investigation)
    clause, args = (" AND scope=?", [investigation]) if investigation is not None else ('', [])
    rows = store.db.execute("SELECT cursor,at,data FROM events WHERE kind='mcp-call'" + clause +
                            ' AND cursor>? ORDER BY cursor LIMIT ?', [*args, after, limit+1]).fetchall()
    page = rows[:limit]
    calls = [dict(cursor=c, finished_at=t, **json.loads(d)) for c,t,d in page]
    totals = store.db.execute("SELECT json_extract(data,'$.tool'),count(*),sum(json_extract(data,'$.elapsed_seconds')),"
        "sum(json_extract(data,'$.response_text_bytes')),sum(json_extract(data,'$.unchanged')) "
        "FROM events WHERE kind='mcp-call'" + clause + " GROUP BY json_extract(data,'$.tool')", args).fetchall()
    return dict(calls=calls, next_cursor=page[-1][0] if page else after, more=len(rows)>limit,
                totals=[dict(tool=n, calls=c, elapsed_seconds=s, response_text_bytes=b or 0, unchanged=u or 0) for n,c,s,b,u in totals],
                note='Totals cover completed/cancelled calls, not tokens; an abrupt controller exit can omit in-flight calls. Response size counts successful text content, not wire bytes. Unchanged compares returned status/output position; it does not prove a call was unnecessary. Internal polling is excluded. Unscoped calls appear in controller-wide results.')
