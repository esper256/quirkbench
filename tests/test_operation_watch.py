"""Watchers consume persisted facts and cannot become execution owners."""
import io
import json
import sqlite3

import pytest

from quirkbench.cli import main
from quirkbench.contracts import ContractError
from quirkbench.controller import Controller
from quirkbench.operation_watch import watch_operation


def test_watch_once_preserves_active_worker_and_lifecycle_epoch(tmp_path, capsys):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('watch', 'image_prepare', {})
        claim = owner.claim(operation['id'], stage='recovery_rootfs', deadline=controller.clock() + 60)
        assert main(['--state', str(controller.root), '--reserve-gib', '0',
                     'operation', 'watch', operation['id'], '--once', '--json']) == 0
        assert json.loads(capsys.readouterr().out)['data'] == claim
        with sqlite3.connect(controller.root / 'controller.sqlite') as db:
            assert db.execute('SELECT epoch FROM controller_lifecycle').fetchone()[0] == owner.epoch
        assert controller.operation_status(operation['id'])['data'] == claim


def test_watch_tracks_completion_without_measuring_fake_progress(tmp_path):
    controller = Controller(tmp_path / 'state', reserve_bytes=0)
    with controller.lifecycle() as owner:
        operation = controller.admit_operation('watch', 'image_prepare', {})
        claim = owner.claim(operation['id'], stage='recovery_rootfs', deadline=controller.clock() + 60)
        waits = []
        def advance(interval):
            waits.append(interval)
            controller._publish_operation(operation['id'], owner.epoch, claim['worker_generation'],
                                          state='FAILED', error={'code': 'BUILD_FAILED',
                                                                'message': 'compiler failed', 'retryable': False})
        output = io.StringIO()
        assert watch_operation(controller, operation['id'], sleep=advance, stream=output,
                               clock=lambda: 100) == 0
        assert waits == [2]
        assert output.getvalue().count('Snapshot:') == 2
        assert 'compiler failed' in output.getvalue()
        assert 'Measured progress: unavailable' in output.getvalue()
        assert '%' not in output.getvalue()


@pytest.mark.parametrize('interval', [0, float('nan'), float('inf'), 61])
def test_watch_invalid_interval_never_reads_controller(interval):
    with pytest.raises(ContractError, match='interval'):
        watch_operation(None, 'unused', interval=interval)


def test_watch_missing_state_does_not_create_database(tmp_path, capsys):
    root = tmp_path / 'absent'
    assert main(['--state', str(root), 'operation', 'watch', 'unused', '--once']) == 2
    assert 'existing controller state' in capsys.readouterr().err
    assert not root.exists()
