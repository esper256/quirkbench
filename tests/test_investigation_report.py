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
    argv=['investigation', 'results', 'show', 'investigation']
    assert cli.main(argv+['--json'], state_root=str(c.root))==0
    machine=json.loads(capsys.readouterr().out)['data']
    assert cli.main(argv, state_root=str(c.root))==0
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
    data=report.retain(c.root,'investigation','preserve inconclusive evidence','retain-preserve')
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
    with pytest.raises(ContractError,match='injected'):report.retain(c.root,'investigation','keep','retain-failure')
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


def test_report_and_retention_records_match_strict_installed_schema(lab):
    from pathlib import Path
    import jsonschema
    c,_=lab;populate(c)
    schema=json.loads((Path(__file__).parents[1]/'schemas/investigation-report.v1.schema.json').read_text())
    validator=jsonschema.Draft202012Validator(schema)
    value=report.report(report.ReportReader(c.root),'investigation')
    validator.validate(value);validator.validate(report.retain(c.root,'investigation','schema check','retain-schema'))
    validator.validate(plan([('patched','experiment-0')]))
    value['items'][0]['attempts'][0]['token']='must reject private wire fields'
    assert not validator.is_valid(value)


def test_retention_reports_missing_objects_without_claiming_restoration(lab):
    c,_=lab;identity=populate(c);c.store.path(identity).unlink()
    retained=report.retain(c.root,'investigation','retain partial evidence','retain-partial')
    assert retained['missing_object_count']==1 and retained['missing_objects']==[identity]
    assert not retained['restored_bytes'] and not retained['payload_bytes_verified']


def test_joined_baseline_patch_regression_revert_compare_actual_retained_bytes(published,joined,tmp_path,monkeypatch):
    from test_proposal_dispatch import capture_proposal, dispatch_and_build, bounded_attempt
    c,composition=published;candidate=joined[5]
    with c.lifecycle() as owner:
        baseline=attended_baseline.admit(c,'investigation',composition,'comparison-baseline',ready=lambda _:None)['data']['experiment_id']
        hardware,client,step,backend,boot=attended_lab(c,tmp_path)
        bounded_attempt(c,step,hardware.boot_id,'candidate-base-report','recovery-base-report')
        with c.transaction() as db:workspace=db.execute('SELECT workspace_id FROM investigations').fetchone()[0]
        original=(c.root/'workspaces'/workspace/'init/main.c').read_text()
        declarations=[('baseline',baseline)];recovery='recovery-base-report'
        for role,content in [('patched','patched report comparison\n'),('regression','different regression conditions\n'),('revert',original)]:
            op,proposal,_=capture_proposal(c,owner,monkeypatch,'report-'+role,content)
            _,experiment=dispatch_and_build(c,owner,monkeypatch,op,candidate,'report-dispatch-'+role)
            bounded_attempt(c,step,recovery,'candidate-report-'+role,'recovery-report-'+role)
            recovery='recovery-report-'+role;declarations.append((role,experiment))
        pages=[];cursor=0;reader=report.ReportReader(c.root)
        with reader.connection():
            while True:
                value=report.report(reader,'investigation',plan=plan(declarations),after=cursor)
                pages.append(value)
                if value['next_cursor'] is None:break
                cursor=value['next_cursor']
    items=[row for page in pages for row in page['items']]
    assert [row['comparison_role'] for row in items]==['baseline','patched','regression','revert']
    sources=[]
    for row in items:
        assert row['attribution']['metadata_verified'],row['attribution']
        assert row['baseline_comparison']['state']=='metadata_compared'
        assert row['baseline_comparison']['conditions_equivalent'] is None
        assert row['attempts'][0]['exact_candidate_adoption'] and row['attempts'][0]['problem_reproduced'] is None
        sources.append(row['attribution']['identities']['source_capture_sha256'])
    assert len(set(sources))>=3
    assert 'source_capture_sha256' in items[1]['baseline_comparison']['different_identities']
    assert value['conclusion']=='inconclusive'
    from pathlib import Path
    import jsonschema
    for page in pages:jsonschema.validate(page,json.loads((Path(__file__).parents[1]/'schemas/investigation-report.v1.schema.json').read_text()))


def test_retention_exact_replay_freezes_scope_and_cross_kind_request_namespace(lab):
    from quirkbench.contracts import Conflict
    c,_=lab;populate(c)
    first=report.retain(c.root,'investigation','original selection','retention-request')
    with c.transaction() as db:
        job=db.execute("INSERT INTO jobs(campaign,experiment,repetition,state) VALUES('investigation','experiment-0',2,'DONE')").lastrowid
        db.execute("INSERT INTO attempts(id,job,device,boot,generation,token,lease_until,deadline,state) VALUES('later',?,'target-1','boot',1,'PRIVATE-LATER',0,0,'COMPLETE')",(job,))
        db.execute("INSERT INTO refs SELECT 'attempt:later',digest FROM refs WHERE owner='attempt:attempt-0-0'")
    assert report.retain(c.root,'investigation','original selection','retention-request')==first
    with c.transaction() as db:assert not db.execute("SELECT 1 FROM storage_pins WHERE owner='attempt:later'").fetchone()
    with pytest.raises(Conflict,match='replay'):report.retain(c.root,'investigation','changed selection','retention-request')
    assert 'attempt:later' in report.retain(c.root,'investigation','original selection','new-retention-request')['pinned_owners']
    from quirkbench.attended_baseline import check_request
    with c.transaction() as db:
        with pytest.raises(Conflict,match='another'):check_request(db,'retention-request','operations')
    with pytest.raises(Conflict,match='another'):report.retain(c.root,'investigation','owner collision','start-request')


