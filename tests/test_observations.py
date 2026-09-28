"""P7b human observation durability and physical deadline boundaries."""
from datetime import datetime, timezone
import json

import pytest

from quirkbench.cli import main
from quirkbench.contracts import CapabilityReport, Conflict, ContractError, Experiment, canonical
from quirkbench.controller import Controller
from quirkbench.monitor import render


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


@pytest.fixture
def lab(tmp_path):
    now = [1_800_000_000.0]
    controller = Controller(tmp_path / 'state', clock=lambda: now[0], reserve_bytes=0)
    controller.register(CapabilityReport('target', 'boot', ['smoke'], mode='simulation'))
    controller.create_campaign('campaign', 'target')
    controller.submit('campaign', Experiment('experiment', 'Human observation', 'smoke'))
    return controller, now


def question(now, name, kind, *, attempt=None, deadline=60):
    return {'schema_version': 1, 'request_id': name, 'session_id': 'session',
            'attempt_id': attempt, 'recipe_step_id': 'step', 'kind': kind,
            'prompt': 'What happened on the target?', 'issued_at': stamp(now),
            'deadline_at': stamp(now + deadline)}


def answer(now, name, result='observed'):
    return {'schema_version': 1, 'request_id': name, 'session_id': 'session',
            'operator_id': 'operator', 'answered_at': stamp(now), 'answer': result,
            'note': 'Observed by the operator.'}


def test_readiness_and_post_test_survive_restart_and_backup(lab, tmp_path):
    controller, now = lab
    first = question(now[0], 'readiness', 'pre_test_readiness')
    last = question(now[0], 'interpretation', 'post_test_interpretation')
    assert controller.issue_observation('campaign', first) == 'readiness'
    assert controller.issue_observation('campaign', first) == 'readiness'
    controller.issue_observation('campaign', last)
    with pytest.raises(Conflict):
        controller.issue_observation('campaign', {**first, 'prompt': 'Changed prompt'})
    replied = controller.respond_observation('session', 'readiness', 'command-1', canonical(answer(now[0], 'readiness')))
    assert not replied['late']
    restarted = Controller(controller.root, clock=lambda: now[0], reserve_bytes=0)
    listed = restarted.list_observations('session')
    assert [item['state'] for item in listed['items']] == ['answered', 'pending']
    backup = tmp_path / 'backup'
    restarted.backup(backup)
    restored = Controller.restore(backup, tmp_path / 'restored', clock=lambda: now[0], reserve_bytes=0)
    assert restored.list_observations('session') == listed


def test_live_observation_cannot_extend_attempt_and_late_reply_stays_on_original(lab):
    controller, now = lab
    controller.resume('campaign')
    claim = controller.claim('target', 'boot', 'claim')
    attempt_id = claim['attempt_id']
    physical_deadline = controller.status('campaign')['attempts'][0]['deadline']
    live = question(now[0], 'live-one', 'live_observation', attempt=attempt_id, deadline=30)
    controller.issue_observation('campaign', live)
    with pytest.raises(ContractError):
        controller.issue_observation('campaign', question(now[0], 'too-long', 'live_observation',
                                                           attempt=attempt_id, deadline=1000))
    with pytest.raises(ContractError):
        controller.issue_observation('campaign', question(now[0], 'foreign', 'live_observation',
                                                           attempt='unknown', deadline=30))
    now[0] += 31
    # A backdated operator timestamp cannot make a response received after expiry timely.
    received = controller.respond_observation('session', 'live-one', 'command-1',
                                               canonical(answer(now[0] - 31, 'live-one')))
    assert received['late']
    newer = question(now[0], 'live-two', 'live_observation', attempt=attempt_id, deadline=30)
    controller.issue_observation('campaign', newer)
    listed = controller.list_observations('session')['items']
    assert [item['state'] for item in listed] == ['answered_late', 'pending']
    assert listed[1]['response'] is None
    assert controller.status('campaign')['attempts'][0]['deadline'] == physical_deadline


def test_post_test_interpretation_can_follow_physical_deadline(lab):
    controller, now = lab
    controller.resume('campaign')
    claim = controller.claim('target', 'boot', 'claim')
    physical_deadline = controller.status('campaign')['attempts'][0]['deadline']
    now[0] = physical_deadline + 1
    post = question(now[0], 'post', 'post_test_interpretation', attempt=claim['attempt_id'])
    controller.issue_observation('campaign', post)
    assert controller.observation_detail('session', 'post')['request']['attempt_id'] == claim['attempt_id']
    assert controller.status('campaign')['attempts'][0]['deadline'] == physical_deadline


