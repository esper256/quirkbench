"""Private expiring invitations are separate from pairing and boot authority."""
from pathlib import Path
import json
import math
import stat

import pytest
from jsonschema import Draft202012Validator

from quirkbench import enrollment
from quirkbench.contracts import Conflict, ContractError, canonical
from quirkbench.controller import Controller
from quirkbench.controller_tls import inspect_identity
from quirkbench.enrollment import create_code, code_status, revoke_code, validate_code
from test_setup_service import initialized, Services, start
from tls_command_fixture import TLSCommands


def historical_code(*args, **kwargs):
    # Preserve explicit v1 expiry regressions alongside new v2 lifetime tests.
    kwargs.setdefault('ttl_seconds', 300)
    return create_code(*args, **kwargs)


@pytest.fixture
def issuer(tmp_path,initialized):
    services=Services();start(tmp_path,services)
    controller=Controller(tmp_path/'state',reserve_bytes=0)
    return controller, {'ready':services.ready,
        'tls_inspector':lambda directory,**kwargs:inspect_identity(directory,run=TLSCommands(),**kwargs),
        'clock':lambda:1000}


def test_code_schema_strict_metadata_has_no_secret_or_enrollment_authority():
    root=Path(__file__).resolve().parents[1]
    value=json.loads((root/'examples/enrollment-code.json').read_text())
    schema=json.loads((root/'schemas/enrollment-code.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(validate_code(value))
    for patch in ({'schema_version':True},{'code':'secret'},{'expires_at':True},{'controller_url':'http://host:443'},
                  {'controller_url':'https://user:pass@host:443'},{'expires_at':value['created_at']+901}):
        with pytest.raises(ContractError):validate_code({**value,**patch})


def test_private_code_replay_does_not_create_targets_or_store_secrets_in_public_objects(issuer,tmp_path):
    c,kwargs=issuer
    first=historical_code(c,'workstation','pair-1',**kwargs)
    assert historical_code(c,'workstation','pair-1',**kwargs)==first
    assert len(first['code'])==43 and not first['enrolled'] and not first['boot_authorized']
    public=code_status(c.root,first['record']['code_id'],clock=kwargs['clock'])
    assert public['redeemable'] and 'code' not in public and first['code'] not in json.dumps(public)
    for file in c.root.rglob('issuance.json'):
        assert stat.S_IMODE(file.stat().st_mode)==0o600 and file.is_relative_to(c.root/'private')
        assert stat.S_IMODE(file.parent.stat().st_mode)==0o700
    with c.transaction() as db:
        rows=db.execute('SELECT * FROM enrollment_codes').fetchall()
        assert len(rows)==1 and first['code'] not in json.dumps(dict(rows[0]))
        for table in ('devices','campaigns','jobs','attempts','credential_generations','operations'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    assert not list(c.store.objects.iterdir())


@pytest.mark.parametrize('boundary',['secret_retained','code_committed'])
def test_code_issuance_ack_loss_reuses_same_secret_and_expiry(issuer,boundary):
    c,kwargs=issuer
    def interrupt(actual):
        if actual==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):historical_code(c,'target','pair',fault_hook=interrupt,**kwargs)
    path=next((c.root/'private/enrollment/codes').rglob('issuance.json'))
    saved=json.loads(path.read_bytes())
    replay=historical_code(c,'target','pair',**kwargs)
    assert replay['code']==saved['code'] and replay['record']==saved['record']


@pytest.mark.parametrize('change',['name','ttl','certificate','secret','fields','symlink'])
def test_changed_request_trust_or_private_material_never_replaces_code(issuer,tmp_path,change):
    c,kwargs=issuer
    historical_code(c,'target','pair',**kwargs)
    args=dict(kwargs);name='target';ttl=300
    if change=='name':name='other'
    elif change=='ttl':ttl=60
    elif change=='certificate':
        original=kwargs['tls_inspector']
        args['tls_inspector']=lambda *a,**k:{**original(*a,**k),'certificate_sha256':'b'*64}
    else:
        path=next((c.root/'private/enrollment/codes').rglob('issuance.json'))
        raw=path.read_bytes();value=json.loads(raw)
        if change=='secret':value['code']='b'*43
        elif change=='fields':value['extra']=1
        elif change=='symlink':
            other=tmp_path/'other.json';other.write_bytes(raw);path.unlink();path.symlink_to(other)
        if change!='symlink':path.write_bytes(canonical(value))
    with pytest.raises((ContractError,Conflict)):
        historical_code(c,name,'pair',**{**args, 'ttl_seconds':ttl})


@pytest.mark.parametrize('clock',[lambda:999,lambda:1300,lambda:True,lambda:math.nan,lambda:math.inf,lambda:0])
def test_expired_backwards_or_unknown_clock_blocks_replay(issuer,clock):
    c,kwargs=issuer;result=historical_code(c,'target','pair',**kwargs)
    with pytest.raises((ContractError,Conflict)):
        historical_code(c,'target','pair',**{**kwargs,'clock':clock})
    if clock()==1300:
        assert not code_status(c.root,result['record']['code_id'],clock=clock)['redeemable']
    elif clock()==999:
        with pytest.raises(Conflict,match='clock moved backwards'):
            code_status(c.root,result['record']['code_id'],clock=clock)


def test_revocation_and_redemption_never_reissue_operator_code(issuer):
    c,kwargs=issuer;result=historical_code(c,'target','pair',**kwargs)
    revoke_code(c,result['record']['code_id']);revoke_code(c,result['record']['code_id'])
    assert code_status(c.root,result['record']['code_id'],clock=kwargs['clock'])['state']=='REVOKED'
    with pytest.raises(Conflict,match='redeemed or revoked'):historical_code(c,'target','pair',**kwargs)
    other=historical_code(c,'other','pair-other',**kwargs)
    with c.transaction() as db:db.execute("UPDATE enrollment_codes SET state='REDEEMED' WHERE id=?",(other['record']['code_id'],))
    with pytest.raises(Conflict,match='credential generation'):revoke_code(c,other['record']['code_id'])
    with pytest.raises(Conflict,match='redeemed or revoked'):historical_code(c,'other','pair-other',**kwargs)


def test_durable_issuance_rate_limit_and_replay_do_not_extend_lifetime(issuer):
    c,kwargs=issuer
    records=[historical_code(c,'target','pair-'+str(n),**kwargs) for n in range(10)]
    assert historical_code(c,'target','pair-0',**kwargs)==records[0]
    with pytest.raises(Conflict,match='rate limit'):historical_code(c,'target','pair-10',**kwargs)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM enrollment_codes').fetchone()[0]==10


def test_controller_clock_watermark_fences_new_requests_and_terminal_expiry(issuer):
    c,kwargs=issuer;first=historical_code(c,'target','pair',**kwargs)
    with pytest.raises(Conflict,match='clock moved backwards'):
        historical_code(c,'other','other',**{**kwargs,'clock':lambda:999})
    with pytest.raises(Conflict,match='expired'):
        historical_code(c,'target','pair',**{**kwargs,'clock':lambda:1300})
    with c.transaction() as db:
        assert db.execute('SELECT last_seen FROM enrollment_clock').fetchone()[0]==1300
        assert db.execute('SELECT state FROM enrollment_codes').fetchone()[0]=='EXPIRED'
    for call in (lambda:historical_code(c,'other','other',**{**kwargs,'clock':lambda:1001}),
                 lambda:code_status(c.root,first['record']['code_id'],clock=lambda:1001)):
        with pytest.raises(Conflict,match='clock moved backwards'):call()
    assert not code_status(c.root,first['record']['code_id'],clock=lambda:1300)['redeemable']


def test_delayed_uncommitted_codes_count_their_actual_first_activation_time(issuer,monkeypatch):
    c,kwargs=issuer
    # Use a captured, already verified trust snapshot to isolate durable rate logic.
    snapshot=enrollment._snapshot(c.root,tls_inspector=kwargs['tls_inspector'])
    monkeypatch.setattr(enrollment,'_snapshot',lambda *args,**kw:snapshot)
    for n in range(10):historical_code(c,'target','active-'+str(n),**{**kwargs,'ttl_seconds':900})
    for n in range(11):
        with pytest.raises(Conflict,match='rate limit'):
            historical_code(c,'target','delayed-'+str(n),ttl_seconds=900,**kwargs)
    delayed={**kwargs,'clock':lambda:1301}
    for n in range(10):
        result=historical_code(c,'target','delayed-'+str(n),ttl_seconds=900,**delayed)
        assert result['record']['created_at']==1000 and result['record']['expires_at']==1900
    with pytest.raises(Conflict,match='rate limit'):
        historical_code(c,'target','delayed-10',ttl_seconds=900,**delayed)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM enrollment_codes WHERE issued_at=1301').fetchone()[0]==10


def test_uncommitted_private_issuance_cannot_change_immutable_ttl(issuer):
    c,kwargs=issuer
    def crash(boundary):
        if boundary=='secret_retained':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):historical_code(c,'target','pair',fault_hook=crash,**kwargs)
    path=next((c.root/'private/enrollment/codes').rglob('issuance.json'))
    saved=json.loads(path.read_bytes());saved['record']['expires_at']+=60;path.write_bytes(canonical(saved))
    with pytest.raises(Conflict,match='issuance differs'):
        historical_code(c,'target','pair',**kwargs)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM enrollment_codes').fetchone()[0]==0


def test_read_only_status_rejects_changed_public_row_metadata(issuer):
    c,kwargs=issuer;result=historical_code(c,'target','pair',**kwargs)
    with c.transaction() as db:db.execute('UPDATE enrollment_codes SET expires_at=expires_at+60')
    with pytest.raises(Conflict,match='metadata differs'):
        code_status(c.root,result['record']['code_id'],clock=kwargs['clock'])


def test_missing_ready_owner_and_static_authentication_block_guided_issuance(issuer):
    c,kwargs=issuer
    def unavailable(root):raise ContractError('service unavailable')
    with pytest.raises(ContractError,match='service unavailable'):
        historical_code(c,'target','pair',**{**kwargs,'ready':unavailable})
    from quirkbench.controller_service import configuration
    config=json.loads((c.root/'private/controller-service.json').read_bytes())
    config.pop('credential_registry');config['tokens_file']=configuration(c.root)['cert']
    atomic=c.root/'private/controller-service.json';atomic.write_bytes(canonical(config))
    with pytest.raises(ContractError,match='registry authentication'):
        historical_code(c,'target','pair',**{**kwargs,'ready':lambda _:None})
