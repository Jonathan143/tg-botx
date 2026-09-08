from __future__ import annotations

from typing import Any

from sqlalchemy import (
    func,
    or_,
    select,
)

from tg_botx.infrastructure.persistence.models import (
    Account,
    AccountChat,
    Task,
    utc_now,
)


class AccountsRepository:
    def __init__(self, session_factory):
        self.session = session_factory

    def get_account(self, name: str = "default") -> Account | None:
        with self.session() as session:
            return session.scalar(select(Account).where(Account.name == name))

    def get_account_by_id(self, account_id: str) -> Account | None:
        with self.session() as session:
            return session.get(Account, account_id)

    def list_accounts(self) -> list[Account]:
        with self.session() as session:
            return list(session.scalars(select(Account).order_by(Account.created_at, Account.name)))

    def save_account(self, account: Account) -> Account:
        with self.session() as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return account

    def activate_account(self, name: str, phone: str | None) -> Account:
        with self.session() as session:
            account = session.scalar(select(Account).where(Account.name == name))
            if account is None:
                account = Account(name=name, session_name=name)
                session.add(account)
            account.phone = phone
            account.is_active = True
            session.commit()
            session.refresh(account)
            return account

    def deactivate_account(self, account_id: str) -> None:
        with self.session() as session:
            account = session.get(Account, account_id)
            if account is not None:
                account.is_active = False
                session.commit()

    def list_account_chats(
        self,
        account_id: str,
        *,
        chat_type: str = "all",
        query: str | None = None,
        limit: int = 200,
    ) -> list[AccountChat]:
        """Return active cached chats with optional type/text filtering."""

        limit = min(max(limit, 1), 500)
        filters: list[Any] = [
            AccountChat.account_id == account_id,
            AccountChat.is_active.is_(True),
        ]
        if chat_type != "all":
            filters.append(AccountChat.chat_type == chat_type)
        if query and (term := query.strip()):
            pattern = f"%{term}%"
            filters.append(
                or_(
                    AccountChat.chat_id.ilike(pattern),
                    AccountChat.title.ilike(pattern),
                    AccountChat.username.ilike(pattern),
                )
            )
        with self.session() as session:
            statement = (
                select(AccountChat)
                .where(*filters)
                .order_by(AccountChat.sort_order, AccountChat.title, AccountChat.chat_id)
                .limit(limit)
            )
            return list(session.scalars(statement))

    def get_account_chat(self, account_id: str, chat_id: str) -> AccountChat | None:
        """Return one cached chat row, including inactive rows."""

        with self.session() as session:
            return session.scalar(
                select(AccountChat).where(
                    AccountChat.account_id == account_id,
                    AccountChat.chat_id == chat_id,
                )
            )

    def update_account_chat_avatar(
        self, account_id: str, chat_id: str, photo_id: int | None
    ) -> None:
        """Persist the photo version observed while downloading an avatar."""

        with self.session() as session:
            row = session.scalar(
                select(AccountChat).where(
                    AccountChat.account_id == account_id,
                    AccountChat.chat_id == chat_id,
                )
            )
            if row is not None:
                row.has_avatar = photo_id is not None
                row.avatar_photo_id = photo_id
                row.updated_at = utc_now()
                session.commit()

    def upsert_account_chats(
        self,
        account_id: str,
        chats: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Incrementally persist a freshly pulled dialog snapshot.

        Existing rows are updated only when metadata changed.  Dialogs absent
        from the snapshot are marked inactive instead of being deleted, which
        keeps old task references and metadata recoverable.
        """

        with self.session() as session:
            existing_rows = list(
                session.scalars(select(AccountChat).where(AccountChat.account_id == account_id))
            )
            existing = {row.chat_id: row for row in existing_rows}
            seen: set[str] = set()
            added = 0
            updated = 0
            for sort_order, payload in enumerate(chats):
                chat_id = str(payload["chat_id"])
                if chat_id in seen:
                    continue
                seen.add(chat_id)
                values: dict[str, Any] = {
                    "chat_type": str(payload["chat_type"]),
                    "title": str(payload.get("title") or chat_id)[:255],
                    "username": payload.get("username"),
                    "has_avatar": bool(payload.get("has_avatar", False)),
                    "sort_order": sort_order,
                }
                # Keep the version captured by a newer build when an older
                # caller submits a payload that does not know this field yet.
                if "avatar_photo_id" in payload:
                    values["avatar_photo_id"] = payload.get("avatar_photo_id")
                row = existing.get(chat_id)
                if row is None:
                    session.add(
                        AccountChat(
                            account_id=account_id,
                            chat_id=chat_id,
                            is_active=True,
                            **values,
                        )
                    )
                    added += 1
                    continue
                changed = any(getattr(row, key) != value for key, value in values.items())
                if not row.is_active:
                    changed = True
                if changed:
                    for key, value in values.items():
                        setattr(row, key, value)
                    row.is_active = True
                    row.updated_at = utc_now()
                    updated += 1

            removed = 0
            for row in existing_rows:
                if row.chat_id not in seen and row.is_active:
                    row.is_active = False
                    row.updated_at = utc_now()
                    removed += 1
            session.commit()
            return {
                "added": added,
                "updated": updated,
                "removed": removed,
                "total": len(seen),
            }

    def account_task_summary(self, account_id: str) -> dict[str, int]:
        with self.session() as session:
            rows = session.execute(
                select(Task.enabled, Task.archived, func.count(Task.id))
                .where(Task.account_id == account_id)
                .group_by(Task.enabled, Task.archived)
            )
            summary = {"total": 0, "enabled": 0, "archived": 0}
            for enabled, archived, count in rows:
                value = int(count)
                summary["total"] += value
                if enabled and not archived:
                    summary["enabled"] += value
                if archived:
                    summary["archived"] += value
            return summary
