"""Scoped controller retarget enrollment, separate from local media activation."""
import json
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from jsonschema import Draft202012Validator

from quirkbench import cli,enrollment_proof as proof,retarget_invitation as retarget
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,canonical,digest
from quirkbench.credential_registry import CredentialRegistry,record_generation,revoke_generation
from quirkbench.enrollment import create_code,revoke_code
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment import issuer
from test_setup_service import initialized
from test_enrollment_proof import request,signed_nonce,args

NEW_UUID='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


@pytest.fixture
def original(publication):
    c,req,code,kwargs=publication;old=complete(c,req,kwargs)
    revoke_generation(c,old['credential_generation']['generation'])
    return c,old,kwargs


def issue(fixture,**patch):
    c,old,kwargs=fixture
    values=dict(target=old['device_id'],generation=old['credential_generation']['generation'],new_name='new-target',
        new_uuid=NEW_UUID,request_id='retarget-1',**kwargs);values.update(patch)
    return retarget.create_invitation(c,**values)


def new_request(code,old,key):
    return request(code,key,request_id='new-request',media_instance_id=old['media_instance_id'],
        target_binding={'schema_version':1,'system_uuid':NEW_UUID})


def reserve(fixture,code):
    c,old,kwargs=fixture;key=ed25519.Ed25519PrivateKey.generate();req=new_request(code,old,key)
    nonce,signed=signed_nonce(c,req,key,kwargs)
    proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],signed,**args(kwargs))
    return req,key


def test_exact_scope_private_issuance_atomic_publication_and_initial_isolation(original):
    c,old,kwargs=original;code=issue(original);assert issue(original)==code
    record=retarget.validate_invitation(code['retarget_invitation'])
    assert record['scope']['old_generation']==old['credential_generation']['generation']
    assert record['scope']['media_instance_id']==old['media_instance_id']
    assert all(record[key] is False for key in ('boot_authorized','one_shot_cleared','old_evidence_drained'))
    assert code['code'] not in json.dumps(record)
    path=c.root/'private/enrollment/codes'/digest(b'retarget-1')/'issuance.json'
    retained=json.loads(path.read_bytes());assert retained['intent']['retarget_scope']==record['scope']
    assert path.stat().st_mode&0o777==0o600
    with c.transaction() as db:
        row=db.execute('SELECT * FROM enrollment_codes WHERE id=?',(code['record']['code_id'],)).fetchone()
        assert row['purpose']=='retarget'
        assert db.execute('SELECT COUNT(*) FROM retarget_invitation_scopes').fetchone()[0]==1
        assert code['code'] not in json.dumps(dict(row))
        for table in ('devices','campaigns','jobs','attempts','operations'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    with pytest.raises(Conflict):create_code(c,'new-target','retarget-1',**kwargs)


@pytest.mark.parametrize('phase',['secret_retained','code_committed'])
def test_interrupted_invitation_retains_code_scope_and_expiry(original,phase):
    c,old,kwargs=original
    def fail(actual):
        if phase==actual:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):issue(original,fault_hook=fail)
    saved=json.loads((c.root/'private/enrollment/codes'/digest(b'retarget-1')/'issuance.json').read_bytes())
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM retarget_invitation_scopes').fetchone()[0]==(phase=='code_committed')
        assert db.execute("SELECT COUNT(*) FROM enrollment_codes WHERE purpose='retarget'").fetchone()[0]==(phase=='code_committed')
    code=issue(original);assert code['code']==saved['code'] and code['record']==saved['record']


@pytest.mark.parametrize('change',['not-revoked','same-uuid','wrong-generation','active-media','active-uuid','active-old-device'])
def test_wrong_or_live_identity_cannot_issue(original,change):
    c,old,kwargs=original;patch={}
    if change=='not-revoked':
        with c.transaction() as db:db.execute('UPDATE credential_generations SET revoked=0')
    elif change=='same-uuid':patch['new_uuid']=old['target_binding']['system_uuid']
    elif change=='wrong-generation':patch['generation']='missing'
    else:
        another={**old['credential_generation'],'generation':'another','device_id':'other',
            'system_uuid':'cccccccc-cccc-cccc-cccc-cccccccccccc','media_instance_id':'other-media',
            'device_token_sha256':'c'*64,'repository_certificate_sha256':'d'*64}
        if change=='active-media':another['media_instance_id']=old['media_instance_id']
        elif change=='active-uuid':another['system_uuid']=NEW_UUID
        else:another['device_id']=old['device_id']
        record_generation(c,another)
    with pytest.raises((Conflict,ContractError)):issue(original,**patch)
    assert not (c.root/'private/enrollment/codes'/digest(b'retarget-1')).exists()


