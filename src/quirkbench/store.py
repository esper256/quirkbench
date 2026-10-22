"""Controller-only SQLite records. Configuration and large outputs remain files."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


class Store:
    def __init__(self, root: Path):
        self.db = sqlite3.connect(root / 'work.sqlite')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))')
        self.db.execute("CREATE TABLE IF NOT EXISTS events (cursor INTEGER PRIMARY KEY, at REAL, kind TEXT, id TEXT, scope TEXT, data TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS mcp_call_lookup ON events(kind,id,cursor)")
        self.db.commit()

    def event(self, kind, key, scope, stage, **fields):
        data = json.dumps(dict(stage=stage, **fields))
        def insert():
            self.db.execute('INSERT INTO events (at,kind,id,scope,data) VALUES (?,?,?,?,?)',
                            (time.time(), kind, key, scope, data))
        if self.db.in_transaction:
            insert()
        else:
            with self.db:
                insert()

    def events(self, scope, after, limit):
        return [dict(cursor=c, at=t, kind=k, id=i, **json.loads(d)) for c,t,k,i,d in self.db.execute(
            "SELECT cursor,at,kind,id,data FROM events WHERE scope=? AND cursor>? AND kind!='mcp-call' ORDER BY cursor LIMIT ?",
            (scope, after, limit))]

    def close(self):
        self.db.close()

    def put(self, kind, key, **data):
        data = dict(data, id=key)
        if self.db.in_transaction:
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (kind, key, json.dumps(data)))
        else:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (kind, key, json.dumps(data)))
        return data

    def get(self, kind, key):
        row = self.db.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
        if row is None:
            raise ValueError(f'No {kind} named {key}.')
        return json.loads(row[0])

    def find(self, kind):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM records WHERE kind=? ORDER BY rowid', (kind,))]

    def change(self, kind, key, **fields):
        data = self.get(kind, key)
        if all(data.get(name) == value for name, value in fields.items()):
            return data
        data.pop('id')
        return self.put(kind, key, **(data | fields))

    @contextmanager
    def admission(self):
        """Commit related request/run records together, before any dispatch.

        Controller-only synchronous use: never await or start physical work here.
        """
        if self.db.in_transaction:
            raise RuntimeError('Admission transactions cannot be nested.')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield

    def request(self, kind, scope, request_id, inputs):
        if not request_id or len(request_id) > 200:
            raise ValueError('Provide a request_id of 1–200 characters for safe retries.')
        key = hashlib.sha256(json.dumps([scope, request_id]).encode()).hexdigest()[:24]
        try:
            work = self.get('work', key)
        except ValueError:
            work = self.put('work', key, action=kind, scope=scope, request_id=request_id,
                            inputs=inputs, status='accepted', stage='Accepted', created_at=time.time(), updated=time.time())
            self.event('work', key, self.get('run', scope)['investigation'] if kind == 'ssh-command' else scope, 'Accepted', action=kind)
            return work, True
        if work['action'] != kind or work['inputs'] != inputs:
            raise ValueError('This request_id already has different inputs. Use a new request_id.')
        return work, False


def new_id(prefix):
    return prefix + '-' + uuid.uuid4().hex[:12]
