"""Local terminal revocation, generation fences and retained evidence attribution."""
from dataclasses import replace
import json
from pathlib import Path
import ssl
import time

import pytest

from quirkbench import cli
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,canonical,digest
from quirkbench.credential_registry import CredentialRegistry,record_generation,revoke_generation
from quirkbench.enrollment import create_code
from quirkbench.target_lifecycle import revoke_target,validate_receipt
from test_enrollment import issuer
from test_enrollment_credentials import publication,complete
from test_enrollment_certificate import bound
from test_setup_service import initialized
from test_operator_approval import lab


def register_generation(c,report,generation='generation-1'):
    document={'schema_version':1,'generation':generation,'device_id':report.device_id,
        'media_instance_id':report.inventory['media_instance_id'],
        'system_uuid':report.inventory['target_binding']['system_uuid'],
        'device_token_sha256':digest((generation+'-token').encode()),
        'repository_certificate_sha256':digest((generation+'-certificate').encode()),'expires_at':int(time.time())+3600}
    record_generation(c,document)
    return document


@pytest.mark.parametrize('boundary',['before_revocation_commit','revocation_committed'])
def test_atomic_revocation_and_lost_reply_exact_receipt(publication,boundary):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    device=result['device_id'];generation=result['credential_generation']['generation']
    c.register(CapabilityReport(device,'recovery',[],inventory={'target_binding':req['target_binding'],'media_instance_id':req['media_instance_id']}))
    c.create_campaign('campaign',device);c.resume('campaign')
    before=list(c.store.objects.iterdir())
    def fault(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):revoke_target(c.root,'target','revoke-1',generation=generation,fault_hook=fault)
    registry=CredentialRegistry(c.root)
    assert registry.authenticate_device(device,result['device_token'])==(boundary=='before_revocation_commit')
    receipt=revoke_target(c.root,'target','revoke-1',generation=generation)
    assert revoke_target(c.root,'target','revoke-1',generation=generation)==receipt
    assert receipt['device_id']==device and receipt['generation']==generation and receipt['paused_campaigns_at_revoke']==['campaign']
    assert not registry.authenticate_device(device,result['device_token'])
    assert not registry.authenticate_repository(ssl.PEM_cert_to_DER_cert(result['repository_certificate_pem']))
    with pytest.raises(Conflict,match='revoked'):complete(c,req,kwargs)
    with pytest.raises(Conflict,match='revoked'):c.resume('campaign')
    with pytest.raises(Conflict,match='revoked'):c.claim(device,'recovery','claim-1')
    assert c.status('campaign')['state']=='PAUSED'
    assert list(c.store.objects.iterdir())==before
    assert result['device_token'] not in json.dumps(receipt)
    assert not any(receipt[k] for k in ('physical_shutdown_verified','evidence_drain_authorized','boot_authorized'))


def test_exact_replay_never_revokes_a_later_generation_or_changed_target(tmp_path):
    c,_,_,_,_,report=lab(tmp_path);register_generation(c,report)
    receipt=revoke_target(c.root,'target')
    second=register_generation(c,report,'generation-2')
    assert revoke_target(c.root,'target')==receipt
    with c.transaction() as db:
        assert db.execute('SELECT revoked FROM credential_generations WHERE generation=?',(second['generation'],)).fetchone()[0]==0
    with pytest.raises(Conflict,match='exact intent'):revoke_target(c.root,'different-target',receipt['request_id'])
    with pytest.raises(Conflict,match='generation changed'):revoke_target(c.root,'target','second-revoke',generation='generation-1')
    assert revoke_target(c.root,'target','second-revoke',generation='generation-2')['generation']=='generation-2'


