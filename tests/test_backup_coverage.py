"""Software backup cuts; tiny injected native repositories, no qualification."""
import json
from pathlib import Path
import tarfile
import threading

import pytest
from quirkbench import backup_coverage as coverage,cli,source_workspace as workspace
from quirkbench.contracts import CapabilityReport,Checkpoint,ContractError,canonical
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.maintenance import private_lock
from test_builder_setup import Workers
from test_controller import lab
from test_controller_deployments import Repository,setup as deployment_setup
from test_source_prepare_operation import setup,dispatched
from test_source_capture import repository
from test_source_operation import worker


def completed_capture(setup,monkeypatch):
    c,original,base=setup
    with c.lifecycle() as owner:
        _,_,coordinator,_=dispatched(setup,owner,monkeypatch)
        assert coordinator.tick()['state']=='SUCCEEDED'
        source=c.root/'workspaces/kernel-one'
        (source/'driver.c').write_text('unfinished approved edit\n')
        row=workspace.handoff(c,'kernel-one','cut-source',quiesced=True,ready=lambda _:None)
        services=Workers();coordinator=JobCoordinator(owner,services);claim=coordinator.tick()
        assert worker(c,claim,monkeypatch)==0
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
    return c,source,row


def test_dirty_stopped_capture_bytes_and_missing_private_identity_restore_paused(setup,monkeypatch,tmp_path):
    c,source,operation=completed_capture(setup,monkeypatch)
    secret=c.root/'private/identity';secret.parent.mkdir(mode=0o700);secret.write_text('private fixture only')
    c.checkpoint(Checkpoint('campaign',[],{'unfinished':'checkpoint intent'}))
    backup=tmp_path/'backup';c.backup(backup,coverage=True)
    report=coverage.verify_if_present(backup);item=report['source_workspaces'][0]
    assert item['capture_operation_id']==operation['operation_id']
    assert item['base_oid']==setup[2] and item['current_source_covered'] and item['captured_source_complete']
    assert item['captured_dirty_state']=='modified' and report['contents']['checkpoint_count']==1
    assert not report['contents']['whole_session_complete']
    assert not (backup/'private').exists() and not (backup/'workspaces').exists()
    assert 'private-identity' in report['restore_requirements']
    receipt=coverage.metadata(backup,item['capture_sha256'])
    with tarfile.open(backup/'artifacts/objects'/receipt['archive_sha256']) as archive:
        assert archive.extractfile('source/driver.c').read()==b'unfinished approved edit\n'
    restored=Controller.restore(backup,tmp_path/'restored',reserve_bytes=0)
    assert restored.status('campaign')['state']=='PAUSED'
    assert not (restored.root/'private').exists() and not (restored.root/'workspaces').exists()
    with pytest.raises((ContractError,OSError),match='missing|directory'):
        workspace.handoff(restored,'kernel-one','no-regrant',quiesced=True,ready=lambda _:None)


def test_editing_writer_keeps_historical_capture_but_never_claims_current_bytes(setup,monkeypatch,tmp_path):
    c,source,operation=completed_capture(setup,monkeypatch)
    workspace.release(c,'kernel-one');(source/'driver.c').write_text('not yet captured')
    backup=tmp_path/'backup';c.backup(backup,coverage=True)
    report=coverage.verify_if_present(backup);item=report['source_workspaces'][0]
    assert item['writer_state']=='EDITING' and item['capture_operation_id']==operation['operation_id']
    assert item['captured_source_complete'] and not item['current_source_covered']
    assert 'workspace-capture-incomplete' in report['limitations']
    receipt=coverage.metadata(backup,item['capture_sha256'])
    with tarfile.open(backup/'artifacts/objects'/receipt['archive_sha256']) as archive:
        assert archive.extractfile('source/driver.c').read()!=b'not yet captured'


@pytest.mark.parametrize('state',['QUEUED','INTERRUPTED','FAILED'])
def test_unfinished_preparation_or_capture_is_incomplete_not_new_writer_authority(setup,tmp_path,state):
    from test_source_prepare_operation import submit
    c,_,_=setup;row=submit(setup)
    if state!='QUEUED':
        with c.transaction() as db:db.execute('UPDATE operations SET state=? WHERE id=?',(state,row['operation_id']))
    backup=tmp_path/'backup';c.backup(backup,coverage=True)
    report=coverage.verify_if_present(backup);item=report['source_workspaces'][0]
    assert item['writer_state']=='PREPARING' and not item['current_source_covered']
    assert item['captured_dirty_state']=='unknown'
    if state!='FAILED':assert report['active_operations'][0]['state']==state
    restored=Controller.restore(backup,tmp_path/'restored',reserve_bytes=0)
    assert restored.status('campaign')['state']=='PAUSED'