def test_response_replay_alias_and_conflict(lab):
    controller, now = lab
    controller.issue_observation('campaign', question(now[0], 'one', 'pre_test_readiness'))
    controller.issue_observation('campaign', question(now[0], 'two', 'post_test_interpretation'))
    raw = canonical(answer(now[0], 'one'))
    first = controller.respond_observation('session', 'one', 'command-1', raw)
    assert controller.respond_observation('session', 'one', 'command-1', raw) == first
    assert controller.respond_observation('session', 'one', 'command-2', raw) == first
    with pytest.raises(Conflict):
        controller.respond_observation('session', 'one', 'command-1', canonical(answer(now[0], 'one', 'uncertain')))
    with pytest.raises(Conflict):
        controller.respond_observation('session', 'one', 'command-3', canonical(answer(now[0], 'one', 'uncertain')))
    with pytest.raises(Conflict):
        controller.respond_observation('session', 'two', 'command-2', canonical(answer(now[0], 'two')))
    with pytest.raises(ContractError):
        controller.respond_observation('session', 'two', 'command-4', raw)
    assert controller.list_observations('session')['items'][1]['response'] is None


def test_response_command_ids_share_operation_namespace_and_answer_cannot_predate_request(lab):
    controller, now = lab
    controller.issue_observation('campaign', question(now[0], 'readiness', 'pre_test_readiness'))
    controller.admit_operation('operation-command', 'source_capture', {})
    with pytest.raises(Conflict):
        controller.respond_observation('session', 'readiness', 'operation-command',
                                       canonical(answer(now[0], 'readiness')))
    with pytest.raises(ContractError):
        controller.respond_observation('session', 'readiness', 'human-command',
                                       canonical(answer(now[0] - 1, 'readiness')))
    controller.respond_observation('session', 'readiness', 'human-command',
                                   canonical(answer(now[0], 'readiness')))
    with pytest.raises(Conflict):
        controller.admit_operation('human-command', 'source_capture', {})


def test_cli_missing_input_and_monitor_client(lab, tmp_path, capsys):
    controller, now = lab
    controller.issue_observation('campaign', question(now[0], 'readiness', 'pre_test_readiness'))
    state = str(controller.root)
    assert main(['--state', state, '--reserve-gib', '0', 'session', 'respond', 'session',
                 '--request', 'readiness', '--file', str(tmp_path / 'missing'), '--request-id', 'command']) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'INVALID_INPUT'
    response_file = tmp_path / 'response.json'
    response_file.write_bytes(canonical(answer(now[0], 'readiness')))
    assert main(['--state', state, '--reserve-gib', '0', 'session', 'respond', 'session',
                 '--request', 'readiness', '--file', str(response_file), '--request-id', 'command']) == 0
    assert json.loads(capsys.readouterr().out)['ok']
    assert main(['--state', state, '--reserve-gib', '0', 'session', 'observations', 'session', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['data']['items'][0]['state'] == 'answered'
    controller.issue_observation('campaign', question(now[0], 'post', 'post_test_interpretation'))
    assert main(['--state', state, '--reserve-gib', '0', 'watch', 'campaign', '--session', 'session', '--once']) == 0
    assert 'Human request post' in capsys.readouterr().out
    assert 'Human request post' in render({**controller.monitor('campaign'),
                                            'observations': controller.list_observations('session')})


def test_observation_query_cursor_and_overdue_are_bounded(lab):
    controller, now = lab
    for number in range(3):
        controller.issue_observation('campaign', question(now[0], f'request-{number}', 'pre_test_readiness'))
    first = controller.list_observations('session', limit=2)
    assert len(first['items']) == 2 and first['next_cursor'] is not None
    second = controller.list_observations('session', after=first['next_cursor'], limit=2)
    assert len(second['items']) == 1 and second['next_cursor'] is None
    now[0] += 61
    assert all(item['state'] == 'overdue' for item in controller.list_observations('session')['items'])
    with pytest.raises(ContractError):
        controller.list_observations('session', limit=101)


def test_large_unicode_record_has_bounded_list_and_exact_detail(lab, capsys):
    controller, now = lab
    long_prompt = '🙂' * 4096
    request = {**question(now[0], 'large', 'pre_test_readiness'), 'prompt': long_prompt}
    controller.issue_observation('campaign', request)
    long_note = '🙂' * 4096
    response = {**answer(now[0], 'large'), 'note': long_note}
    controller.respond_observation('session', 'large', 'command', canonical(response))
    listing = controller.list_observations('session')
    assert len(canonical(listing)) < 64 * 1024
    assert listing['items'][0]['truncated']
    assert controller.observation_detail('session', 'large')['request']['prompt'] == long_prompt
    assert controller.observation_detail('session', 'large')['response']['note'] == long_note
    assert main(['--state', str(controller.root), '--reserve-gib', '0', 'session', 'observation',
                 'session', '--request', 'large', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['data']['response']['note'] == long_note
