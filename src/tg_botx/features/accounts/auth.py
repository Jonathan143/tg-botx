from __future__ import annotations

import asyncio
from getpass import getpass
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import Account, Database
from tg_botx.integrations.telegram import TelegramAccountConfig, create_telethon_client


class AuthService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def _session_path(self, account_name: str) -> Path:
        return self.settings.sessions_dir / account_name

    def _client(self, account_name: str) -> TelegramClient:
        api_id, api_hash = self.settings.require_api_credentials()
        return create_telethon_client(
            TelegramAccountConfig(
                api_id=api_id,
                api_hash=api_hash,
                session_path=self._session_path(account_name),
            )
        )

    async def login(self, account_name: str = "default", method: str = "qr") -> Account:
        client = self._client(account_name)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
            elif method == "qr":
                me = await self._qr_login(client)
            else:
                me = await self._phone_login(client)
            phone = getattr(me, "phone", None)
            account = self.database.get_account(account_name)
            if account is None:
                account = Account(name=account_name, phone=phone, session_name=account_name)
                self.database.save_account(account)
            else:
                with self.database.session() as session:
                    stored = session.get(Account, account.id)
                    stored.phone = phone
                    stored.is_active = True
                    session.commit()
            return account
        finally:
            await client.disconnect()

    async def _phone_login(self, client: TelegramClient):
        phone = input("请输入手机号（含国家区号）：").strip()
        sent = await client.send_code_request(phone)
        code = input("请输入验证码：").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = getpass("请输入二次验证密码（输入内容不会显示，也不会保存）：")
            await client.sign_in(password=password)
        return await client.get_me()

    async def _qr_login(self, client: TelegramClient):
        login = await client.qr_login()
        qr_path = self.settings.data_dir / "login-qr.png"
        while True:
            print(f"请使用 Telegram 扫描二维码（临时文件：{qr_path}）：")
            qrcode.make(login.url).save(qr_path)
            try:
                terminal_qr = qrcode.QRCode(border=1)
                terminal_qr.add_data(login.url)
                terminal_qr.make(fit=True)
                terminal_qr.print_ascii(invert=True)
            except Exception:
                print("终端二维码输出不可用，请打开临时 PNG 文件。")
            try:
                await login.wait()
                break
            except asyncio.TimeoutError:
                print("二维码已过期，正在刷新……")
                login = await client.qr_login()
            except SessionPasswordNeededError:
                password = getpass("请输入二次验证密码（输入内容不会显示，也不会保存）：")
                await client.sign_in(password=password)
                break
        try:
            return await client.get_me()
        except SessionPasswordNeededError:
            password = getpass("请输入二次验证密码（输入内容不会显示，也不会保存）：")
            await client.sign_in(password=password)
            return await client.get_me()

    async def logout(self, account_name: str = "default") -> None:
        account = self.database.get_account(account_name)
        if not account:
            return
        client = self._client(account_name)
        await client.connect()
        try:
            if await client.is_user_authorized():
                await client.log_out()
        finally:
            await client.disconnect()
        with self.database.session() as session:
            stored = session.get(Account, account.id)
            stored.is_active = False
            session.commit()
