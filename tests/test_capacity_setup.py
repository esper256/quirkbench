"""Attended capacity flow over synthetic USB inventory, never a real disk."""
from dataclasses import asdict
from io import StringIO
import json

import pytest

from quirkbench.capacity_setup import run_attended_commission
from quirkbench.commission import CommissionError
from test_commission import GUID, lab


def _identity_file(lab, tmp_path):
    path = tmp_path/'identity.json'
    path.write_text(json.dumps({'schema_version': 2, **asdict(lab.identity)}))
    path.chmod(0o600)
    return path


def _run(lab, identity, source, output):
    return run_attended_commission(
        identity_path=identity, journal=lab.journal, paths=lab.paths,
        runner=lab.run, block_rdev=lab.rdev, ram_reader=lambda: 8,
        input_stream=source, output_stream=output)


@pytest.mark.parametrize('answer', ['wrong\n', ''])
def test_cancelled_capacity_screen_does_not_authorize_storage(lab, tmp_path, answer):
    output = StringIO()
    assert not _run(lab, _identity_file(lab, tmp_path), StringIO(answer), output)
    assert 'Disk GUID: ' + GUID in output.getvalue()
    assert 'Target RAM: 8 MiB' in output.getvalue()
    assert 'Experiments sectors:' in output.getvalue()
    assert not lab.journal.exists() and lab.mutations == 0


def test_confirmed_capacity_screen_completes_journaled_layout(lab, tmp_path):
    output = StringIO()
    assert _run(lab, _identity_file(lab, tmp_path), StringIO(GUID + '\n'), output)
    record = json.loads(lab.journal.read_text())
    assert record['confirmed'] is True and record['complete'] is True
    assert len(lab.rows) == 6
    assert 'Reboot the target' in output.getvalue()


def test_advanced_sizing_is_displayed_then_journaled_and_reused(lab, tmp_path):
    identity = _identity_file(lab, tmp_path)
    output = StringIO()
    answer = StringIO('advanced\n20\n24\n' + GUID + '\n')
    assert _run(lab, identity, answer, output)
    assert 'Proposed experiments: 20 MiB; library: 24 MiB' in output.getvalue()
    record = json.loads(lab.journal.read_text())
    assert record['identity']['experiment_mib'] == 20
    assert record['identity']['library_mib'] == 24
    assert record['geometry'][4] == [49152, 98303]
    before = lab.mutations
    resumed = StringIO()
    assert not _run(lab, identity, StringIO(''), resumed)
    assert 'Commissioning is complete' in resumed.getvalue()
    assert lab.mutations == before


def test_interrupted_advanced_sizing_resumes_original_geometry(lab, tmp_path):
    identity = _identity_file(lab, tmp_path)
    lab.crash_after = 1
    with pytest.raises(RuntimeError, match='power loss'):
        _run(lab, identity, StringIO('advanced\n20\n24\n' + GUID + '\n'), StringIO())
    lab.crash_after = 0
    assert _run(lab, identity, StringIO(GUID + '\n'), StringIO())
    record = json.loads(lab.journal.read_text())
    assert record['complete'] is True
    assert record['identity']['experiment_mib'] == 20
    assert record['identity']['library_mib'] == 24


def test_advanced_sizing_cancellation_and_low_capacity_leave_media_untouched(lab, tmp_path):
    identity = _identity_file(lab, tmp_path)
    assert not _run(lab, identity, StringIO('advanced\n20\n24\n\n'), StringIO())
    assert not lab.journal.exists() and lab.mutations == 0
    with pytest.raises(CommissionError, match='insufficient evidence capacity'):
        _run(lab, identity, StringIO('advanced\n40\n40\n' + GUID + '\n'), StringIO())
    assert not lab.journal.exists() and lab.mutations == 0


@pytest.mark.parametrize('sizes', ['15\n24\n', '20\n15\n', '1048577\n24\n', 'bad\n24\n'])
def test_unsupported_advanced_sizing_never_authorizes_storage(lab, tmp_path, sizes):
    with pytest.raises(CommissionError, match='sizing'):
        _run(lab, _identity_file(lab, tmp_path), StringIO('advanced\n' + sizes), StringIO())
    assert not lab.journal.exists() and lab.mutations == 0


def test_changed_storage_while_operator_reads_requires_new_confirmation(lab, tmp_path):
    class ChangedInput:
        def readline(self):
            lab.disk_sectors += 2048
            lab.sysfs()
            return GUID + '\n'

    with pytest.raises(CommissionError, match='plan changed'):
        _run(lab, _identity_file(lab, tmp_path), ChangedInput(), StringIO())
    assert not lab.journal.exists() and lab.mutations == 0


def test_changed_ram_while_operator_reads_requires_new_confirmation(lab, tmp_path):
    current_ram = 8

    class ChangedInput:
        def readline(self):
            nonlocal current_ram
            current_ram = 100000
            return GUID + '\n'

    with pytest.raises(CommissionError, match='target RAM changed after display'):
        run_attended_commission(
            identity_path=_identity_file(lab, tmp_path), journal=lab.journal,
            paths=lab.paths, runner=lab.run, block_rdev=lab.rdev,
            ram_reader=lambda: current_ram, input_stream=ChangedInput(),
            output_stream=StringIO())
    assert not lab.journal.exists() and lab.mutations == 0


def test_changed_ram_after_confirmation_stops_before_device_commands(lab, tmp_path):
    readings = iter((8, 8, 100000))
    with pytest.raises(CommissionError, match='target RAM changed after confirmation'):
        run_attended_commission(
            identity_path=_identity_file(lab, tmp_path), journal=lab.journal,
            paths=lab.paths, runner=lab.run, block_rdev=lab.rdev,
            ram_reader=lambda: next(readings), input_stream=StringIO(GUID + '\n'),
            output_stream=StringIO())
    assert json.loads(lab.journal.read_text())['confirmed'] is True
    assert lab.mutations == 0


def test_completed_journal_with_wrong_identity_is_not_reported_as_ready(lab, tmp_path):
    identity = _identity_file(lab, tmp_path)
    assert _run(lab, identity, StringIO(GUID + '\n'), StringIO())
    record = json.loads(lab.journal.read_text())
    record['identity']['disk_guid'] = '22222222-2222-2222-2222-222222222222'
    lab.journal.write_text(json.dumps(record))
    output = StringIO()
    with pytest.raises(CommissionError, match='selected sizing changes fixed boot identity'):
        _run(lab, identity, StringIO(''), output)
    assert 'Commissioning is complete' not in output.getvalue()


def test_completed_journal_with_changed_layout_is_not_reported_as_ready(lab, tmp_path):
    identity = _identity_file(lab, tmp_path)
    assert _run(lab, identity, StringIO(GUID + '\n'), StringIO())
    lab.rows[6][1] -= 2048
    lab.sysfs()
    output = StringIO()
    with pytest.raises(CommissionError, match='existing partition differs'):
        _run(lab, identity, StringIO(''), output)
    assert 'Commissioning is complete' not in output.getvalue()
