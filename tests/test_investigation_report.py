"""Report software attribution; physical/native qualification remains external."""
import json
from dataclasses import asdict
import pytest
from quirkbench import investigation_report as report, cli, attended_baseline
from quirkbench.contracts import Experiment, Result, ContractError, canonical, digest
from test_investigation_context import lab, question, answer
from test_investigations import setup, observations
from test_attended_baseline import published, joined, bounded_build, bounded_compose, attended_lab, candidate_setup, assembly_setup


def populate(c,count=1,attempts=1):
    evidence=c.store.put(b'public evidence').sha256
    with c.transaction() as db:
        for n in range(count):
            exp='experiment-'+str(n)
            spec=Experiment(exp,'Non-audio input observation','system-observation',artifacts={'deployment':'a'*64}).to_dict()
            db.execute('INSERT INTO experiments VALUES(?,?)',(exp,canonical(spec).decode()))
            db.execute('INSERT INTO refs VALUES(?,?)',('experiment:'+exp,evidence))
            for j in range(attempts):
                job=db.execute("INSERT INTO jobs(campaign,experiment,repetition,state) VALUES('investigation',?,?,'DONE')",(exp,j)).lastrowid
                identity='attempt-'+str(n)+'-'+str(j)
                result=asdict(Result(identity,'PASS','Execution completed',[evidence],measurements={'untrusted_secret':'do not echo'},limitations=['Peripheral unavailable; stimulus not observed.']))
                db.execute("INSERT INTO attempts(id,job,device,boot,generation,token,lease_until,deadline,state,result,started) VALUES(?,?,'target-1','boot',1,'PRIVATE-TOKEN',0,0,'COMPLETE',?,1)",(identity,job,canonical(result).decode()))
                db.execute('INSERT INTO refs VALUES(?,?)',('attempt:'+identity,evidence))
                db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(identity,'log',0,evidence,15))
    return evidence


def plan(roles):
    return {'schema_version':1,'record_type':'investigation-comparison','investigation_id':'investigation',
        'roles':[{'role':role,'experiment_id':exp} for role,exp in roles]}


def test_all_comparison_roles_and_incomplete_identity_never_become_causal_claims(lab):
    c,_=lab;populate(c,5)
    declaration=plan(zip(report.ROLES,('experiment-'+str(i) for i in range(5))))
    data=report.report(report.ReportReader(c.root),'investigation',plan=declaration)
    assert len(data['items'])==5 and data['conclusion']=='inconclusive'
    assert data['comparison_sha256']==digest(canonical(declaration))
    for row,role in zip(data['items'],report.ROLES):
        assert row['comparison_role']==role and row['role_origin']=='declared'
        assert row['exposure']['started_attempts']==1 and row['exposure']['stimulus_exposures'] is None
        assert row['attribution']['state']=='unavailable' and row['problem_reproduced'] is None
        attempt=row['attempts'][0]
        assert attempt['execution_outcome']=='PASS' and not attempt['exact_candidate_adoption']
        assert attempt['problem_reproduced'] is None and attempt['causal_claim'] is None
        assert 'problem_observation_not_requested' in attempt['missing']
        assert 'Peripheral unavailable' in attempt['limitations'][0]
    raw=json.dumps(data)
    assert 'PRIVATE-TOKEN' not in raw and 'untrusted_secret' not in raw and 'do not echo' not in raw


def test_cli_human_json_same_facts_and_reads_do_not_initialize_prune_or_pin(lab,monkeypatch,capsys):
    c,_=lab;populate(c)
    def forbidden(*a,**kw):raise AssertionError('read mutated controller')
    monkeypatch.setattr('quirkbench.controller.Controller.__init__',forbidden)
    monkeypatch.setattr('quirkbench.maintenance.prune',forbidden)
    monkeypatch.setattr('quirkbench.retention.pin',forbidden)
    argv=['--state',str(c.root),'investigation','report','investigation']
    assert cli.main(argv+['--json'])==0
    machine=json.loads(capsys.readouterr().out)['data']
    assert cli.main(argv)==0
    human=json.loads(capsys.readouterr().out)
    assert machine==human