@pytest.mark.parametrize('handoff',[False,True])
def test_revocation_reports_unresolved_work_and_never_revives_issued_authority(tmp_path,handoff):
    c,_,_,_,_,report=lab(tmp_path);register_generation(c,report)
    # A worker may already own staging when credential maintenance pauses the
    # physical campaign; construct that independent device-bound claim directly.
    with c.lifecycle() as owner:
        c.resume('campaign')
        claim=c.claim('target',report.boot_id,'physical-claim');attempt=claim['attempt_id']
        c.decide_attempt(attempt,'approved',request_id='approve-1')
        if handoff:c.handoff(attempt,claim['token'],report.boot_id,'a'*64)
        original=c.status('campaign')['attempts'][0]
        operation=c.admit_operation('old-build','build',{},device_id='target')
        worker=owner.claim(operation['id'],stage='job_inputs',deadline=c.clock()+60)
        receipt=revoke_target(c.root,'target','revoke')
        assert receipt['workers_pending_at_revoke']==[worker['id']]
        assert c.status('campaign')['state']=='PAUSE_REQUESTED'
        assert c.status('campaign')['attempts'][0]['state']==original['state']
    assert receipt['unresolved_attempts_at_revoke']==[attempt]
    assert c.status('campaign')['state']=='PAUSED'
    assert c.status('campaign')['attempts'][0]['state']=='UNCERTAIN'
    assert c.operator_attempt_status(attempt)['state']=='blocked'
    for action in (lambda:c.claim('target',report.boot_id,'physical-claim'),
                   lambda:c.start(attempt,claim['token'],report.boot_id),
                   lambda:c.decide_attempt(attempt,'approved',request_id='approve-1'),
                   lambda:c.handoff(attempt,claim['token'],report.boot_id,'a'*64)):
        with pytest.raises(Conflict):action()
    # Same device/media with a new live generation still cannot revive this token.
    register_generation(c,report,'generation-2')
    for action in (lambda:c.claim('target',report.boot_id,'physical-claim'),
                   lambda:c.start(attempt,claim['token'],report.boot_id),
                   lambda:c.handoff(attempt,claim['token'],report.boot_id,'a'*64),
                   lambda:c.decide_attempt(attempt,'approved',request_id='approve-1')):
        with pytest.raises(Conflict):action()
    assert c.operator_attempt_status(attempt)['state']=='blocked'
    if handoff:
        c.register(replace(report,boot_id='candidate',mode='experiment'))
        with pytest.raises(Conflict,match='another credential generation'):
            c.candidate_started(attempt,claim['token'],'candidate','a'*64)
    assert c.status('campaign')['attempts'][0]['handoff_revision']==original['handoff_revision']
    # Local terminal evidence can be retained with original attribution; this is
    # not a new remote drain credential or permission to register moved hardware.
    raw=b'old evidence';reply=c.upload(attempt,claim['token'],report.boot_id,'evidence',0,raw,digest(raw),len(raw))
    assert reply['complete'] and c.store.get(digest(raw))==raw


def test_first_candidate_adoption_is_blocked_by_revocation(tmp_path):
    c,_,_,_,_,report=lab(tmp_path);register_generation(c,report)
    claim=c.claim('target',report.boot_id,'claim');attempt=claim['attempt_id']
    c.decide_attempt(attempt,'approved',request_id='approve')
    c.handoff(attempt,claim['token'],report.boot_id,'a'*64)
    revoke_target(c.root,'target','revoke')
    c.register(replace(report,boot_id='candidate',mode='experiment'))
    with pytest.raises(Conflict,match='revoked'):c.candidate_started(attempt,claim['token'],'candidate','a'*64)
    assert c.status('campaign')['attempts'][0]['state']=='BOOT_PENDING'


def test_later_live_generation_cannot_replay_a_still_pending_physical_handoff(tmp_path):
    c,_,_,_,_,report=lab(tmp_path);first=register_generation(c,report)
    claim=c.claim('target',report.boot_id,'claim');attempt=claim['attempt_id']
    c.decide_attempt(attempt,'approved',request_id='approve')
    c.handoff(attempt,claim['token'],report.boot_id,'a'*64)
    revoke_generation(c,first['generation']);register_generation(c,report,'generation-2')
    assert c.operator_attempt_status(attempt)['reason']=='credentials_not_live'
    with pytest.raises(Conflict):c.handoff(attempt,claim['token'],report.boot_id,'a'*64)
    with pytest.raises(Conflict,match='another credential generation'):c.claim('target',report.boot_id,'claim')
    c.register(replace(report,boot_id='candidate',mode='experiment'))
    with pytest.raises(Conflict,match='another credential generation'):c.candidate_started(attempt,claim['token'],'candidate','a'*64)


