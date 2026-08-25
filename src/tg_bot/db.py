from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Integer, String, Text, TypeDecorator, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Store datetimes as UTC and always return timezone-aware values.

    SQLite drops the timezone component from ``DateTime(timezone=True)``
    columns.  Treating naive values as UTC on both sides keeps persisted data
    compatible while preventing naive/aware datetime comparisons in the
    scheduler.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        # SQLite has no timezone-aware datetime type, so preserve the existing
        # naive-UTC storage format.  PostgreSQL's TIMESTAMP WITH TIME ZONE
        # should receive an aware value so the server timezone cannot alter it.
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_name: Mapped[str] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    target: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    schedule_type: Mapped[str] = mapped_column(String(20))
    fixed_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    random_start: Mapped[str | None] = mapped_column(String(8), nullable=True)
    random_end: Mapped[str | None] = mapped_column(String(8), nullable=True)
    config_json: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

    @property
    def config(self) -> dict:
        return json.loads(self.config_json)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    planned_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Database:
    def __init__(self, url: str):
        # SQLAlchemy's ``postgresql://`` shorthand defaults to psycopg2.  The
        # project ships psycopg 3, so make the driver explicit for either
        # PostgreSQL URL spelling.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]

        database_url = make_url(url)
        engine_kwargs = {}
        if database_url.get_backend_name() == "sqlite":
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(url, **engine_kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.Session()

    def get_account(self, name: str = "default") -> Account | None:
        with self.session() as session:
            return session.scalar(select(Account).where(Account.name == name))

    def get_account_by_id(self, account_id: str) -> Account | None:
        with self.session() as session:
            return session.get(Account, account_id)

    def get_task(self, task_id_or_name: str) -> Task | None:
        with self.session() as session:
            task = session.get(Task, task_id_or_name)
            if task:
                return task
            return session.scalar(select(Task).where(Task.name == task_id_or_name, Task.archived.is_(False)))

    def list_tasks(self, include_archived: bool = False) -> list[Task]:
        with self.session() as session:
            query = select(Task).order_by(Task.created_at)
            if not include_archived:
                query = query.where(Task.archived.is_(False))
            return list(session.scalars(query))

    def save_account(self, account: Account) -> Account:
        with self.session() as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return account

    def save_task(self, task: Task) -> Task:
        with self.session() as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def update_task(self, task_id: str, **values) -> Task:
        with self.session() as session:
            task = session.get(Task, task_id)
            if not task:
                raise KeyError(task_id)
            for key, value in values.items():
                setattr(task, key, value)
            task.updated_at = utc_now()
            session.commit()
            session.refresh(task)
            return task

    def add_run(self, run: TaskRun) -> TaskRun:
        with self.session() as session:
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    def update_run(self, run_id: str, **values) -> TaskRun:
        with self.session() as session:
            run = session.get(TaskRun, run_id)
            if not run:
                raise KeyError(run_id)
            for key, value in values.items():
                setattr(run, key, value)
            session.commit()
            session.refresh(run)
            return run

    def task_history(self, task_id: str, limit: int = 20) -> list[TaskRun]:
        with self.session() as session:
            query = select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.started_at.desc()).limit(limit)
            return list(session.scalars(query))
