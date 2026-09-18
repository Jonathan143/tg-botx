import os
import uuid

import pytest
from sqlalchemy import text

from tg_botx.features.bot.executors.base import CommandContext
from tg_botx.features.bot.executors.policy import ExecutorPolicy
from tg_botx.features.bot.executors.registry import ExecutorRegistry
from tg_botx.features.bot.executors.schemas import origin
from tg_botx.infrastructure.persistence.db import Database


@pytest.fixture
def db(tmp_path):
    # CI can run the same transaction suite against a real PostgreSQL service.
    url = os.getenv("EXECUTOR_TEST_DATABASE_URL")
    database = Database(url or f"sqlite:///{tmp_path / 'commands.sqlite3'}")
    schema = "executor_" + uuid.uuid4().hex
    if url:
        with database.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        from sqlalchemy import event

        @event.listens_for(database.engine, "checkout")
        def set_schema(connection, record, proxy):
            old = connection.autocommit
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
            connection.autocommit = old

    database.create_all()
    try:
        yield database
    finally:
        if url:
            with database.engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        database.engine.dispose()


@pytest.fixture
def registry():
    return ExecutorRegistry(
        ExecutorPolicy(allowed_origins=frozenset({origin("https://api.example.test")}))
    )


@pytest.fixture
def context():
    return CommandContext(str(uuid.uuid4()), "hello", 'a"b&c/中文', 123, 123, "user")
