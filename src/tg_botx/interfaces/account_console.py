"""账号登录的终端适配器，共用账号服务的状态机和退出规则。"""

from __future__ import annotations

import asyncio
from getpass import getpass
from typing import Literal, cast

import qrcode

from tg_botx.config import Settings
from tg_botx.features.accounts.service import AdminAccountError, LoginFlowManager
from tg_botx.infrastructure.persistence.db import Account, Database


class AuthService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    async def login(self, account_name: str = "default", method: str = "qr") -> Account:
        if method not in {"qr", "phone"}:
            raise AdminAccountError("LOGIN_METHOD_INVALID", "登录方式仅支持二维码或手机号")
        manager = LoginFlowManager(self.settings, self.database)
        qr_path = self.settings.data_dir / "login-qr.png"
        shown_url = None
        try:
            flow = await manager.start(account_name, cast(Literal["qr", "phone"], method))
            while flow.stage != "completed":
                if flow.stage == "phone_required":
                    phone = await asyncio.to_thread(input, "请输入手机号（含国家区号）：")
                    flow = await manager.submit_phone(flow.flow_id, phone.strip())
                elif flow.stage == "code_pending":
                    code = await asyncio.to_thread(input, "请输入验证码：")
                    flow = await manager.submit_code(flow.flow_id, code.strip())
                elif flow.stage == "password_pending":
                    password = await asyncio.to_thread(getpass, "请输入二次验证密码：")
                    flow = await manager.submit_password(flow.flow_id, password)
                elif flow.stage == "failed":
                    raise AdminAccountError("LOGIN_FAILED", "登录失败，请重新开始")
                else:
                    if flow.qr_url and flow.qr_url != shown_url:
                        shown_url = flow.qr_url
                        with qr_path.open("wb") as image_file:
                            qrcode.make(shown_url).save(image_file)
                        print(f"请使用 Telegram 扫描二维码（临时文件：{qr_path}）：")
                        terminal_qr = qrcode.QRCode(border=1)
                        terminal_qr.add_data(shown_url)
                        terminal_qr.make(fit=True)
                        terminal_qr.print_ascii(invert=True)
                    await asyncio.sleep(0.5)
                    flow = await manager.get_flow(flow.flow_id)
            account = self.database.get_account_by_id(flow.account_id or "")
            if account is None:
                raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
            return account
        finally:
            await manager.close()
            if shown_url is not None:
                qr_path.unlink(missing_ok=True)

    async def logout(self, account_name: str = "default") -> None:
        manager = LoginFlowManager(self.settings, self.database)
        try:
            await manager.logout(account_name)
        finally:
            await manager.close()