def test_experiment_and_attempt_cursors_are_complete_and_counts_not_page_counts(lab):
    c,_=lab;populate(c,12,3);reader=report.ReportReader(c.root)
    ids=[];cursor=0
    while True:
        page=report.report(reader,'investigation',after=cursor,limit=2,attempt_limit=1)
        assert len(canonical(page))<=65536
        ids.extend(r['experiment_id'] for r in page['items'])
        for row in page['items']:
            assert row['exposure']['attempts']==3 and len(row['attempts'])==1
            assert row['next_attempt_cursor'] is not None
        if page['next_cursor'] is None:break
        cursor=page['next_cursor']
    assert len(set(ids))==12
    attempts=[];cursor=0
    while True:
        row=report.report(reader,'investigation',experiment='experiment-0',attempt_after=cursor,attempt_limit=1)['items'][0]
        attempts += [a['attempt_id'] for a in row['attempts']]
        if row['next_attempt_cursor'] is None:break
        cursor=row['next_attempt_cursor']
    assert len(set(attempts))==3


def test_missing_bytes_unacknowledged_chunks_and_late_answers_keep_original_attempt(lab):
    c,_=lab;identity=populate(c,attempts=2)
    q=question(attempt='attempt-0-0');c.issue_observation('investigation',q)
    c.respond_observation('investigation',q['request_id'],'response',canonical(answer(q)),campaign_id='investigation')
    c.store.path(identity).unlink()
    with c.transaction() as db:db.execute('DELETE FROM evidence WHERE attempt=?',('attempt-0-0',))
    attempts=report.report(report.ReportReader(c.root),'investigation')['items'][0]['attempts']
    assert attempts[0]['observations']['items'][0]['late'] is True
    assert attempts[0]['observations']['items'][0]['answer']=='uncertain'
    assert attempts[1]['observations']['total']==0
    assert attempts[0]['evidence']['acknowledged_count']==0
    assert not attempts[1]['evidence']['items'][0]['bytes_present']
    assert all(a['problem_reproduced'] is None for a in attempts)


@pytest.mark.parametrize('kind',['spec','result','question'])
def test_oversized_opaque_values_refused_before_json_parser(lab,monkeypatch,kind):
    c,_=lab;populate(c)
    if kind=='question':
        q=question(attempt='attempt-0-0');c.issue_observation('investigation',q)
    with c.transaction() as db:
        if kind=='spec':db.execute('UPDATE experiments SET spec=?',('x'*1000000,))
        elif kind=='result':db.execute('UPDATE attempts SET result=?',('x'*1000000,))
        else:
            db.execute('UPDATE observation_requests SET document=?',('x'*1000000,))
    original=report.stored
    def bounded(raw,*args):
        assert len(raw.encode())<=65536
        return original(raw,*args)
    monkeypatch.setattr(report,'stored',bounded)
    with pytest.raises(ContractError,match='budget'):report.report(report.ReportReader(c.root),'investigation')


def test_wrong_comparison_scope_duplicate_roles_unknown_records_and_symlink_inputs(lab,tmp_path):
    c,_=lab;populate(c)
    with pytest.raises(ContractError,match='another'):report.report(report.ReportReader(c.root),'investigation',plan=plan([('patched','foreign')]))
    with pytest.raises(ContractError):report.comparison(plan([('baseline','experiment-0'),('patched','experiment-0')]),'investigation')
    path=tmp_path/'comparison.json';path.write_bytes(canonical(plan([('revert','experiment-0')])))
    assert report.load_comparison(path,'investigation')['roles'][0]['role']=='revert'
    link=tmp_path/'linked';link.symlink_to(path)
    with pytest.raises(OSError):report.load_comparison(link,'investigation')
    c.create_campaign('legacy','target-1')
    with pytest.raises(ContractError,match='existing investigation'):report.report(report.ReportReader(c.root),'legacy')