@pytest.mark.parametrize('change',['media','uuid','bool-binding-version','legacy-admission','expired'])
def test_execution_requires_current_generation_and_exact_report_binding(tmp_path,change):
    c,_,_,_,_,report=lab(tmp_path)
    old=c.claim('target',report.boot_id,'old') if change=='legacy-admission' else None
    document=register_generation(c,report)
    if change in ('media','uuid','bool-binding-version'):
        inventory=report.inventory.copy()
        if change=='media':inventory['media_instance_id']='different'
        elif change=='uuid':inventory['target_binding']={'schema_version':1,'system_uuid':'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'}
        else:inventory['target_binding']={**inventory['target_binding'],'schema_version':True}
        c.register(replace(report,inventory=inventory))
    elif change=='expired':c.clock=lambda:document['expires_at']
    if old:
        with pytest.raises(Conflict,match='another credential generation'):c.start(old['attempt_id'],old['token'],report.boot_id)
    else:
        with pytest.raises(Conflict):c.claim('target',report.boot_id,'claim')
        with pytest.raises(Conflict):c.resume('campaign')


@pytest.mark.parametrize('stage',['active','bound','complete'])
def test_invitation_revocation_is_exact_and_blocks_pending_completion(request,stage):
    if stage=='active':
        c,kwargs=request.getfixturevalue('issuer');code=create_code(c,'target','invitation',**kwargs);code_id=code['record']['code_id'];req=None
    elif stage=='bound':c,req,code,kwargs=request.getfixturevalue('publication');code_id=req['code_id']
    else:c,req,code,kwargs=request.getfixturevalue('publication');complete(c,req,kwargs);code_id=req['code_id']
    if stage=='complete':
        with pytest.raises(Conflict,match='completed'):revoke_target(c.root,code_id,'revoke-code',action='revoke-code')
        assert c.db_path.exists()
        return
    receipt=revoke_target(c.root,code_id,'revoke-code',action='revoke-code')
    assert revoke_target(c.root,code_id,'revoke-code',action='revoke-code')==receipt
    with c.transaction() as db:
        assert db.execute('SELECT state FROM enrollment_codes WHERE id=?',(code_id,)).fetchone()[0]=='REVOKED'
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==0
        if req:assert db.execute('SELECT state FROM enrollment_requests WHERE request_id=?',(req['request_id'],)).fetchone()[0]=='REVOKED'
    if req:
        with pytest.raises(Conflict):complete(c,req,kwargs)


def test_command_failures_do_not_initialize_state_and_cli_options_remain_separate(tmp_path,capsys):
    from quirkbench.setup_contracts import SetupUnavailable
    with pytest.raises(SetupUnavailable):revoke_target(tmp_path/'absent','target')
    assert not (tmp_path/'absent').exists()
    assert cli.main(['--state',str(tmp_path/'absent'),'target','revoke','target','--json'])==2
    assert json.loads(capsys.readouterr().out)['error']['code']=='INVALID_INPUT'
    assert not (tmp_path/'absent').exists()
    for argv in (['target','revoke','target','--ttl-seconds','60'],['target','show','target','--generation','gen'],
                 ['target','revoke-code','code','--generation','gen'],['target','revoke','target','--status-version','2']):
        with pytest.raises(SystemExit):cli.parser().parse_args(argv)


