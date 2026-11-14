from types import SimpleNamespace

import pytest

from services.patrol_ownership import PatrolDatabaseOwner, patrol_ownership
from services.patrol_supervisor import PatrolCancelled


class Scalar:
    def __init__(self, value):
        self.value = value
    def scalar(self):
        return self.value


class FakeConnection:
    def __init__(self, acquired=True):
        self.connection = SimpleNamespace(driver_connection=object())
        self.closed = self.invalidated = self.transaction = False
        self.acquired = acquired
        self.statements = []
    def execute(self, statement, parameters=None):
        self.transaction = True
        sql = str(statement)
        self.statements.append(sql)
        return Scalar(self.acquired if "pg_try_advisory_lock" in sql else True)
    def commit(self):
        self.transaction = False
    def rollback(self):
        self.transaction = False
    def in_transaction(self):
        return self.transaction
    def invalidate(self):
        self.invalidated = True
    def close(self):
        self.closed = True


def _postgres_engine(monkeypatch, connection):
    import database
    monkeypatch.setattr(database, "engine", SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection))


def test_business_factory_binds_same_fence_connection_and_guard_leaves_no_idle_transaction(monkeypatch):
    connection = FakeConnection()
    _postgres_engine(monkeypatch, connection)
    with patrol_ownership() as owner:
        assert owner.session_factory.kw["bind"] is connection
        assert owner.session_factory.kw["join_transaction_mode"] == "control_fully"
        owner()
        assert not connection.in_transaction()
        # Existing business transactions remain controlled by their Session.
        connection.transaction = True
        owner()
        assert connection.in_transaction()
    assert connection.closed
    assert any("pg_advisory_unlock" in statement for statement in connection.statements)


def test_successor_cannot_enter_while_predecessor_still_holds_pg_lock(monkeypatch):
    connection = FakeConnection(acquired=False)
    _postgres_engine(monkeypatch, connection)
    with pytest.raises(PatrolCancelled, match="predecessor"):
        with patrol_ownership():
            pytest.fail("successor was admitted")
    assert connection.closed


def test_lost_or_replaced_physical_connection_cannot_continue_business_writes(monkeypatch):
    connection = FakeConnection()
    _postgres_engine(monkeypatch, connection)
    with patrol_ownership() as owner:
        connection.invalidated = True
        with pytest.raises(PatrolCancelled, match="connection was lost"):
            owner()
        connection.invalidated = False
        connection.connection.driver_connection = object()
        with pytest.raises(PatrolCancelled, match="physical connection was replaced"):
            owner()


def test_replaced_scheduler_token_is_rejected_without_releasing_successor(monkeypatch):
    connection = FakeConnection()
    _postgres_engine(monkeypatch, connection)
    state = {"token": b"ownerA", "closed": False}
    class Redis:
        @staticmethod
        def from_url(*args, **kwargs):
            return Redis()
        def get(self, key):
            assert key == "scheduler"
            return state["token"]
        def close(self):
            state["closed"] = True
    import redis
    monkeypatch.setattr(redis, "Redis", Redis)
    with patrol_ownership({"lock_key": "scheduler", "owner_token_hex": b"ownerA".hex(),
                           "redis_url": "redis://unused"}) as owner:
        state["token"] = b"ownerB"
        with pytest.raises(PatrolCancelled, match="replaced or expired"):
            owner()
    assert state["token"] == b"ownerB"
    assert state["closed"]


def test_bound_physical_session_commit_persists_after_session_close(client, db_session):
    import database
    from models import User
    connection = database.engine.connect()
    try:
        owner = PatrolDatabaseOwner(connection, lambda: None)
        session = owner.session_factory()
        row = User(username="same_connection_commit")
        row.set_password("testpassword")
        session.add(row)
        session.commit()
        session.close()
        # The Session's commit must commit the business transaction, rather
        # than silently returning a still-uncommitted external Connection.
        assert db_session.query(User).filter_by(username="same_connection_commit").one()
    finally:
        connection.close()