def work(c,old,kind):
    device=old['device_id']
    c.register(CapabilityReport(device,'original-boot',[],mode='recovery',inventory={}))
    c.create_campaign('old-campaign',device)
    with c.transaction() as db:
        if kind=='campaign':db.execute("UPDATE campaigns SET state='ACTIVE'")
        elif kind=='worker':db.execute("INSERT INTO operations(id,request_id,request_digest,input_digest,kind,state,created,updated,device,worker_unit) VALUES('worker','worker',?,?,'build','INTERRUPTED',1,1,?,'old.service')",('a'*64,'b'*64,device))
        else:
            db.execute("INSERT INTO experiments VALUES('experiment',?)",('{}',))
            db.execute("INSERT INTO jobs(id,campaign,experiment,repetition,state) VALUES(1,'old-campaign','experiment',0,'RESOLVED')")
            db.execute('INSERT INTO attempts(id,job,device,boot,generation,token,lease_until,deadline,state,handoff_revision) VALUES(?,1,?,?,1,?,1,1,?,?)',
                ('old-attempt',device,'original-boot','A'*32,'RESOLVED' if kind=='handoff' else kind,'a'*64 if kind=='handoff' else None))


@pytest.mark.parametrize('kind',['campaign','worker','UNCERTAIN','CLAIMED','RUNNING','BOOT_PENDING','unknown','handoff'])
def test_all_original_work_fences_issuance_and_later_challenge(original,kind):
    c,old,kwargs=original;code=issue(original);work(c,old,kind)
    with pytest.raises(Conflict):issue(original,request_id='second')
    key=ed25519.Ed25519PrivateKey.generate();req=new_request(code,old,key)
    with pytest.raises(Conflict):proof.challenge(c,req,'192.0.2.1',**args(kwargs))


@pytest.mark.parametrize('patch',[{'media_instance_id':'different'}, {'target_binding':{'schema_version':1,'system_uuid':'cccccccc-cccc-cccc-cccc-cccccccccccc'}}])
def test_request_cannot_expand_scope(original,patch):
    c,old,kwargs=original;code=issue(original);key=ed25519.Ed25519PrivateKey.generate();req={**new_request(code,old,key),**patch}
    with pytest.raises(Conflict,match='exact approved'):proof.challenge(c,req,'192.0.2.1',**args(kwargs))


@pytest.mark.parametrize('change',['missing','purpose','document','digest','original-digest'])
def test_scope_and_purpose_never_fall_back_to_initial(original,change):
    c,old,kwargs=original;code=issue(original);key=ed25519.Ed25519PrivateKey.generate();req=new_request(code,old,key)
    with c.transaction() as db:
        if change=='missing':db.execute('DELETE FROM retarget_invitation_scopes')
        elif change=='purpose':db.execute("UPDATE enrollment_codes SET purpose='initial' WHERE id=?",(code['record']['code_id'],))
        elif change=='digest':db.execute('UPDATE retarget_invitation_scopes SET document_sha256=?',('f'*64,))
        elif change=='document':db.execute("UPDATE retarget_invitation_scopes SET document='{}'")
        else:db.execute('UPDATE credential_generations SET expires_at=expires_at+1')
    with pytest.raises((Conflict,ContractError)):proof.challenge(c,req,'192.0.2.1',**args(kwargs))


def test_revoked_invitation_and_missing_private_secret_are_terminal(original):
    c,old,kwargs=original;code=issue(original);revoke_code(c,code['record']['code_id'])
    with pytest.raises(Conflict):issue(original)
    key=ed25519.Ed25519PrivateKey.generate()
    with pytest.raises(Conflict):proof.challenge(c,new_request(code,old,key),'192.0.2.1',**args(kwargs))
    path=c.root/'private/enrollment/codes'/digest(b'retarget-1')/'issuance.json';path.unlink()
    with pytest.raises(Conflict,match='secret is missing'):issue(original)
    assert not path.exists()


def test_redeem_rechecks_scope_after_native_proof(original,monkeypatch):
    c,old,kwargs=original;code=issue(original);key=ed25519.Ed25519PrivateKey.generate();req=new_request(code,old,key)
    nonce,signature=signed_nonce(c,req,key,kwargs);native=proof.verify_signature
    def changed(*a,**k):
        result=native(*a,**k);work(c,old,'worker');return result
    monkeypatch.setattr(proof,'verify_signature',changed)
    with pytest.raises(Conflict,match='worker'):proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],signature,**args(kwargs))
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM enrollment_requests WHERE request_id=?',(req['request_id'],)).fetchone()[0]==0
        assert db.execute('SELECT used FROM enrollment_challenges WHERE id=?',(nonce['challenge_id'],)).fetchone()[0]==1


