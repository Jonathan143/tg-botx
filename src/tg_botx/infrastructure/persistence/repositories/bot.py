from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tg_botx.infrastructure.persistence.models import (
    PERMANENT_EXPIRY,
    BotAuditLog,
    BotBinding,
    BotBindingBatch,
    BotBindingCode,
    BotCommandConfig,
    BotSetting,
    BotUserPoint,
    utc_now,
)


class BotRepository:
    def __init__(self, session_factory):
        self.session = session_factory

    @staticmethod
    def _begin_sqlite_write(session) -> None:
        """Acquire SQLite's database-wide write lock before a read/modify/write flow.

        SQLite does not implement ``SELECT ... FOR UPDATE``.  Starting an
        IMMEDIATE transaction prevents two bot requests from both reading the
        same uncommitted state and then racing during the subsequent update.
        PostgreSQL callers rely on row-level locks instead.
        """

        bind = session.get_bind()
        if bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    def create_bot_binding_code(
        self, code_hash: str, code_hint: str, expires_at: datetime | None, role: str = "user"
    ) -> BotBindingCode:
        with self.session() as session:
            stored_expiry = expires_at
            if (
                stored_expiry is None
                and session.bind is not None
                and session.bind.dialect.name == "sqlite"
            ):
                stored_expiry = PERMANENT_EXPIRY
            item = BotBindingCode(
                code_hash=code_hash, code_hint=code_hint, expires_at=stored_expiry, role=role
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def create_bot_binding_codes(
        self,
        items: list[tuple[str, str, datetime | None, str]],
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        ttl_days: int | None = None,
    ) -> tuple[BotBindingBatch | None, list[BotBindingCode]]:
        with self.session() as session:
            if idempotency_key:
                existing = session.scalar(
                    select(BotBindingBatch).where(
                        BotBindingBatch.idempotency_key == idempotency_key
                    )
                )
                if existing:
                    if existing.request_hash != request_hash:
                        raise ValueError("IDEMPOTENCY_CONFLICT")
                    ids = json.loads(existing.code_ids_json)
                    codes = list(
                        session.scalars(select(BotBindingCode).where(BotBindingCode.id.in_(ids)))
                    )
                    return existing, sorted(codes, key=lambda item: ids.index(item.id))
            codes = []
            for code_hash, hint, expires_at, role in items:
                # Legacy SQLite schemas declared expires_at NOT NULL; use a
                # far-future sentinel there while exposing permanent as null
                # at the API boundary.
                stored_expiry = expires_at
                if (
                    stored_expiry is None
                    and session.bind is not None
                    and session.bind.dialect.name == "sqlite"
                ):
                    stored_expiry = PERMANENT_EXPIRY
                item = BotBindingCode(
                    code_hash=code_hash, code_hint=hint, expires_at=stored_expiry, role=role
                )
                session.add(item)
                codes.append(item)
            session.flush()
            batch = None
            if idempotency_key:
                batch = BotBindingBatch(
                    idempotency_key=idempotency_key,
                    request_hash=request_hash or "",
                    role=items[0][3] if items else "user",
                    quantity=len(items),
                    ttl_days=ttl_days,
                    code_ids_json=json.dumps([item.id for item in codes]),
                )
                session.add(batch)
            session.commit()
            for item in codes:
                session.refresh(item)
            if batch:
                session.refresh(batch)
            return batch, codes

    def get_bot_binding_batch(self, idempotency_key: str) -> BotBindingBatch | None:
        with self.session() as session:
            return session.scalar(
                select(BotBindingBatch).where(BotBindingBatch.idempotency_key == idempotency_key)
            )

    def get_bot_binding_code(self, code_id: str) -> BotBindingCode | None:
        with self.session() as session:
            return session.get(BotBindingCode, code_id)

    def list_bot_binding_codes(self) -> list[BotBindingCode]:
        with self.session() as session:
            return list(
                session.scalars(select(BotBindingCode).order_by(BotBindingCode.created_at.desc()))
            )

    def list_bot_binding_codes_page(
        self, *, page: int, page_size: int
    ) -> tuple[list[BotBindingCode], int]:
        with self.session() as session:
            base = select(BotBindingCode).order_by(BotBindingCode.created_at.desc())
            items = list(session.scalars(base.offset((page - 1) * page_size).limit(page_size)))
            total = session.scalar(select(func.count()).select_from(BotBindingCode)) or 0
            return items, total

    def revoke_bot_binding_code(self, code_id: str) -> bool:
        with self.session() as session:
            item = session.get(BotBindingCode, code_id)
            if item is None or item.used_at is not None or item.revoked_at is not None:
                return False
            item.revoked_at = utc_now()
            session.commit()
            return True

    def consume_bot_binding_code(
        self,
        code_hash: str,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> BotBinding | None:
        now = utc_now()
        with self.session() as session:
            self._begin_sqlite_write(session)
            item = session.scalar(
                select(BotBindingCode)
                .where(
                    BotBindingCode.code_hash == code_hash,
                    BotBindingCode.used_at.is_(None),
                    BotBindingCode.revoked_at.is_(None),
                    or_(BotBindingCode.expires_at.is_(None), BotBindingCode.expires_at > now),
                )
                .with_for_update()
            )
            if item is None:
                return None
            previous = session.scalar(
                select(BotBinding)
                .where(BotBinding.user_id == user_id, BotBinding.is_active.is_(True))
                .with_for_update()
            )
            if previous is not None:
                return None
            item.used_at = now
            binding = BotBinding(
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                role=item.role or "user",
                bound_at=now,
                is_active=True,
            )
            session.add(binding)
            session.commit()
            session.refresh(binding)
            return binding

    def get_bot_binding(self, user_id: int, *, active_only: bool = True) -> BotBinding | None:
        with self.session() as session:
            filters: list[Any] = [BotBinding.user_id == user_id]
            if active_only:
                filters.append(BotBinding.is_active.is_(True))
            return session.scalar(select(BotBinding).where(*filters))

    def list_bot_bindings(self, *, active_only: bool = True) -> list[BotBinding]:
        with self.session() as session:
            query = select(BotBinding).order_by(BotBinding.bound_at.desc())
            if active_only:
                query = query.where(BotBinding.is_active.is_(True))
            return list(session.scalars(query))

    def list_bot_bindings_page(
        self, *, page: int, page_size: int, active_only: bool = True
    ) -> tuple[list[BotBinding], int]:
        with self.session() as session:
            query = select(BotBinding)
            count_query = select(func.count()).select_from(BotBinding)
            if active_only:
                query = query.where(BotBinding.is_active.is_(True))
                count_query = count_query.where(BotBinding.is_active.is_(True))
            query = query.order_by(BotBinding.bound_at.desc())
            items = list(session.scalars(query.offset((page - 1) * page_size).limit(page_size)))
            total = session.scalar(count_query) or 0
            return items, total

    def revoke_bot_binding(self, binding_id: str) -> bool:
        with self.session() as session:
            item = session.get(BotBinding, binding_id)
            if item is None or not item.is_active:
                return False
            item.is_active = False
            item.unbound_at = utc_now()
            session.commit()
            return True

    def get_bot_user_points(self, user_id: int) -> BotUserPoint | None:
        with self.session() as session:
            return session.get(BotUserPoint, user_id)

    def checkin_bot_user(
        self,
        user_id: int,
        chat_id: int,
        amount_min: int,
        amount_max: int,
    ) -> tuple[str, int, int]:
        """Award one random daily check-in amount to an active binding.

        The returned status is ``not_bound``, ``already`` or ``success``;
        callers can turn it into user-facing text without exposing database
        details.  The binding check and point update happen in one transaction.
        """

        if amount_min < 1 or amount_max < amount_min:
            raise ValueError("积分随机范围无效")

        import secrets

        now = utc_now()
        today = now.date()
        with self.session() as session:
            self._begin_sqlite_write(session)
            binding = session.scalar(
                select(BotBinding).where(
                    BotBinding.user_id == user_id,
                    BotBinding.chat_id == chat_id,
                    BotBinding.is_active.is_(True),
                )
            )
            if binding is None:
                return "not_bound", 0, 0
            row = session.scalar(
                select(BotUserPoint).where(BotUserPoint.user_id == user_id).with_for_update()
            )
            if row is None:
                values = {"user_id": user_id, "points": 0}
                dialect = session.get_bind().dialect.name
                if dialect == "sqlite":
                    session.execute(
                        sqlite_insert(BotUserPoint)
                        .values(**values)
                        .on_conflict_do_nothing(index_elements=[BotUserPoint.user_id])
                    )
                elif dialect == "postgresql":
                    session.execute(
                        postgresql_insert(BotUserPoint)
                        .values(**values)
                        .on_conflict_do_nothing(index_elements=[BotUserPoint.user_id])
                    )
                else:
                    session.add(BotUserPoint(**values))
                session.flush()
                row = session.scalar(
                    select(BotUserPoint).where(BotUserPoint.user_id == user_id).with_for_update()
                )
                if row is None:
                    raise RuntimeError("积分记录初始化失败")
            if row.last_checkin_date == today:
                return "already", 0, row.points
            amount = secrets.randbelow(amount_max - amount_min + 1) + amount_min
            row.points += amount
            row.last_checkin_date = today
            row.updated_at = now
            session.commit()
            return "success", amount, row.points

    def get_bot_setting(self, key: str) -> str | None:
        with self.session() as session:
            item = session.get(BotSetting, key)
            return item.value if item is not None else None

    def set_bot_setting(self, key: str, value: str) -> BotSetting:
        with self.session() as session:
            item = session.get(BotSetting, key)
            if item is None:
                item = BotSetting(key=key, value=value)
                session.add(item)
            else:
                item.value = value
            item.updated_at = utc_now()
            session.commit()
            session.refresh(item)
            return item

    def list_bot_command_configs(self) -> list[BotCommandConfig]:
        with self.session() as session:
            return list(
                session.scalars(select(BotCommandConfig).order_by(BotCommandConfig.command))
            )

    def upsert_bot_command_config(
        self,
        command: str,
        description: str,
        enabled: bool,
        allowed_roles_json: str | None = None,
        *,
        menu_visible: bool | None = None,
        command_type: str | None = None,
        executor_type: str | None = None,
        executor_config_json: str | None = None,
    ) -> BotCommandConfig:
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                item = BotCommandConfig(command=command)
                session.add(item)
            item.description = description
            item.enabled = enabled
            if menu_visible is not None:
                item.menu_visible = menu_visible
            if allowed_roles_json is not None:
                item.allowed_roles_json = allowed_roles_json
            if command_type is not None:
                item.command_type = command_type
            if executor_type is not None:
                item.executor_type = executor_type
            if executor_config_json is not None:
                item.executor_config_json = executor_config_json
            item.updated_at = utc_now()
            session.commit()
            session.refresh(item)
            return item

    def rename_bot_command_config(self, command: str, new_command: str) -> BotCommandConfig | None:
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                return None
            item.command = new_command
            item.updated_at = utc_now()
            session.commit()
            session.refresh(item)
            return item

    def set_bot_command_order(self, command: str, sort_order: int) -> bool:
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                return False
            item.sort_order = sort_order
            item.updated_at = utc_now()
            session.commit()
            return True

    def delete_bot_command_config(self, command: str) -> bool:
        """Remove a persisted management-bot command configuration."""
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                return False
            session.delete(item)
            session.commit()
            return True

    def add_bot_audit_log(self, item: BotAuditLog) -> BotAuditLog:
        with self.session() as session:
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def bot_audit_logs_since(self, since: datetime) -> list[BotAuditLog]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(BotAuditLog)
                    .where(BotAuditLog.created_at >= since)
                    .order_by(BotAuditLog.created_at.desc())
                )
            )
