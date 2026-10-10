"""Serialize patrol side effects independently of Redis lease expiration.

This session advisory lock is held on one physical PostgreSQL connection inside
the owned monitor process. A successor cannot enter while the predecessor still
owns a database connection, even if its Redis scheduler lease expires.
"""
from __future__ import annotations

from contextlib import contextmanager
import os

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from services.patrol_supervisor import PatrolCancelled

_PATROL_ADVISORY_KEY = 0x4553505F50415452


class PatrolDatabaseOwner:
    def __init__(self, connection, assert_owned):
        self.assert_owned = assert_owned
        # Business writes use the exact physical connection holding the fence.
        # A lost fence therefore also aborts the old business transaction.
        self.session_factory = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="control_fully",
        )

    def __call__(self):
        self.assert_owned()


@contextmanager
def patrol_ownership(scheduler_ownership=None):
    from database import engine
    if engine.dialect.name != "postgresql":
        if scheduler_ownership:
            raise PatrolCancelled("Production patrol ownership requires PostgreSQL")
        from database import create_isolated_session
        class LocalOwner:
            session_factory = staticmethod(create_isolated_session)
            def __call__(self):
                return None
        yield LocalOwner()
        return
    connection = engine.connect()
    physical = connection.connection.driver_connection
    acquired = False
    client = None
    try:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                           {"key": _PATROL_ADVISORY_KEY}).scalar())
        connection.commit()
        if not acquired:
            raise PatrolCancelled("A predecessor still owns patrol side effects")
        if scheduler_ownership:
            from redis import Redis
            client = Redis.from_url(
                scheduler_ownership.get("redis_url") or os.environ.get("REDIS_URL") or os.environ.get("VALKEY_URL"),
                socket_timeout=2, socket_connect_timeout=2, retry_on_timeout=False,
            )
            token = bytes.fromhex(scheduler_ownership["owner_token_hex"])
            lock_key = scheduler_ownership["lock_key"]
        def assert_owned():
            if connection.closed or connection.invalidated:
                raise PatrolCancelled("Patrol database ownership connection was lost")
            if connection.connection.driver_connection is not physical:
                raise PatrolCancelled("Patrol ownership physical connection was replaced")
            try:
                # A lost dedicated connection must not silently reconnect and
                # leave normal business Sessions writing without their fence.
                standalone = not connection.in_transaction()
                connection.execute(text("SELECT 1"))
                if standalone:
                    connection.commit()
                if client is not None and client.get(lock_key) != token:
                    raise PatrolCancelled("Scheduler owner token was replaced or expired")
            except PatrolCancelled:
                raise
            except Exception as exc:
                raise PatrolCancelled("Patrol ownership could not be verified") from exc
        assert_owned()
        yield PatrolDatabaseOwner(connection, assert_owned)
    finally:
        try:
            if acquired and not connection.closed and not connection.invalidated:
                connection.rollback()
                released = connection.execute(text("SELECT pg_advisory_unlock(:key)"),
                                              {"key": _PATROL_ADVISORY_KEY}).scalar()
                connection.commit()
                if not released:
                    connection.invalidate()
        except BaseException:
            connection.invalidate()
        finally:
            connection.close()
            if client is not None:
                client.close()
