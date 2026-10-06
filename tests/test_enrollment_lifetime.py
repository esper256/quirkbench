"""Non-expiring invitations still use the original request/key redemption."""
import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from jsonschema import Draft202012Validator

from quirkbench import enrollment, enrollment_proof as proof
from quirkbench.contracts import Conflict, ContractError
from quirkbench.controller import MIGRATIONS
from test_enrollment import issuer
from test_setup_service import initialized
from test_enrollment_proof import request, signed_nonce, args


def test_new_code_schema_and_arbitrarily_delayed_redemption(issuer):
    c, kwargs = issuer
    code = enrollment.create_code(c, 'other-hardware', 'prepared-usb', **kwargs)
    assert code['record']['schema_version'] == 2
    assert code['record']['expires_at'] is None
    schema = json.loads((Path(__file__).resolve().parents[1]/'schemas/enrollment-code.v2.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(code['record'])
    late = {**kwargs, 'clock':lambda:2_000_000_000}
    assert enrollment.code_status(c.root, code['record']['code_id'], clock=late['clock'])['redeemable']
    assert enrollment.create_code(c, 'other-hardware', 'prepared-usb', **late) == code
    key = ed25519.Ed25519PrivateKey.generate()
    req = request(code, key)
    nonce, signature = signed_nonce(c, req, key, late)
    result = proof.reserve_redemption(c, req, nonce['challenge_id'], code['code'], signature, **args(late))
    assert result['state'] == 'BOUND' and not result['boot_authorized']
    # A fresh challenge recovers the same bound request after a lost reply.
    nonce, signature = signed_nonce(c, req, key, late)
    assert proof.reserve_redemption(c, req, nonce['challenge_id'], code['code'], signature, **args(late)) == result
    with pytest.raises(Conflict, match='another request/key'):
        proof.challenge(c, request(code, ed25519.Ed25519PrivateKey.generate()), '192.0.2.40', **args(late))
    with c.transaction() as db:
        for table in ('devices', 'campaigns', 'jobs', 'attempts'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 0


def test_revocation_and_short_lived_challenge_remain_effective(issuer):
    c, kwargs = issuer
    code = enrollment.create_code(c, 'target', 'usb', **kwargs)
    key = ed25519.Ed25519PrivateKey.generate()
    req = request(code, key)
    nonce, signature = signed_nonce(c, req, key, kwargs)
    late = {**kwargs, 'clock':lambda:1061}
    with pytest.raises((ContractError, Conflict), match='challenge'):
        proof.reserve_redemption(c, req, nonce['challenge_id'], code['code'], signature, **args(late))
    assert enrollment.code_status(c.root, code['record']['code_id'], clock=late['clock'])['redeemable']
    enrollment.revoke_code(c, code['record']['code_id'])
    with pytest.raises(Conflict, match='revoked'):
        proof.challenge(c, req, '192.0.2.40', **args(late))


@pytest.mark.parametrize('boundary', ['secret_retained', 'code_committed'])
def test_v2_issuance_interruption_reuses_secret_after_long_delay(issuer, boundary):
    c, kwargs = issuer
    def interrupt(stage):
        if stage == boundary:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        enrollment.create_code(c, 'target', 'usb', fault_hook=interrupt, **kwargs)
    saved = json.loads(next((c.root/'private/enrollment/codes').rglob('issuance.json')).read_bytes())
    answer = enrollment.create_code(c, 'target', 'usb', **{**kwargs, 'clock':lambda:2_000_000_000})
    assert answer['record'] == saved['record'] and answer['code'] == saved['code']
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM enrollment_codes').fetchone()[0] == 1


def test_v2_cannot_be_reinterpreted_with_deadline_or_changed_sql_metadata(issuer):
    c, kwargs = issuer
    code = enrollment.create_code(c, 'target', 'usb', **kwargs)
    with pytest.raises(ContractError, match='null expiry'):
        enrollment.validate_code({**code['record'], 'expires_at':1300})
    with c.transaction() as db:
        db.execute('UPDATE enrollment_codes SET expires_at=1300')
    with pytest.raises(Conflict, match='metadata differs'):
        enrollment.code_status(c.root, code['record']['code_id'], clock=kwargs['clock'])


def test_forward_migration_preserves_old_rows_foreign_keys_and_deadlines():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    for migration in MIGRATIONS[:-1]:
        db.executescript(migration)
    db.execute("INSERT INTO enrollment_codes(id,request_id,request_digest,code_sha256,document,created_at,expires_at,issued_at,state) VALUES('c','r','d','s','{}',1000,1300,1000,'REDEEMED')")
    db.execute("INSERT INTO enrollment_requests(request_id,request_digest,document,code_id,key_sha256,state) VALUES('r','d','{}','c','k','BOUND')")
    for state in ('ACTIVE', 'REVOKED', 'EXPIRED'):
        db.execute("INSERT INTO enrollment_codes(id,request_id,request_digest,code_sha256,document,created_at,expires_at,issued_at,state) VALUES(?,?,?,?,?,1000,1300,1000,?)",
                   (state, state, 'd', state, '{}', state))
    before = dict(zip([v[0] for v in db.execute('SELECT * FROM enrollment_codes').description], db.execute('SELECT * FROM enrollment_codes').fetchone()))
    db.executescript(enrollment.NONEXPIRING_MIGRATION)
    after = dict(zip([v[0] for v in db.execute('SELECT * FROM enrollment_codes').description], db.execute('SELECT * FROM enrollment_codes').fetchone()))
    assert before == after
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT code_id FROM enrollment_requests').fetchone() == ('c',)
    assert dict(db.execute("SELECT id,state FROM enrollment_codes WHERE id!='c'").fetchall()) == {
        state:state for state in ('ACTIVE', 'REVOKED', 'EXPIRED')}
    db.execute("INSERT INTO enrollment_codes(id,request_id,request_digest,code_sha256,document,created_at,expires_at,issued_at,state) VALUES('v2','v2','d','v2','{}',1000,NULL,1000,'ACTIVE')")
    assert db.execute("SELECT expires_at FROM enrollment_codes WHERE id='v2'").fetchone() == (None,)
