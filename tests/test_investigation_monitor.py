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
    assert cli.main(['--state',str(c.root),'monitor','investigation','--json'])==0
    data=json.loads(capsys.readouterr().out)['data']
    assert [r['id'] for r in data['operations']]==['0']
    assert [r['id'] for r in data['investigations']]==['investigation']
    facts=data['investigation_facts']
    assert facts['admission_stopped'] and facts['pending_observations'][0]['state']=='overdue'
    assert not facts['physical_poweroff_verified'] and facts['local_evidence_durable'] is None
    assert (c.root/'controller.sqlite').read_bytes()==before
    assert cli.main(['--state',str(c.root),'monitor','investigation','--once'])==0
    assert 'Human request question' in capsys.readouterr().out


def test_filter_refuses_unknown_or_legacy_record_and_run_overlap(setup):
    c,_=setup;start(setup);reader=StateReader(c.root)
    c.create_campaign('legacy','target-1')
    for name in ('missing','legacy'):
        with pytest.raises(ContractError):tui.snapshot(reader,investigation=name)
    with pytest.raises(ContractError):tui.snapshot(reader,run_id='run',investigation='investigation')
