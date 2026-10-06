"""Investigation filtering is read-only even with an idle/offline controller."""
import json
import pytest
from quirkbench import cli,tui
from quirkbench.controller import Controller
from quirkbench.contracts import ContractError
from quirkbench.state_reader import StateReader
from test_investigations import setup,start,observations,repository
from test_investigation_context import question


def test_filter_precedes_global_limit_and_reports_pending_human_waits(setup,monkeypatch,capsys):
    c,_=setup;start(setup)
    c.issue_observation('investigation',question())
    with c.transaction() as db:
        for n in range(80):
            db.execute("INSERT INTO operations(id,request_id,request_digest,input_digest,kind,campaign,state,created,updated) VALUES(?,?,?,?,'external_proposal',?,'QUEUED',?,?)",(str(n),str(n),'a'*64,'b'*64,'investigation' if n==0 else None,n,n))
    before=(c.root/'controller.sqlite').read_bytes()
    def forbidden(*a,**kw):raise AssertionError('query acquired writer/service ownership')
    monkeypatch.setattr(Controller,'__init__',forbidden)
    monkeypatch.setattr('quirkbench.maintenance.prune',forbidden)
    assert cli.main(['monitor','investigation','--json'], state_root=str(c.root))==0
    data=json.loads(capsys.readouterr().out)['data']
    assert [r['id'] for r in data['operations']]==['0']
    assert [r['id'] for r in data['investigations']]==['investigation']
    facts=data['investigation_facts']
    assert facts['admission_stopped'] and facts['pending_observations'][0]['state']=='overdue'
    assert not facts['physical_poweroff_verified'] and facts['local_evidence_durable'] is None
    assert (c.root/'controller.sqlite').read_bytes()==before
    assert cli.main(['monitor','investigation','--once'], state_root=str(c.root))==0
    assert 'Human request question' in capsys.readouterr().out


def test_filter_refuses_unknown_or_legacy_record_and_run_overlap(setup):
    c,_=setup;start(setup);reader=StateReader(c.root)
    c.create_campaign('legacy','target-1')
    for name in ('missing','legacy'):
        with pytest.raises(ContractError):tui.snapshot(reader,investigation=name)
    with pytest.raises(ContractError):tui.snapshot(reader,run_id='run',investigation='investigation')


@pytest.mark.parametrize('field',['progress','wait_event','reason','report','shutdown'])
def test_large_retained_fields_are_bounded_before_parsing(setup,monkeypatch,field):
    c,_=setup;start(setup);large='x'*(1024**2)
    with c.transaction() as db:
        if field=='reason':db.execute('UPDATE campaigns SET reason=?',(large,))
        elif field=='report':db.execute('UPDATE devices SET report=?',(large,))
        elif field=='shutdown':
            db.execute("INSERT INTO target_shutdown_requests VALUES('shutdown','a','target-1','target-1','{}','{}','PREPARED',?)",(large,))
        else:
            db.execute("INSERT INTO operations(id,request_id,request_digest,input_digest,kind,campaign,state,created,updated,"+field+") VALUES('large','large','a','b','external_proposal','investigation','QUEUED',1,1,?)",(large,))
    original=json.loads
    def bounded(raw,*a,**kw):
        if len(raw)>512*1024:raise AssertionError('oversized metadata reached a parser')
        return original(raw,*a,**kw)
    if field in ('report','shutdown'):monkeypatch.setattr(json,'loads',bounded)
    reader=StateReader(c.root)
    if field in ('report','shutdown'):
        with pytest.raises(ContractError,match='bounded|budget'):tui.snapshot(reader,investigation='investigation')
    else:
        snapshot=tui.snapshot(reader,investigation='investigation')
        assert len(json.dumps(snapshot))<65536
        if field=='reason':assert len(snapshot['investigations'][0]['reason'])==512
        else:assert snapshot['operations'][0][field] is None and snapshot['operations'][0]['details_truncated']


def test_monitor_preflight_and_helpers_share_one_wal_snapshot(setup,monkeypatch):
    from quirkbench import target_setup
    c,_=setup;start(setup);original=target_setup.resolve_target_identity;changed=[False]
    def concurrent(db,target):
        assert db.in_transaction and db.execute('PRAGMA query_only').fetchone()[0]==1
        if not changed[0]:
            changed[0]=True
            with c.transaction() as writer:writer.execute('UPDATE devices SET report=?',('x'*(1024**2),))
        return original(db,target)
    monkeypatch.setattr(target_setup,'resolve_target_identity',concurrent)
    value=tui.snapshot(StateReader(c.root),investigation='investigation')
    assert value['investigation_facts']['recovery']['report_available']
    with pytest.raises(ContractError,match='bounded'):tui.snapshot(StateReader(c.root),investigation='investigation')