def test_strict_receipt_schema_and_additive_cli_fixture():
    from jsonschema import Draft202012Validator,ValidationError
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/target-revocation.v1.schema.json').read_bytes())
    value=json.loads((root/'examples/target-revocation.json').read_bytes())
    Draft202012Validator.check_schema(schema);validator=Draft202012Validator(schema);validator.validate(value)
    assert validate_receipt(value)==value
    for key in ('physical_shutdown_verified','evidence_drain_authorized','boot_authorized'):
        with pytest.raises(ContractError):validate_receipt({**value,key:True})
        with pytest.raises(ValidationError):validator.validate({**value,key:True})
    for case in json.loads((root/'examples/target-cli.v4.json').read_bytes())['cases']:
        args=vars(cli.parser().parse_args(case['argv']))
        assert {key:args[key] for key in case['expected']}==case['expected']


def test_revocation_after_clock_rollback_is_available_and_receipt_time_retains_high_water(publication):
    c,req,code,kwargs=publication;complete(c,req,kwargs)
    with c.transaction() as db:high_water=db.execute('SELECT last_seen FROM enrollment_clock WHERE id=1').fetchone()[0]
    receipt=revoke_target(c.root,'target','rollback',clock=lambda:1000)
    assert receipt['created_at']==high_water and receipt['revoked']


@pytest.mark.parametrize('corruption',['missing','corrupt'])
def test_revocation_is_sql_only_when_inventory_cas_is_unavailable(tmp_path,monkeypatch,corruption):
    from test_hardware_plan import inventory
    from quirkbench.store import ArtifactStore
    from quirkbench.state_reader import ReadOnlyStore,StateReader
    c,_,_,_,_,report=lab(tmp_path);generation=register_generation(c,report)
    hardware=inventory(tmp_path)
    received=c.register(replace(report,inventory={**report.inventory,
        'architecture':hardware['platform']['architecture'],'hardware_inventory':hardware}))
    path=c.store.path(received['hardware_inventory_digest'])
    if corruption=='missing':path.unlink()
    else:path.write_bytes(b'corrupt retained inventory')
    # No readiness reader, native adapter or hashing may obstruct revocation.
    for cls in (ArtifactStore,ReadOnlyStore):
        for method in ('get','verify'):
            monkeypatch.setattr(cls,method,lambda *a:pytest.fail('revocation must not read/hash readiness artifacts'))
    monkeypatch.setattr(StateReader,'target_inventory',lambda *a:pytest.fail('revocation must not plan candidate readiness'))
    receipt=revoke_target(c.root,report.device_id,'revoke',generation=generation['generation'])
    assert receipt['generation']==generation['generation'] and receipt['revoked']


def test_bound_device_worker_cannot_start_after_revocation(tmp_path):
    c,_,_,_,_,report=lab(tmp_path);register_generation(c,report)
    with c.lifecycle() as owner:
        operation=c.admit_operation('queued-build','build',{},device_id='target')
        revoke_target(c.root,'target','revoke')
        with pytest.raises(Conflict,match='revoked'):owner.claim(operation['id'],stage='job_inputs',deadline=c.clock()+60)
        assert c.operation_status(operation['id'])['data']['state']=='QUEUED'


def test_large_history_revocation_is_complete_bounded_and_replayable_after_lost_ack(tmp_path):
    c,_,_,_,_,report=lab(tmp_path);register_generation(c,report)
    with c.transaction() as db:
        db.executemany("INSERT INTO campaigns(id,device,state,reason) VALUES(?,'target','PAUSED','historical diagnosis')",
                       [('history-'+str(i),) for i in range(1100)])
        db.executemany("INSERT INTO campaigns(id,device,state) VALUES(?,'target','RUNNING')",
                       [('campaign-'+str(i).zfill(32),) for i in range(1100)])
    def lost(stage):
        if stage=='revocation_committed':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):revoke_target(c.root,'target','large',fault_hook=lost)
    receipt=revoke_target(c.root,'target','large')
    assert len(canonical(receipt))>16384 and receipt['work_lists_truncated']
    assert receipt['paused_campaign_count_at_revoke']==1101 and len(receipt['paused_campaigns_at_revoke'])==1000
    assert revoke_target(c.root,'target','large')==receipt
    with c.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM campaigns WHERE state!='PAUSED'").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM campaigns WHERE reason='historical diagnosis'").fetchone()[0]==1100