def test_quiesced_interrupted_capture_has_no_usable_source_claim(setup,monkeypatch,tmp_path):
    c,source,_=completed_capture(setup,monkeypatch);workspace.release(c,'kernel-one')
    row=workspace.handoff(c,'kernel-one','interrupted-cut',quiesced=True,ready=lambda _:None)
    with c.transaction() as db:db.execute("UPDATE operations SET state='INTERRUPTED' WHERE id=?",(row['operation_id'],))
    backup=tmp_path/'backup';c.backup(backup,coverage=True)
    item=coverage.verify_if_present(backup)['source_workspaces'][0]
    assert item['writer_state']=='QUIESCED' and item['capture_state']=='INTERRUPTED'
    assert not item['captured_source_complete'] and not item['current_source_covered']


def test_offline_backlog_partial_upload_and_running_attempt_stay_unknown_and_paused(lab,tmp_path):
    c,_=lab;attempt=c.claim('target','boot','claim');c.start(attempt['attempt_id'],attempt['token'],'boot')
    raw=b'partial then remaining';identity=__import__('hashlib').sha256(raw).hexdigest()
    c.upload(attempt['attempt_id'],attempt['token'],'boot','cut-upload',0,raw[:3],identity,len(raw))
    backup=tmp_path/'backup';c.backup(backup,coverage=True);report=coverage.verify_if_present(backup)
    assert report['contents']['pending_upload_count']==1
    assert report['targets'][0]['target_only_backlog']=='unknown'
    assert report['targets'][0]['unresolved_attempt_count']==1
    assert not (backup/'artifacts/uploads').exists()
    restored=Controller.restore(backup,tmp_path/'restored',reserve_bytes=0)
    assert restored.status('campaign')['state']=='PAUSED'
    assert restored.status('campaign')['attempts'][0]['state']=='UNCERTAIN'


def test_copied_database_cut_and_shared_barrier_allow_live_target_publication(tmp_path):
    repository=Repository();c,artifact,experiment=deployment_setup(tmp_path,repository)
    c.submit('campaign',experiment);c.checkpoint(Checkpoint('campaign',[artifact.sha256]))
    export=repository.export;errors=[];published=threading.Event()
    def after_cut(refs,destination):
        def publish():
            try:
                with private_lock(c.root/'command.lock',shared=True):
                    c.register(CapabilityReport('late-target','late-boot',[],mode='simulation'))
                    c.checkpoint(Checkpoint('campaign',[artifact.sha256],{'late':True}))
                    published.set()
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=publish);thread.start();thread.join(timeout=2)
        assert not thread.is_alive() and not errors and published.is_set()
        export(refs,destination)
    repository.export=after_cut
    backup=tmp_path/'backup'
    with private_lock(c.root/'command.lock',shared=True):c.backup(backup,coverage=True)
    report=coverage.verify_if_present(backup)
    assert [t['device_id'] for t in report['targets']]==['target']
    assert report['contents']['checkpoint_count']==1
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM devices').fetchone()[0]==2


@pytest.mark.parametrize('failure',['export','coverage'])
def test_interrupted_capture_has_no_backup_success_marker(tmp_path,monkeypatch,failure):
    repository=Repository();c,_,experiment=deployment_setup(tmp_path,repository);c.submit('campaign',experiment)
    if failure=='export':repository.fail_export=True
    else:monkeypatch.setattr(coverage,'derive',lambda *_:(_ for _ in ()).throw(ContractError('interrupted coverage')))
    destination=tmp_path/'backup'
    with pytest.raises((ContractError,OSError)):c.backup(destination,coverage=True)
    assert not destination.exists()
    pending=list(tmp_path.glob('backup.pending-*'));assert len(pending)==1
    assert not (pending[0]/'manifest.json').exists()


