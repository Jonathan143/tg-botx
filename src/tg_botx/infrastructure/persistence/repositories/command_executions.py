"""持久化、有界的命令队列；运行与回复交付使用不同状态。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tg_botx.features.bot.executors.base import ExecutionError
from tg_botx.features.bot.executors.policy import ExecutorPolicy
from tg_botx.infrastructure.persistence.models import (
    BotAuditLog,
    BotCommandExecution,
    BotExecutionGate,
    utc_now,
)

TERMINAL = ("succeeded", "failed", "unknown", "cancelled")


class CommandExecutionRepository:
    def __init__(self, session_factory: Callable[[], Session]):
        self.session = session_factory

    @staticmethod
    def _lock(session: Session, bot_identity: str) -> None:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        session.execute(
            insert(BotExecutionGate).values(bot_identity=bot_identity).on_conflict_do_nothing()
        )
        session.scalar(
            select(BotExecutionGate)
            .where(BotExecutionGate.bot_identity == bot_identity)
            .with_for_update()
        )

    def enqueue(
        self, item: BotCommandExecution, policy: ExecutorPolicy
    ) -> tuple[BotCommandExecution, bool]:
        now = utc_now()
        with self.session() as session:
            self._lock(session, item.bot_identity)
            existing = session.scalar(
                select(BotCommandExecution).where(
                    BotCommandExecution.bot_identity == item.bot_identity,
                    BotCommandExecution.dedupe_key == item.dedupe_key,
                )
            )
            if existing is not None:
                return existing, False
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(BotCommandExecution)
                    .where(
                        BotCommandExecution.bot_identity == item.bot_identity,
                        BotCommandExecution.status.in_(("queued", "running")),
                    )
                )
                or 0
            )
            if pending >= policy.queue_limit:
                raise ExecutionError("EXECUTION_BUSY")
            recent = (
                session.scalar(
                    select(func.count())
                    .select_from(BotCommandExecution)
                    .where(
                        BotCommandExecution.bot_identity == item.bot_identity,
                        BotCommandExecution.actor_key == item.actor_key,
                        BotCommandExecution.created_at >= now - timedelta(seconds=60),
                    )
                )
                or 0
            )
            if recent >= policy.rate_limit:
                raise ExecutionError("EXECUTION_RATE_LIMITED")
            session.add(item)
            session.commit()
            session.refresh(item)
            return item, True

    @staticmethod
    def _recover(session: Session, bot: str) -> None:
        now = utc_now()
        # A process crash can leave an external POST applied. Never requeue it.
        session.execute(
            update(BotCommandExecution)
            .where(
                BotCommandExecution.bot_identity == bot,
                BotCommandExecution.status == "running",
                BotCommandExecution.lease_until < now,
            )
            .values(
                status="unknown",
                error_code="EXECUTION_INTERRUPTED",
                finished_at=now,
                owner=None,
                lease_until=None,
            )
        )
        session.execute(
            update(BotCommandExecution)
            .where(
                BotCommandExecution.bot_identity == bot,
                BotCommandExecution.status == "queued",
                BotCommandExecution.expires_at < now,
            )
            .values(status="cancelled", error_code="QUEUE_EXPIRED", finished_at=now)
        )
        session.execute(
            update(BotCommandExecution)
            .where(
                BotCommandExecution.bot_identity == bot,
                BotCommandExecution.delivery_state == "sending",
                BotCommandExecution.lease_until < now,
            )
            .values(delivery_state="failed", owner=None, lease_until=None)
        )

    def claim(self, bot: str, owner: str, policy: ExecutorPolicy) -> BotCommandExecution | None:
        with self.session() as session:
            self._lock(session, bot)
            self._recover(session, bot)
            active = list(
                session.scalars(
                    select(BotCommandExecution).where(
                        BotCommandExecution.bot_identity == bot,
                        BotCommandExecution.status == "running",
                    )
                )
            )
            if len(active) >= policy.max_workers:
                session.commit()
                return None
            busy_users = {row.actor_key for row in active}
            python_busy = (
                sum(row.executor_type == "python" for row in active) >= policy.python_workers
            )
            candidates = session.scalars(
                select(BotCommandExecution)
                .where(
                    BotCommandExecution.bot_identity == bot,
                    BotCommandExecution.status == "queued",
                )
                .order_by(BotCommandExecution.created_at, BotCommandExecution.id)
                .limit(policy.queue_limit)
            )
            item = next(
                (
                    row
                    for row in candidates
                    if row.actor_key not in busy_users
                    and not (python_busy and row.executor_type == "python")
                ),
                None,
            )
            if item is not None:
                item.status = "running"
                item.owner = owner
                item.started_at = utc_now()
                # All executors have a <=30s wall budget; allow shutdown/cleanup overhead.
                item.lease_until = utc_now() + timedelta(seconds=90)
            session.commit()
            return item

    def complete(
        self,
        execution_id: str,
        owner: str,
        *,
        status: str,
        result_json: str | None,
        error_code: str | None,
        duration_ms: int,
    ) -> bool:
        with self.session() as session:
            item = session.get(BotCommandExecution, execution_id)
            if item is None:
                return False
            changed = session.execute(
                update(BotCommandExecution)
                .where(
                    BotCommandExecution.id == execution_id,
                    BotCommandExecution.status == "running",
                    BotCommandExecution.owner == owner,
                )
                .values(
                    status=status,
                    result_json=result_json,
                    error_code=error_code,
                    duration_ms=duration_ms,
                    finished_at=utc_now(),
                    owner=None,
                    lease_until=None,
                )
                .returning(BotCommandExecution.id)
            ).scalar_one_or_none()
            if changed:
                session.add(
                    BotAuditLog(
                        actor_user_id=item.user_id,
                        actor_chat_id=item.chat_id,
                        action="custom_command",
                        result=status,
                        update_id=item.update_id,
                        details=f"execution_id={item.id} command={item.command} code={error_code or 'OK'}",
                    )
                )
            session.commit()
            return bool(changed)

    def claim_delivery(self, bot: str, owner: str) -> BotCommandExecution | None:
        now = utc_now()
        with self.session() as session:
            self._lock(session, bot)
            self._recover(session, bot)
            item = session.scalar(
                select(BotCommandExecution)
                .where(
                    BotCommandExecution.bot_identity == bot,
                    BotCommandExecution.source == "telegram",
                    BotCommandExecution.status.in_(TERMINAL),
                    BotCommandExecution.delivery_state.in_(("pending", "failed")),
                    BotCommandExecution.delivery_attempts < 3,
                    (BotCommandExecution.delivery_after.is_(None))
                    | (BotCommandExecution.delivery_after <= now),
                )
                .order_by(BotCommandExecution.created_at)
                .limit(1)
            )
            if item is not None:
                item.delivery_state = "sending"
                item.delivery_attempts += 1
                item.owner = owner
                item.lease_until = now + timedelta(seconds=30)
            session.commit()
            return item

    def finish_delivery(self, execution_id: str, owner: str, *, success: bool) -> None:
        with self.session() as session:
            session.execute(
                update(BotCommandExecution)
                .where(
                    BotCommandExecution.id == execution_id,
                    BotCommandExecution.delivery_state == "sending",
                    BotCommandExecution.owner == owner,
                )
                .values(
                    delivery_state="sent" if success else "failed",
                    owner=None,
                    lease_until=None,
                    delivery_after=utc_now() + timedelta(seconds=5),
                )
            )
            session.commit()

    def get(self, execution_id: str, bot: str) -> BotCommandExecution | None:
        with self.session() as session:
            return session.scalar(
                select(BotCommandExecution).where(
                    BotCommandExecution.id == execution_id,
                    BotCommandExecution.bot_identity == bot,
                )
            )

    def list_recent(
        self, bot: str, *, command: str | None = None, limit: int = 50
    ) -> list[BotCommandExecution]:
        with self.session() as session:
            statement = select(BotCommandExecution).where(BotCommandExecution.bot_identity == bot)
            if command is not None:
                statement = statement.where(BotCommandExecution.command == command)
            return list(
                session.scalars(
                    statement.order_by(BotCommandExecution.created_at.desc()).limit(min(limit, 100))
                )
            )

    def latest(self, bot: str) -> dict[str, BotCommandExecution]:
        with self.session() as session:
            ranked = (
                select(
                    BotCommandExecution.id,
                    func.row_number()
                    .over(
                        partition_by=BotCommandExecution.command,
                        order_by=(
                            BotCommandExecution.created_at.desc(),
                            BotCommandExecution.id.desc(),
                        ),
                    )
                    .label("rank"),
                )
                .where(BotCommandExecution.bot_identity == bot)
                .subquery()
            )
            rows = session.scalars(
                select(BotCommandExecution)
                .join(ranked, ranked.c.id == BotCommandExecution.id)
                .where(ranked.c.rank == 1)
            )
            return {item.command: item for item in rows}

    def prune(self, bot: str, retention_days: int) -> None:
        with self.session() as session:
            session.execute(
                delete(BotCommandExecution).where(
                    BotCommandExecution.bot_identity == bot,
                    BotCommandExecution.status.in_(TERMINAL),
                    BotCommandExecution.created_at < utc_now() - timedelta(days=retention_days),
                )
            )
            session.commit()
