"""Advisory authenticated protocol observations, never execution authorization."""
import hmac

from .contracts import identifier,digest
from .credential_registry import _document
from .enrollment_records import _now

MIGRATION='''
CREATE TABLE protocol_contacts(
 device_id TEXT PRIMARY KEY REFERENCES devices(id),boot_id TEXT NOT NULL,
 credential_generation TEXT NOT NULL REFERENCES credential_generations(generation),
 received_at INTEGER NOT NULL);
'''


def observe_contact(controller,device_id,token,boot_id):
    """Call only after a successful registry HTTPS register/claim/reconcile.

    Recheck current scoped credentials and boot in the recording transaction.
    The receipt records a request, without asserting continuous connectivity.
    """
    identifier(device_id);identifier(boot_id)
    if not isinstance(token,str) or not 32<=len(token)<=512 or not token.isascii():return False
    now=_now(controller.clock)
    with controller.transaction() as db:
        row=db.execute('SELECT * FROM credential_generations WHERE device_id=? AND revoked=0',(device_id,)).fetchone()
        device=db.execute('SELECT boot FROM devices WHERE id=?',(device_id,)).fetchone()
        if row is None or device is None or device['boot']!=boot_id:return False
        _document(row)
        if now>=row['expires_at'] or not hmac.compare_digest(row['device_token_sha256'],digest(token.encode())):return False
        previous=db.execute('SELECT received_at FROM protocol_contacts WHERE device_id=?',(device_id,)).fetchone()
        if (now<db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]
                or previous is not None and now<previous['received_at']):return False
        db.execute('INSERT INTO protocol_contacts VALUES(?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET boot_id=excluded.boot_id,credential_generation=excluded.credential_generation,received_at=excluded.received_at',
                   (device_id,boot_id,row['generation'],now))
    return True