def test_valid_large_closure_and_well_formed_unadmitted_or_alternate_recipe_inputs(published,monkeypatch):
    c,composition=published
    original=attended_baseline.admit(c,'investigation',composition,'large-report',ready=lambda _:None)['data']['experiment_id']
    with c.transaction() as db:
        for n in range(200):
            blob=c.store.put(('synthetic pinned package payload '+str(n)).encode()).sha256
            db.execute('INSERT INTO refs VALUES(?,?)',('experiment:'+original,blob))
        spec=json.loads(db.execute('SELECT spec FROM experiments WHERE id=?',(original,)).fetchone()[0])
    view=report.report(report.ReportReader(c.root),'investigation')['items'][0]
    assert view['attribution']['metadata_verified'] and view['comparison_role']=='baseline'
    assert view['attribution']['required_object_count']>200 and view['attribution']['required_objects_truncated']
    assert len(view['attribution']['required_objects'])<128
    proof=attended_baseline.document(c,spec['artifacts']['attended_baseline'])
    newproof={**proof,'experiment_id':'manual-input'}
    forged=c.store.put(canonical(newproof)).sha256
    manual={**spec,'experiment_id':'manual-input','artifacts':{**spec['artifacts'],'attended_baseline':forged}}
    # A regular manually submitted experiment may retain opaque extra artifacts;
    # they cannot impersonate controller-derived attended baseline admission.
    c.submit_attended('investigation',Experiment.from_dict(manual))
    with c.transaction() as db:
        db.execute("INSERT OR IGNORE INTO refs SELECT 'experiment:manual-input',digest FROM refs WHERE owner=?",('experiment:'+original,))
    unadmitted=report.report(report.ReportReader(c.root),'investigation',experiment='manual-input')['items'][0]
    assert not unadmitted['attribution']['metadata_verified'] and unadmitted['comparison_role']=='unclassified'
    recipe=report.stored(attended_baseline.raw_metadata(c,proof['recipe_manifest_sha256']).decode(),'recipe')
    alternate=c.store.put(canonical({**recipe,'version':recipe['version']+1})).sha256
    changedproof={**proof,'recipe_manifest_sha256':alternate}
    changed_input=c.store.put(canonical(changedproof)).sha256
    altered={**spec,'artifacts':{**spec['artifacts'],'recipe_manifest':alternate,'attended_baseline':changed_input}}
    with c.transaction() as db:
        db.execute('UPDATE experiments SET spec=? WHERE id=?',(canonical(altered).decode(),original))
        # Make the admission digest agree, isolating the actual deployed recipe check.
        db.execute('UPDATE attended_baseline_commands SET input_digest=? WHERE experiment=?',(changed_input,original))
        db.execute('INSERT INTO refs VALUES(?,?)',('experiment:'+original,alternate))
        db.execute('INSERT INTO refs VALUES(?,?)',('experiment:'+original,changed_input))
    wrong=report.report(report.ReportReader(c.root),'investigation',experiment=original)['items'][0]
    assert not wrong['attribution']['metadata_verified'] and wrong['comparison_role']=='unclassified'


def test_same_recovery_start_interrupted_handoff_result_is_not_candidate_adoption(published,tmp_path):
    c,composition=published
    experiment=attended_baseline.admit(c,'investigation',composition,'interrupted-report',ready=lambda _:None)['data']['experiment_id']
    hardware,client,step,backend,boot=attended_lab(c,tmp_path)
    assert step()=='awaiting_operator_approval'
    with c.transaction() as db:attempt,token=db.execute('SELECT id,token FROM attempts').fetchone()
    c.decide_attempt(attempt,'approved',request_id='approve-interrupted',operator='operator')
    c.start(attempt,token,hardware.boot_id)
    source=report.report(report.ReportReader(c.root),'investigation')['items'][0]['attribution']
    c.handoff(attempt,token,hardware.boot_id,source['deployment_revision'])
    c.recovery_returned(attempt,token,hardware.boot_id)
    c.complete(Result(attempt,'INCONCLUSIVE','Candidate handoff interrupted.'),token,hardware.boot_id)
    fact=report.report(report.ReportReader(c.root),'investigation')['items'][0]['attempts'][0]
    assert fact['candidate_requested'] and fact['attempt_started']
    assert fact['boot_id']==hardware.boot_id and not fact['exact_candidate_adoption']
    assert fact['problem_reproduced'] is None


def test_retention_machine_input_and_receipt_privacy_are_strict(lab,capsys):
    c,_=lab;populate(c)
    argv=['investigation', 'results', 'retain', 'investigation', '--note', 'preserve', '--json']
    assert cli.main(argv, state_root=str(c.root))==0
    implicit=json.loads(capsys.readouterr().out)['data']
    assert implicit['request_id'].startswith('report-retain-')
    assert cli.main(argv+['--request-id','cli-retain'], state_root=str(c.root))==0
    receipt=json.loads(capsys.readouterr().out)['data']
    assert receipt['request_id']=='cli-retain'
    with c.transaction() as db:
        receipt['token']='PRIVATE-RECEIPT-TOKEN'
        db.execute('UPDATE report_retention_commands SET result_document=? WHERE request_id=?',(canonical(receipt).decode(),'cli-retain'))
    with pytest.raises(ContractError,match='invalid retained'):report.retain(c.root,'investigation','preserve','cli-retain')
