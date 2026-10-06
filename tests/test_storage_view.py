"""Storage queries do not acquire execution or deletion authority."""
import json
import pytest
from quirkbench import cli,maintenance,retention
from quirkbench.contracts import ContractError
from quirkbench.controller import Controller
from quirkbench.storage_view import status


def test_actual_main_is_bounded_readonly_and_pin_notes_never_loaded(tmp_path,monkeypatch,capsys):
    c=Controller(tmp_path/'state',reserve_bytes=0);object_=c.store.put(b'protected')
    with c.transaction() as db:
        for owner in ('first','second','third'):db.execute('INSERT INTO refs VALUES(?,?)',(owner,object_.sha256))
        db.execute('INSERT INTO storage_pins VALUES(?,?)',('second','private note '*100000))
    def forbidden(*_,**__):pytest.fail('query acquired mutation/cleanup authority')
    monkeypatch.setattr(Controller,'__init__',forbidden)
    monkeypatch.setattr(maintenance,'private_lock',forbidden);monkeypatch.setattr(maintenance,'prune',forbidden)
    assert cli.main(['admin', 'storage', 'show', '--json', '--limit', '2'], state_root=str(c.root))==0
    answer=json.loads(capsys.readouterr().out);assert answer['ok'] and answer['operation_id'] is None
    data=answer['data'];assert data['next_cursor']=='second'
    assert data['owners'][1]['pinned'] and data['owners'][1]['cleanup_eligible'] is None
    assert not data['cleanup_eligibility_checked'] and data['disk_budget_bytes'] is None
    assert 'maintenance prune --dry-run' in data['guidance']
    assert 'private note' not in json.dumps(answer)
    assert status(c.root,after=data['next_cursor'])['data']['owners'][0]['owner']=='third'


def test_bad_cursor_or_oversized_legacy_owner_fails_without_hide_or_cleanup(tmp_path):
    c=Controller(tmp_path/'state',reserve_bytes=0)
    with c.transaction() as db:db.execute('INSERT INTO refs VALUES(?,?)',('z'*100000,'a'*64))
    with pytest.raises(ContractError,match='budget'):status(c.root)
    with pytest.raises(ContractError):status(c.root,limit=101)
    with pytest.raises(ContractError):status(c.root,after='a'*257)


def test_storage_does_not_create_missing_state(tmp_path,capsys):
    root=tmp_path/'absent'
    assert cli.main(['admin', 'storage', 'show', '--json'], state_root=str(root))!=0
    assert not root.exists() and not json.loads(capsys.readouterr().out)['ok']