def test_corrupt_copied_nonsource_object_never_claims_complete_backup(tmp_path):
    repository=Repository();c,artifact,experiment=deployment_setup(tmp_path,repository);c.submit('campaign',experiment)
    export=repository.export
    def corrupt_copy(refs,destination):
        export(refs,destination)
        (destination.parent/'artifacts/objects'/artifact.sha256).write_bytes(b'bad copied bytes')
    repository.export=corrupt_copy
    backup=tmp_path/'backup'
    with pytest.raises(ContractError,match='verification'):c.backup(backup,coverage=True)
    assert not backup.exists()
    assert not next(tmp_path.glob('backup.pending-*')).joinpath('manifest.json').exists()


@pytest.mark.parametrize('change',['claim','database','manifest','linked-report','source'])
def test_present_companion_and_cut_tampering_rejected_before_restore_native_actions(tmp_path,monkeypatch,change):
    repository=Repository();c,artifact,experiment=deployment_setup(tmp_path,repository);c.submit('campaign',experiment)
    backup=tmp_path/'backup';c.backup(backup,coverage=True)
    if change=='claim':
        value=json.loads((backup/coverage.NAME).read_bytes());value['contents']['checkpoint_count']+=1
        (backup/coverage.NAME).write_bytes(canonical(value))
    elif change=='database':
        import sqlite3
        with sqlite3.connect(backup/'controller.sqlite') as db:db.execute("UPDATE devices SET last_contact=last_contact+1")
    elif change=='manifest':(backup/'manifest.json').write_bytes((backup/'manifest.json').read_bytes()+b'\n')
    elif change=='linked-report':
        outside=tmp_path/'outside';(backup/coverage.NAME).rename(outside);(backup/coverage.NAME).symlink_to(outside)
    else:(backup/'artifacts/objects'/artifact.sha256).write_bytes(b'corrupt')
    monkeypatch.setattr(repository,'restore',lambda *_:pytest.fail('native restore before cut validation'))
    with pytest.raises((ContractError,OSError)):Controller.restore(backup,tmp_path/'restored',reserve_bytes=0,deployment_repository=repository)
    assert not (tmp_path/'restored').exists()


def test_legacy_positional_and_guided_aliases_preserve_restore_checks_and_show_unknown(tmp_path,capsys):
    c=Controller(tmp_path/'controller',reserve_bytes=0)
    legacy=tmp_path/'legacy';c.backup(legacy)
    assert coverage.verify_if_present(legacy) is None
    assert cli.main(['--state', str(tmp_path / 'restored'), 'admin', 'restore', '--input', str(legacy), '--reserve-gib', '0'])==0
    answer=json.loads(capsys.readouterr().out)
    assert answer['scheduling']=='paused' and answer['historical_backup_coverage']['coverage']=='unknown-legacy'
    for guided in (False,True):
        destination=tmp_path/('guided' if guided else 'positional')
        command=['--state', str(c.root), 'admin', 'backup', '--reserve-gib', '0']+(['--output'] if guided else [])+[str(destination)]
        assert cli.main(command)==0;answer=json.loads(capsys.readouterr().out)
        assert answer['backup']==str(destination) and (('coverage' in answer)==guided)
        if guided:
            import shlex
            commands=answer['next_steps'][1].split('run ',1)[1].split('; inspect ')
            capture=cli.parser().parse_args(shlex.split(commands[0])[1:])
            show=cli.parser().parse_args(shlex.split(commands[1].split(', then ',1)[0])[1:])
            assert capture.route=='investigation source capture' and capture.state==c.root
            assert show.route=='admin operation show' and show.state==c.root
        assert (coverage.verify_if_present(destination) is not None)==guided
    with pytest.raises(SystemExit):cli.parser().parse_args(['admin', 'backup', str(legacy), '--output', str(tmp_path / 'bad')])
    with pytest.raises(SystemExit):cli.parser().parse_args(['admin', 'restore'])


@pytest.mark.parametrize('sidecar',['-wal','-shm','-journal'])
def test_legacy_backup_rejects_unmanifested_journals_before_native_restoration(tmp_path,monkeypatch,sidecar):
    repository=Repository();c,_,experiment=deployment_setup(tmp_path,repository);c.submit('campaign',experiment)
    backup=tmp_path/'backup';c.backup(backup)
    assert not (backup/coverage.NAME).exists()
    (backup/('controller.sqlite'+sidecar)).write_bytes(b'not part of the stopped cut')
    monkeypatch.setattr(repository,'restore',lambda *_:pytest.fail('native action before stopped cut validation'))
    with pytest.raises(ContractError,match='journal'):
        Controller.restore(backup,tmp_path/'restored',reserve_bytes=0,deployment_repository=repository)
    assert not (tmp_path/'restored').exists()