def test_new_generation_and_complete_lost_ack_replay_preserve_old_attribution(original):
    c,old,kwargs=original;code=issue(original);req,key=reserve(original,code)
    result=complete(c,req,kwargs)
    assert result['device_id']!=old['device_id'] and result['target_binding']['system_uuid']==NEW_UUID
    assert result['media_instance_id']==old['media_instance_id']
    assert result['credential_generation']['generation']!=old['credential_generation']['generation']
    nonce,signed=signed_nonce(c,req,key,kwargs)
    reply=proof.reserve_redemption(c,req,nonce['challenge_id'],code['code'],signed,**args(kwargs))
    assert reply['state']=='COMPLETE' and complete(c,req,kwargs)==result
    registry=CredentialRegistry(c.root)
    assert not registry.authenticate_device(old['device_id'],old['device_token'])
    assert registry.authenticate_device(result['device_id'],result['device_token'])
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==2
        for table in ('attempts','campaigns','jobs','devices','operations','evidence'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0


@pytest.mark.parametrize('phase',['credential_intent_retained','credential_reply_retained','credential_generation_committed'])
def test_new_credential_interruption_never_inherits_old_authority(original,phase):
    c,old,kwargs=original;code=issue(original);req,key=reserve(original,code)
    def fail(actual):
        if actual==phase:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):complete(c,req,kwargs,fault_hook=fail)
    result=complete(c,req,kwargs);assert complete(c,req,kwargs)==result
    assert result['device_id']!=old['device_id']


def test_scope_changes_after_certificate_reply_block_final_publication(original):
    c,old,kwargs=original;code=issue(original);req,_=reserve(original,code)
    def fault(actual):
        if actual=='credential_reply_retained':work(c,old,'worker')
    with pytest.raises(Conflict,match='worker'):complete(c,req,kwargs,fault_hook=fault)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1
        assert db.execute('SELECT state FROM enrollment_requests WHERE request_id=?',(req['request_id'],)).fetchone()[0]=='BOUND'


def test_later_media_owner_blocks_complete_replay(original):
    c,old,kwargs=original;code=issue(original);req,key=reserve(original,code);result=complete(c,req,kwargs)
    revoke_generation(c,result['credential_generation']['generation'])
    record_generation(c,{**result['credential_generation'],'generation':'later-generation','device_id':'later-device',
        'device_token_sha256':'e'*64,'repository_certificate_sha256':'f'*64})
    with pytest.raises(Conflict,match='another active'):signed_nonce(c,req,key,kwargs)


def test_cli_scope_arguments_and_no_missing_state_initialization(tmp_path,monkeypatch,capsys):
    argv=['target','retarget-code','old','--generation','old-generation','--new-name','new','--new-uuid',NEW_UUID,'--request-id','request','--json']
    parsed=cli.parser().parse_args(argv);assert parsed.action=='retarget-code' and parsed.new_uuid==NEW_UUID
    for removed in ('--generation','--new-name','--new-uuid','--request-id'):
        index=argv.index(removed)
        with pytest.raises(SystemExit):cli.parser().parse_args(argv[:index]+argv[index+2:])
    for command in ('add','show','revoke','drain-revoke'):
        with pytest.raises(SystemExit):cli.parser().parse_args(['target',command,'target','--new-uuid',NEW_UUID])
    root=tmp_path/'missing';assert cli.main(['--state',str(root),*argv])==4
    assert not root.exists()


def test_missing_committed_scope_is_not_recreated(original):
    c,old,kwargs=original;issue(original)
    with c.transaction() as db:db.execute('DELETE FROM retarget_invitation_scopes')
    with pytest.raises(Conflict,match='committed retarget scope is missing'):issue(original)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM retarget_invitation_scopes').fetchone()[0]==0


def test_scope_schema_examples_and_frozen_cli_versions():
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/retarget-invitation.v1.schema.json').read_bytes())
    value=json.loads((root/'examples/retarget-invitation.json').read_bytes())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(retarget.validate_invitation(value))
    for patch in ({'schema_version':True},{'one_shot_cleared':True},{'extra':1}):
        with pytest.raises(ContractError):retarget.validate_invitation({**value,**patch})
    for version in range(2,7):
        cases=json.loads((root/f'examples/target-cli.v{version}.json').read_bytes())
        assert cases['schema_version']==version
        for case in cases['cases']:
            parsed=cli.parser().parse_args(case['argv'])
            for key,expected in case['expected'].items():
                actual=getattr(parsed,key)
                assert (str(actual) if isinstance(actual,Path) else actual)==expected


