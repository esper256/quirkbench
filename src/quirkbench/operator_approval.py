"""Local operator decisions bound to an exact physical attempt; no model grants."""
from __future__ import annotations
import json
import os

from .contracts import Conflict, canonical, digest, identifier

CAPABILITY = 'operator-approval.v1'
MIGRATION = '''
ALTER TABLE attempts ADD COLUMN approval_required INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attempts ADD COLUMN approval_inventory TEXT;
CREATE TABLE attempt_approval_commands(seq INTEGER PRIMARY KEY, request TEXT NOT NULL UNIQUE,
 attempt TEXT NOT NULL REFERENCES attempts(id), document TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX attempt_approval_scope ON attempt_approval_commands(attempt,seq);
'''


def required(spec, report):
    return CAPABILITY in spec.get('required_capabilities', []) or CAPABILITY in report.get('capabilities', [])


class OperatorApprovals:
    def _approval_context(self, db, row):
        device=db.execute('SELECT * FROM devices WHERE id=?',(row['device'],)).fetchone()
        report=json.loads(device['report'])
        job=db.execute('SELECT j.campaign,e.spec FROM jobs j JOIN experiments e ON e.id=j.experiment WHERE j.id=?',(row['job'],)).fetchone()
        spec=json.loads(job['spec'])
        manifest=self._deployment_manifest(spec['artifacts'].get('deployment'))
        if not row["approval_required"] and not required(spec,report): return None,job['campaign']
        if CAPABILITY not in report['capabilities']:
            raise Conflict('runtime does not support required operator approval')
        inventory=json.loads(row['approval_inventory']) if row['approval_inventory'] else report.get('inventory',{})
        media=inventory.get('media_instance_id')
        binding=inventory.get('target_binding')
        if not isinstance(media,str) or not media or not isinstance(binding,dict):
            raise Conflict('operator approval requires media and target binding')
        identifier(media)
        from .binding import system_uuid
        if set(binding)!={'schema_version','system_uuid'} or type(binding['schema_version']) is not int or binding['schema_version']!=1:
            raise Conflict('operator approval target binding invalid')
        system_uuid(binding['system_uuid'])
        for other in db.execute('SELECT report FROM devices WHERE id!=?',(row['device'],)):
            observed=json.loads(other['report']).get('inventory',{}).get('target_binding',{})
            if isinstance(observed,dict) and observed.get('system_uuid','').lower()==binding['system_uuid'].lower():
                raise Conflict('duplicate registered target system UUID')

        epoch=db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
        return {'attempt_id':row['id'],'device_id':row['device'],'media_instance_id':media,
                'target_binding':binding,'origin_boot_id':row['boot'],'generation':row['generation'],
                'experiment_digest':digest(canonical(spec)), 'deployment_digest':spec['artifacts']['deployment'],
                'revision':manifest.revision,'deadline':row['deadline'],'controller_epoch':epoch},job['campaign']

    def operator_attempt_status(self, attempt_id):
        identifier(attempt_id)
        with self.transaction() as db:
            row=db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if row is None: raise Conflict('unknown attempt')
            return {'attempt_id':attempt_id,'attempt_state':row['state'],**self._approval_status(db,row)}

    def decide_attempt(self, attempt_id, decision, *, request_id, operator=None):
        """Local administration only. Rejection cannot undo an already issued handoff."""
        identifier(attempt_id); identifier(request_id)
        if decision not in {'approved','rejected'}: raise ValueError('invalid operator decision')
        operator=operator or 'uid:'+str(os.getuid())
        if not isinstance(operator,str) or not operator or len(operator)>128: raise ValueError('invalid operator attribution')
        self._expire()
        with self.transaction() as db:
            row=db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if row is None: raise Conflict('unknown attempt')
            from .credential_registry import require_execution_credentials
            require_execution_credentials(db,row['device'],self.clock(),expected_generation=row['credential_generation'])
            context,campaign=self._approval_context(db,row)
            if context is None: raise Conflict('legacy attempt has no negotiated approval contract')
            current_inventory=json.loads(db.execute('SELECT report FROM devices WHERE id=?',(row['device'],)).fetchone()[0]).get('inventory',{})
            if any(current_inventory.get(key)!=context[key] for key in ('media_instance_id','target_binding')):
                raise Conflict('claimed media or target identity changed')
            document={'schema_version':1,'decision':decision,'operator':operator,'binding':context}
            raw=canonical(document).decode()
            previous=db.execute('SELECT document FROM attempt_approval_commands WHERE request=?',(request_id,)).fetchone()
            if previous:
                if previous['document']!=raw: raise Conflict('changed operator decision replay')
                return document
            self._live(db,row['id'],row['token'],row['boot'])
            report=json.loads(db.execute('SELECT report FROM devices WHERE id=?',(row['device'],)).fetchone()[0])
            if report['mode']!='recovery' or row['handoff_revision'] is not None:
                raise Conflict('operator decision requires recovery before handoff')
            if self._campaign(db,campaign)['state']!='RUNNING': raise Conflict('campaign paused')
            db.execute('INSERT INTO attempt_approval_commands(request,attempt,document,created) VALUES(?,?,?,?)',
                       (request_id,attempt_id,raw,self.clock()))
            return document

    def _approval_status(self, db, row):
        from .credential_registry import require_execution_credentials
        try:
            require_execution_credentials(db, row['device'], self.clock(),expected_generation=row['credential_generation'])
        except Conflict:
            return {'state':'blocked','reason':'credentials_not_live'}
        context,campaign=self._approval_context(db,row)
        if context is None: return {'state':'not_required'}
        if self._campaign(db,campaign)['state']!='RUNNING': return {'state':'blocked','reason':'campaign_paused'}
        report=json.loads(db.execute('SELECT report FROM devices WHERE id=?',(row['device'],)).fetchone()[0])
        if any(report.get('inventory',{}).get(key)!=context[key] for key in ('media_instance_id','target_binding')):
            return {'state':'waiting','reason':'claimed_identity_changed','binding':context}
        current=db.execute('SELECT document FROM attempt_approval_commands WHERE attempt=? ORDER BY seq DESC LIMIT 1',(row['id'],)).fetchone()
        if not current: return {'state':'waiting','binding':context}
        decision=json.loads(current['document'])
        if decision['binding']!=context: return {'state':'waiting','reason':'approval_binding_changed','binding':context}
        return {'state':decision['decision'],'binding':context}

    def attempt_approval(self, attempt_id, token, boot_id):
        self._expire()
        with self.transaction() as db:
            row=self._live(db,attempt_id,token,boot_id)
            return self._approval_status(db,row)

    def _require_operator_approval(self, db, row):
        status=self._approval_status(db,row)
        if status['state'] not in {'approved','not_required'}:
            raise Conflict('exact operator approval required before handoff')
