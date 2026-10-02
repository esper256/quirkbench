"""SQLite connection ownership must not depend on garbage-collection timing."""
import sqlite3

import pytest

from quirkbench.contracts import ContractError
from quirkbench.controller import Controller, MIGRATIONS


@pytest.fixture
def connections(monkeypatch):
    opened=[]
    original=Controller._connect
    def capture(controller):
        db=original(controller)
        opened.append(db)
        return db
    monkeypatch.setattr(Controller,'_connect',capture)
    try:yield opened
    finally:
        for db in opened:db.close()


def assert_closed(connections):
    assert connections
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError,match='closed'):
            db.execute('SELECT 1')


def test_initialization_closes_migration_connection(tmp_path,connections):
    controller=Controller(tmp_path/'state',reserve_bytes=0)
    assert_closed(connections)
    with controller.transaction() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==len(MIGRATIONS)
    assert_closed(connections)


def test_rejected_newer_database_closes_migration_connection(tmp_path,connections):
    root=tmp_path/'state';root.mkdir(mode=0o700)
    db=sqlite3.connect(root/'controller.sqlite')
    try:db.execute('PRAGMA user_version='+str(len(MIGRATIONS)+1))
    finally:db.close()
    with pytest.raises(ContractError,match='newer software'):
        Controller(root,reserve_bytes=0)
    assert_closed(connections)


@pytest.mark.parametrize('failure',[False,True],ids=['success','exception'])
def test_deployment_reference_query_closes_connection(tmp_path,connections,monkeypatch,failure):
    controller=Controller(tmp_path/'state',reserve_bytes=0)
    if failure:
        def rejected(*args):raise RuntimeError('query fixture failed')
        monkeypatch.setattr(Controller,'_deployment_rows',rejected)
        with pytest.raises(RuntimeError,match='query fixture failed'):
            controller.deployment_references()
    else:
        assert controller.deployment_references()==[]
    assert_closed(connections)


def test_failed_connection_configuration_closes_database(tmp_path,monkeypatch):
    opened=[]
    original=sqlite3.connect
    class RejectConfiguration(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql=='PRAGMA foreign_keys=ON':raise sqlite3.OperationalError('configuration fixture failed')
            return super().execute(sql,*args,**kwargs)
    def capture(*args,**kwargs):
        db=original(*args,factory=RejectConfiguration,**kwargs)
        opened.append(db)
        return db
    monkeypatch.setattr(sqlite3,'connect',capture)
    controller=object.__new__(Controller)
    controller.db_path=tmp_path/'controller.sqlite'
    try:
        with pytest.raises(sqlite3.OperationalError,match='configuration fixture failed'):
            controller._connect()
        assert_closed(opened)
    finally:
        for db in opened:db.close()
