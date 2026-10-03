"""Installed fresh-agent read and human reply boundaries, without native execution."""
import base64
import json
import time
from datetime import datetime, timezone
import pytest
from quirkbench import cli, investigation_context as context
from quirkbench.contracts import Conflict, ContractError, canonical, digest
from quirkbench.controller import Controller
from quirkbench.state_reader import StateReader, QUERY_BYTES
from test_investigations import setup, start, observations, repository


@pytest.fixture
def lab(setup):
    c, catalog = setup
    start(setup)
    return c, catalog


def question(name='question',session='investigation',attempt=None):
    now=time.time()
    stamp=lambda n: datetime.fromtimestamp(n, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {'schema_version':1,'request_id':name,'session_id':session,'attempt_id':attempt,
        'recipe_step_id':'collect','kind':'post_test_interpretation','prompt':'Did you observe the symptom?',
        'issued_at':stamp(now-10),'deadline_at':stamp(now-1)}


def answer(q):
    return {'schema_version':1,'request_id':q['request_id'],'session_id':q['session_id'],
        'operator_id':'operator','answered_at':q['issued_at'],'answer':'uncertain','note':None}


def populate(c,count=1):
    with c.transaction() as db:
        db.execute('INSERT INTO experiments VALUES(?,?)',('experiment',canonical({'artifacts':{'deployment':'a'*64},'recipe':'system-observation'}).decode()))
        for n in range(count):
            cur=db.execute("INSERT INTO jobs(campaign,experiment,repetition,state) VALUES('investigation','experiment',?,'DONE')",(n,))
            db.execute("INSERT INTO attempts(id,job,device,boot,generation,token,lease_until,deadline,state) VALUES(?,?,'target-1','boot',1,'PRIVATE-TOKEN',0,0,'DONE')",('attempt-'+str(n),cur.lastrowid))
            db.execute('INSERT INTO events(campaign,created,kind,document) VALUES(?,?,?,?)',('investigation',n,'note',canonical({'note':'x'*4096}).decode()))
    identity=c.store.put(b'public evidence').sha256
    with c.transaction() as db:
        db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',('attempt-0','kernel',0,identity,15))
        db.execute('INSERT INTO refs VALUES(?,?)',('attempt:attempt-0',identity))
    return identity


def test_fresh_context_resources_and_readonly_cli(lab,monkeypatch,capsys):
    c,_=lab
    def forbidden(*a,**kw): raise AssertionError('read initialized or pruned state')
    monkeypatch.setattr(Controller,'__init__',forbidden)
    monkeypatch.setattr('quirkbench.maintenance.prune',forbidden)
    for action in ('context','history','recipes','proposal-schema','observations','brief'):
        assert cli.main(['--state',str(c.root),'investigation',action,'investigation','--json'])==0
        data=json.loads(capsys.readouterr().out)['data']
        assert not data.get('execution_authorized',False)
        assert len(canonical(data))<QUERY_BYTES
    assert cli.main(['--state',str(c.root),'investigation','brief','investigation'])==0
    human=capsys.readouterr().out
    assert 'agent-guide.md' in human and 'product-contracts.v1.schema.json' in human
    assert 'context investigation --json' in human and 'prepare-distribution' in human


def test_large_history_cursor_attribution_and_no_tokens(lab):
    c,_=lab;populate(c,1200);reader=StateReader(c.root)
    value=context.context(reader,'investigation')
    assert value['summary']['attempt_count']==1200
    seen=[];cursor=0
    while True:
        page=context.history(reader,'investigation','attempts',after=cursor,limit=100)
        assert len(canonical(page))<QUERY_BYTES
        assert 'PRIVATE-TOKEN' not in json.dumps(page)
        for item in page['items']:
            assert item['experiment_id']=='experiment' and item['artifacts']['deployment']=='a'*64
            seen.append(item['attempt_id'])
        if page['next_cursor'] is None:break
        cursor=page['next_cursor']
    assert len(set(seen))==1200
    page=context.history(reader,'investigation','events',limit=100)
    assert page['next_cursor'] is not None and len(page['items'])<100
    with pytest.raises(ContractError):context.history(reader,'investigation','attempts',limit=101)


def test_evidence_public_scope_missing_bytes_and_link_rejection(lab,tmp_path):
    c,_=lab;identity=populate(c);reader=StateReader(c.root)
    got=context.evidence_read(reader,'investigation',identity,offset=7,length=8)
    assert base64.b64decode(got['content_base64'])==b'evidence'
    assert got['attributions']['items'][0]['attempt_id']=='attempt-0'
    private=c.store.put(b'private').sha256
    with pytest.raises(ContractError):context.evidence_read(reader,'investigation',private)
    with c.transaction() as db:db.execute('DELETE FROM refs WHERE digest=?',(identity,))
    with pytest.raises(ContractError):context.evidence_read(reader,'investigation',identity)
    with c.transaction() as db:db.execute('INSERT INTO refs VALUES(?,?)',('attempt:attempt-0',identity))
    c.store.path(identity).unlink()
    assert context.evidence_read(reader,'investigation',identity)['status']=='unavailable'
    target=tmp_path/'foreign';target.write_bytes(b'public evidence');c.store.path(identity).symlink_to(target)
    with pytest.raises(ContractError):context.evidence_read(reader,'investigation',identity)


def test_recipe_discovery_non_audio_missing_peripheral_and_unsupported(lab):
    c,_=lab;data=context.recipes(StateReader(c.root),'investigation')
    records={v['recipe_id']:v for v in data['items']}
    assert 'system-observation' in records
    assert not records['audio-observation']['eligible']
    assert data['peripheral_status']=='unknown'
    assert not data['target_code_verified'] and not data['execution_authorized']
    with c.transaction() as db:
        row=db.execute('SELECT report FROM devices').fetchone();report=json.loads(row[0]);report['mode']='simulation'
        db.execute('UPDATE devices SET report=?',(canonical(report).decode(),))
    assert all(not v['eligible'] for v in context.recipes(StateReader(c.root),'investigation')['items'])
    with c.transaction() as db:
        report['mode']='experiment';report['inventory']['architecture']='x86_64'
        report['capabilities']=['recipe.system-observation','recipe.unreviewed-shell']
        db.execute('UPDATE devices SET report=?',(canonical(report).decode(),))
    data=context.recipes(StateReader(c.root),'investigation')
    records={v['recipe_id']:v for v in data['items']}
    assert records['system-observation']['eligible']
    assert not records['audio-observation']['eligible']
    assert data['unsupported_advertisements']==['recipe.unreviewed-shell']


def test_investigation_response_late_replay_conflict_and_foreign_session(lab,tmp_path,capsys):
    c,_=lab;q=question();c.issue_observation('investigation',q)
    path=tmp_path/'response.json';path.write_bytes(canonical(answer(q)))
    command=['--state',str(c.root),'--reserve-gib','0','investigation','respond','investigation','--request','question','--file',str(path),'--request-id','answer-command','--json']
    assert cli.main(command)==0;first=json.loads(capsys.readouterr().out)['data'];assert first['late']
    assert cli.main(command)==0;assert json.loads(capsys.readouterr().out)['data']==first
    changed=answer(q);changed['answer']='observed';path.write_bytes(canonical(changed))
    assert cli.main(command)==3;capsys.readouterr()
    c.create_campaign('foreign','target-1');foreign=question('foreign-question','foreign-session');c.issue_observation('foreign',foreign)
    with pytest.raises(ContractError):c.respond_observation('foreign-session','foreign-question','foreign-answer',canonical(answer(foreign)),campaign_id='investigation')
    with pytest.raises(ContractError):StateReader(c.root).observation_detail('foreign-session','foreign-question',campaign_id='investigation')


def test_query_missing_state_does_not_initialize(tmp_path,capsys):
    state=tmp_path/'absent'
    assert cli.main(['--state',str(state),'investigation','context','unknown','--json'])==2
    assert not state.exists()


def test_brief_actual_workspace_and_presence_without_archive_hashes(lab,repository,monkeypatch):
    import shutil
    from quirkbench import source_workspace
    from quirkbench.state_reader import ReadOnlyStore
    c,_=lab;root,base,_=repository
    path=source_workspace.location(c.root,'investigation-source');path.parent.mkdir(mode=0o700)
    shutil.copytree(root,path);path.chmod(0o700)
    source_workspace.register(c,'investigation','investigation-source',base)
    def forbidden(*a,**kw):raise AssertionError('context hashed potentially large input')
    monkeypatch.setattr(ReadOnlyStore,'verify',forbidden)
    view=context.context(StateReader(c.root),'investigation')
    assert view['source']['workspace_path']==str(path)
    assert view['source']['base_oid']==base and view['source']['writer_state']=='EDITING'
    assert view['baseline']['input_presence_only'] and not view['baseline']['input_bytes_verified']
    path.rename(path.with_name('preserved-edit'))
    view=context.brief(StateReader(c.root),'investigation')
    assert not view['source']['available'] and view['source']['blocking_reason']


def test_large_legacy_event_is_truncated_before_parse(lab):
    c,_=lab
    with c.transaction() as db:
        db.execute('INSERT INTO events(campaign,created,kind,document) VALUES(?,?,?,?)',('investigation',1,'legacy','{"note":"'+'x'*1000000+'"}'))
    page=context.history(StateReader(c.root),'investigation','events')
    assert page['items'][0]['truncated'] and page['items'][0]['document'] is None
    assert len(page['items'][0]['excerpt'])==512


def test_evidence_other_investigation_has_no_authority(lab):
    from quirkbench import investigations
    c,_=lab;identity=populate(c)
    investigations.start(c,'other','target-1','other-start')
    with pytest.raises(ContractError):context.evidence_read(StateReader(c.root),'other',identity)
    page=context.history(StateReader(c.root),'other','evidence')
    assert not page['items']


def test_schema_is_standalone_and_preserves_legacy_digest_semantics(lab):
    import jsonschema
    c,_=lab;data=context.proposal_schema(StateReader(c.root),'investigation')
    jsonschema.Draft202012Validator.check_schema(data['schema'])
    assert data['schema']['properties']['schema_version']['const']==2
    legacy=json.loads(open(data['legacy_schema_path']).read())
    assert legacy['$defs']['agent-proposal']['properties']['base_revision']=={'$ref':'#/$defs/digest'}
    assert data['admission_available'] and not data['execution_authorized']


def test_attended_answer_selection_and_lost_reply_retry(lab,monkeypatch,capsys):
    c,_=lab;q=question();c.issue_observation('investigation',q)
    monkeypatch.setattr('sys.stdin.isatty',lambda:True)
    inputs=iter(['question','operator','uncertain','observed only reset'])
    monkeypatch.setattr('builtins.input',lambda prompt:next(inputs))
    command=['--state',str(c.root),'--reserve-gib','0','investigation','respond','investigation','--request-id','attended-answer']
    assert cli.main(command)==0;first=json.loads(capsys.readouterr().out)
    assert first['late'] and first['response']['request_id']=='question'
    def forbidden(prompt):raise AssertionError('retry prompted or regenerated answer')
    monkeypatch.setattr('builtins.input',forbidden)
    assert cli.main(command)==0;assert json.loads(capsys.readouterr().out)==first


def test_scoped_live_response_preserves_physical_deadline(lab):
    from quirkbench.contracts import Experiment
    from datetime import datetime,timezone
    c,_=lab
    # Use an existing simulation target/attempt adapter, not a physical campaign.
    from quirkbench.contracts import CapabilityReport
    c.register(CapabilityReport('target-1','boot',['smoke'],mode='simulation'))
    c.submit('investigation',Experiment('live-experiment','Observe','smoke'));c.resume('investigation')
    claim=c.claim('target-1','boot','claim')
    with c.transaction() as db:row=db.execute('SELECT id,deadline FROM attempts').fetchone()
    q=question('live-question',attempt=row['id']);q['kind']='live_observation'
    q['deadline_at']=datetime.fromtimestamp(row['deadline']-1,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    c.issue_observation('investigation',q)
    c.respond_observation('investigation','live-question','live-answer',canonical(answer(q)),campaign_id='investigation')
    with c.transaction() as db:assert db.execute('SELECT deadline FROM attempts WHERE id=?',(row['id'],)).fetchone()[0]==row['deadline']


def test_evidence_ancestor_swap_cannot_read_foreign_bytes(lab,tmp_path,monkeypatch):
    import os
    c,_=lab;identity=populate(c);objects=c.root/'artifacts'/'objects'
    foreign=tmp_path/'foreign-objects';foreign.mkdir();(foreign/identity).write_bytes(b'PRIVATE-SECRETS')
    original=os.open;swapped=[False]
    def replace(path,*a,**kw):
        if path=='objects' and not swapped[0]:
            swapped[0]=True;objects.rename(objects.with_name('preserved-objects'));objects.symlink_to(foreign,target_is_directory=True)
        return original(path,*a,**kw)
    monkeypatch.setattr(os,'open',replace)
    with pytest.raises(ContractError,match='linked'):
        context.evidence_read(StateReader(c.root),'investigation',identity)
    assert swapped[0]


def test_attended_retry_rejects_changed_operator(lab,monkeypatch,capsys):
    c,_=lab;q=question();c.issue_observation('investigation',q)
    c.respond_observation('investigation','question','answer-command',canonical(answer(q)),campaign_id='investigation')
    command=['--state',str(c.root),'--reserve-gib','0','investigation','respond','investigation','--request-id','answer-command','--operator','other','--json']
    assert cli.main(command)==3
    assert json.loads(capsys.readouterr().out)['error']['code']=='CONFLICT'