def test_authenticated_v2_scope_and_initial_activation_rejection(original,tmp_path):
    from quirkbench.enrollment_result import validate_result
    from quirkbench.enrollment_activation import activate_enrollment
    c,old,kwargs=original;code=issue(original);req,_=reserve(original,code);result=complete(c,req,kwargs)
    assert result['schema_version']==2 and result['retarget_invitation']==code['retarget_invitation']
    assert result['retarget_invitation']['code']['request_id']!=result['request_id']
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/enrollment-result.v2.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(validate_result(result,req))
    effects=[]
    with pytest.raises(Conflict,match='explicit stopped local retarget'):
        activate_enrollment(tmp_path/'absent',result,'',verify_target=lambda:effects.append('verify'))
    assert not effects and not (tmp_path/'absent').exists()
    for field in ('device_id','generation'):
        mutated=json.loads(canonical(result))
        if field=='device_id':
            mutated['device_id']=old['device_id'];mutated['credential_generation']['device_id']=old['device_id']
        else:mutated['credential_generation']['generation']=old['credential_generation']['generation']
        with pytest.raises(Conflict,match='inherits'):validate_result(mutated,req)


@pytest.mark.parametrize('change',['downgrade','scope'])
def test_complete_replay_requires_exact_retained_authenticated_v2_authority(original,change):
    c,old,kwargs=original;code=issue(original);req,_=reserve(original,code);result=complete(c,req,kwargs)
    raw=json.loads(canonical(result))
    if change=='downgrade':raw.pop('retarget_invitation');raw['schema_version']=1
    else:raw['retarget_invitation']['scope']['old_generation_sha256']='f'*64
    path=c.root/'private/enrollment/replies'/digest(req['request_id'].encode())/'result.json';path.write_bytes(canonical(raw))
    # Even coordinated retained-reply digest changes cannot replace controller scope.
    with c.transaction() as db:db.execute('UPDATE enrollment_requests SET result_sha256=? WHERE request_id=?',(digest(canonical(raw)),req['request_id']))
    with pytest.raises(Conflict,match='authenticated controller invitation'):complete(c,req,kwargs)


def test_scope_change_during_reply_preparation_cannot_publish_superseded_authority(original):
    c,old,kwargs=original;code=issue(original);req,_=reserve(original,code)
    def changed(actual):
        if actual!='credential_reply_retained':return
        with c.transaction() as db:
            row=db.execute('SELECT * FROM retarget_invitation_scopes').fetchone();authority=json.loads(row['document'])
            authority['code']['name']='different-operator-choice';raw=canonical(authority)
            db.execute('UPDATE retarget_invitation_scopes SET document=?,document_sha256=?',(raw.decode(),digest(raw)))
            db.execute('UPDATE enrollment_codes SET document=? WHERE id=?',(canonical(authority['code']).decode(),code['record']['code_id']))
    with pytest.raises(Conflict,match='authenticated controller invitation'):complete(c,req,kwargs,fault_hook=changed)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM credential_generations').fetchone()[0]==1


def test_revoked_lost_complete_owner_must_reconcile_before_replacement(original):
    from quirkbench.contracts import Experiment
    from quirkbench.target_lifecycle import revoke_target
    c,old,kwargs=original;code=issue(original);req,key=reserve(original,code)
    result=complete(c,req,kwargs)
    report=CapabilityReport(result['device_id'],'abandoned-boot',['smoke'],mode='recovery',inventory={
        'media_instance_id':result['media_instance_id'],'target_binding':result['target_binding']})
    c.register(report);c.create_campaign('abandoned-work',result['device_id'])
    c.submit('abandoned-work',Experiment('abandoned','Lost reply work','smoke'));c.resume('abandoned-work')
    attempt=c.claim(result['device_id'],'abandoned-boot','abandoned-claim')
    revoke_target(c.root,result['device_id'],'revoke-abandoned',generation=result['credential_generation']['generation'])
    with pytest.raises(Conflict,match='reconcile'):
        issue(original,new_name='replacement',request_id='replacement-invitation')
    with c.transaction() as db:assert db.execute("SELECT COUNT(*) FROM enrollment_codes WHERE request_id='replacement-invitation'").fetchone()[0]==0
    c.startup();c.resolve(attempt['attempt_id'],'abandon','Abandoned generation reconciled explicitly')
    replacement=issue(original,new_name='replacement',request_id='replacement-invitation')
    assert replacement['record']['code_id']!=code['record']['code_id']