def test_retention_uses_existing_owners_and_cannot_revive_expired_bytes(lab):
    c,_=lab;populate(c,attempts=2)
    with c.transaction() as db:db.execute("INSERT INTO storage_retired VALUES('attempt:attempt-0-1',1)")
    data=report.retain(c.root,'investigation','preserve inconclusive evidence')
    assert 'experiment:experiment-0' in data['pinned_owners']
    assert 'attempt:attempt-0-0' in data['pinned_owners']
    assert data['unavailable']==[{'owner':'attempt:attempt-0-1','reason':'retired_bytes_cannot_be_restored'}]
    assert not data['restored_bytes'] and not data['payload_bytes_verified']
    with c.transaction() as db:
        pins={r[0] for r in db.execute('SELECT owner FROM storage_pins')}
        assert pins==set(data['pinned_owners'])
        from quirkbench.retention import _retire_candidates
        from quirkbench.retention_settings import settings
        config=settings(c.root);config['completed_attempts']=0;config['completed_builds']=0
        assert 'attempt:attempt-0-0' not in _retire_candidates(db,config,c.root)
        assert 'experiment:experiment-0' not in _retire_candidates(db,config,c.root)


def test_retention_failure_rolls_back_all_new_pins(lab,monkeypatch):
    c,_=lab;populate(c)
    from quirkbench import retention
    original=retention.pin_db
    calls=[]
    def fail(db,owner,note):
        calls.append(owner)
        if len(calls)==2:raise ContractError('injected pin failure')
        return original(db,owner,note)
    monkeypatch.setattr(retention,'pin_db',fail)
    with pytest.raises(ContractError,match='injected'):report.retain(c.root,'investigation','keep')
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM storage_pins').fetchone()[0]==0


def test_joined_published_baseline_report_preserves_exact_chain_and_runtime_evidence(published,tmp_path,monkeypatch):
    c,composition=published
    experiment=attended_baseline.admit(c,'investigation',composition,'baseline-report',ready=lambda _:None)['data']['experiment_id']
    report_before=report.report(report.ReportReader(c.root),'investigation')
    source=report_before['items'][0]['attribution']
    assert source['state']=='available',source
    assert source['input_type']=='attended-baseline-input' and source['recipe_version']==1
    assert source['identities']['composition_operation_id']==composition and source['identities']['candidate_operation_id']
    assert not source['source_bytes_verified']
    hardware,client,step,backend,boot=attended_lab(c,tmp_path)
    assert step()=='awaiting_operator_approval'
    with c.transaction() as db:attempt=db.execute('SELECT id FROM attempts').fetchone()[0]
    c.decide_attempt(attempt,'approved',request_id='approve-report',operator='operator')
    assert step()=='candidate_requested'
    assert step('candidate-report','experiment')=='recovery_requested'
    assert step('recovery-report')=='completed'
    final=report.report(report.ReportReader(c.root),'investigation')['items'][0]
    assert final['comparison_role']=='baseline' and final['exposure']['started_attempts']==1
    fact=final['attempts'][0]
    assert fact['exact_candidate_adoption'] and fact['execution_outcome']=='PASS'
    assert fact['evidence']['declared_count']==3 and fact['recovery_returned']
    assert fact['problem_reproduced'] is None and final['exposure']['stimulus_exposures'] is None
    # Corrupt stored metadata must not preserve verified source attribution.
    c.store.path(source['input_sha256']).write_bytes(b'corrupt')
    broken=report.report(report.ReportReader(c.root),'investigation')['items'][0]
    assert not broken['attribution']['metadata_verified'] and not broken['attempts'][0]['exact_candidate_adoption']
