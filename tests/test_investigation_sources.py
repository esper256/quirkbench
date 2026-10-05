"""Installed source commands join existing durable workers without new ownership."""
import json
from types import SimpleNamespace
import pytest

from quirkbench import cli, investigation_sources as facade, source_workspace
from quirkbench.contracts import Conflict, ContractError
from quirkbench.job_coordinator import JobCoordinator
from test_source_prepare_operation import setup
from test_source_capture import repository
from test_source_operation import worker
from test_builder_setup import Workers


def args(action, **values):
    return SimpleNamespace(**{
            'action': action, 'name': 'campaign', 'workspace': None, 'source': None,
            'base_oid': None, 'request_id': None, 'quiesced': False, 'allow_untracked': [],
            'json': True, 'reserve_gib': 0, **values})


def prepare(setup):
    c, root, base = setup
    return facade.execute(c.root, args('prepare-source', source=root, base_oid=base,
        request_id='prepare-command', quiesced=True), ready=lambda _: None)


def test_commands_join_private_preparation_capture_release_and_historical_replay(setup, monkeypatch):
    c, root, base = setup
    (root/'driver.c').write_text('dirty original')
    with c.lifecycle() as owner:
        response = prepare(setup)
        services = Workers(); coordinator = JobCoordinator(owner, services)
        assert coordinator.tick() is None
        state = facade.execute(c.root, args('source'))['data']
        assert state['writer_state'] is None and state['workspace_path'] is None
        assert state['preparation_operation'] == response['operation_id']
        facade.execute(c.root, args('resume'))
        claim = coordinator.tick(); assert worker(c, claim, monkeypatch) == 0
        services.done = True; assert coordinator.tick()['state'] == 'SUCCEEDED'
        state = facade.execute(c.root, args('source'))['data']
        assert state['writer_state'] == 'EDITING' and state['available']
        assert state['base_oid'] == base
        assert prepare(setup)['operation_id'] == response['operation_id']
        path = source_workspace.location(c.root, 'campaign-source')
        (path/'driver.c').write_text('agent approved edit')
        capture_args = args('capture-source', request_id='capture-command', quiesced=True)
        response = facade.execute(c.root, capture_args, ready=lambda _: None)
        with pytest.raises(Conflict): facade.execute(c.root, args('release-source'))
        services.done = False
        claim = coordinator.tick(); assert worker(c, claim, monkeypatch) == 0
        services.done = True; assert coordinator.tick()['state'] == 'SUCCEEDED'
        facade.execute(c.root, args('release-source'))
        (path/'driver.c').write_text('later editing period')
        assert facade.execute(c.root, capture_args, ready=lambda _: None)['operation_id'] == response['operation_id']
        assert facade.execute(c.root, args('source'))['data']['writer_state'] == 'EDITING'
        assert (path/'driver.c').read_text() == 'later editing period'
        assert (root/'driver.c').read_text() == 'dirty original'
        assert facade.execute(c.root, args('status'))['data']['sources'][0]['available']


def test_cli_acknowledges_operation_without_starting_worker(setup, monkeypatch, capsys):
    from quirkbench import controller_service
    c, root, base = setup
    monkeypatch.setattr(controller_service, 'require_ready', lambda _: None)
    command = ['--state', str(c.root), 'investigation', 'source', 'prepare', 'campaign', '--source', str(root), '--base-oid', base, '--request-id', 'cli-preparation', '--quiesced', '--json', '--reserve-gib', '0']
    assert cli.main(command) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['operation_id']
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)['operation_id'] == response['operation_id']
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 1
        assert db.execute('SELECT worker_unit FROM operations').fetchone()[0] is None


@pytest.mark.parametrize('action', ['status', 'source'])
def test_readonly_cli_never_initializes_or_prunes(setup, monkeypatch, capsys, action):
    from quirkbench import controller, maintenance
    c, root, base = setup; prepare(setup)
    def forbidden(*a, **kw): raise AssertionError('read-only command mutated state')
    monkeypatch.setattr(controller, 'Controller', forbidden)
    monkeypatch.setattr(maintenance, 'prune', forbidden)
    assert cli.main(['--state', str(c.root), 'investigation', *(['source','show'] if action=='source' else [action]), 'campaign', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['data']


def test_explicit_workspace_scope_and_ambiguity(setup):
    c, root, base = setup; prepare(setup)
    from quirkbench.source_prepare_operation import submit
    submit(c, 'campaign', 'second-source', root, base, 'second-request', quiesced=True, ready=lambda _: None)
    with pytest.raises(Conflict): facade.execute(c.root, args('source'))
    assert facade.execute(c.root, args('source', workspace='campaign-source'))['data']['workspace_id'] == 'campaign-source'
    c.create_campaign('other-campaign', 'target')
    with pytest.raises(Conflict): facade.execute(c.root, args('source', name='other-campaign', workspace='campaign-source'))


@pytest.mark.parametrize('change', [{'quiesced': False}])
def test_preparation_requires_explicit_handoff_and_json_retry_identity(setup, change):
    c, root, base = setup
    values = {'source': root, 'base_oid': base, 'request_id': 'request', 'quiesced': True, **change}
    with pytest.raises((Conflict, ContractError)):
        facade.execute(c.root, args('prepare-source', **values), ready=lambda _: None)
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0


def test_missing_investigation_does_not_create_state(tmp_path):
    state = tmp_path/'never-created'
    with pytest.raises(ContractError): facade.execute(state, args('status'))
    assert not state.exists()


def test_capture_needs_fresh_explicit_request(setup):
    c, root, base = setup; prepare(setup)
    with pytest.raises(ContractError, match='request-id'):
        facade.execute(c.root, args('capture-source', quiesced=True), ready=lambda _: None)


def test_human_preparation_uses_stable_named_request(setup):
    c, root, base = setup
    command = args('prepare-source', source=root, base_oid=base, quiesced=True, json=False)
    first = facade.execute(c.root, command, ready=lambda _: None)
    assert facade.execute(c.root, command, ready=lambda _: None) == first


def test_exact_cli_forms_and_creation_require_existing_target_are_explicit():
    parsed = cli.parser().parse_args(['investigation', 'source', 'capture', 'existing', '--workspace', 'kernel', '--request-id', 'capture', '--quiesced', '--json'])
    assert parsed.name == 'existing' and parsed.workspace == 'kernel' and parsed.quiesced
    parsed = cli.parser().parse_args(['investigation','start','new','--target','target'])
    assert parsed.target=='target' and parsed.action=='start'
    with pytest.raises(SystemExit):cli.parser().parse_args(['investigation','start','new'])
