from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import typer

from tg_bot.auth import AuthService
from tg_bot.config import Settings
from tg_bot.db import Database, Task
from tg_bot.runtime import CheckinService
from tg_bot.schedule import next_run_for, schedule_from_task
from tg_bot.schemas import TaskDefinition

app = typer.Typer(help="Telegram 用户账号签到调度器")
task_app = typer.Typer(help="管理签到任务")
app.add_typer(task_app, name="task")


def resources() -> tuple[Settings, Database]:
    settings = Settings()
    settings.ensure_directories()
    database = Database(settings.database_url)
    database.create_all()
    return settings, database


@app.command()
def login(
    method: str = typer.Option("qr", "--method", help="登录方式：qr 或 phone"),
    account: str = typer.Option("default", "--account"),
):
    """登录 Telegram 用户账号并缓存 session。"""
    if method not in {"qr", "phone"}:
        raise typer.BadParameter("必须是 qr 或 phone")
    settings, database = resources()
    asyncio.run(AuthService(settings, database).login(account, method))
    typer.echo(f"账号 {account} 登录成功")


@app.command()
def logout(account: str = typer.Option("default", "--account")):
    """退出 Telegram 登录并停用账号。"""
    settings, database = resources()
    asyncio.run(AuthService(settings, database).logout(account))
    typer.echo(f"账号 {account} 已退出登录")


@task_app.command("create")
def create_task(config: Path = typer.Option(..., "--config", exists=True, readable=True)):
    """从 YAML 创建签到任务。"""
    _, database = resources()
    try:
        definition = TaskDefinition.from_yaml(config)
    except Exception as exc:
        raise typer.BadParameter(f"配置无效：{exc}") from exc
    account = database.get_account(definition.account)
    if not account:
        raise typer.BadParameter(f"账号不存在，请先登录：{definition.account}")
    if database.get_task(definition.name):
        raise typer.BadParameter(f"任务已存在：{definition.name}")
    schedule = definition.schedule
    task = Task(
        account_id=account.id,
        name=definition.name,
        target=definition.target,
        timezone=schedule.timezone,
        schedule_type=schedule.type,
        fixed_time=schedule.time,
        random_start=schedule.start,
        random_end=schedule.end,
        config_json=json.dumps(definition.model_dump(mode="json"), ensure_ascii=False),
        enabled=False,
        next_run_at=next_run_for(schedule, now=datetime.now(timezone.utc)),
    )
    database.save_task(task)
    typer.echo(f"任务已创建：{task.name} ({task.id})，当前为停用状态")


@task_app.command("list")
def list_tasks():
    """列出签到任务。"""
    _, database = resources()
    tasks = database.list_tasks()
    if not tasks:
        typer.echo("暂无任务")
        return
    for item in tasks:
        typer.echo(
            f"{item.id}  {item.name}  {'启用' if item.enabled else '停用'}  "
            f"{item.schedule_type}  next={item.next_run_at or '-'}"
        )


@task_app.command("validate")
def validate_task(task_id: str):
    """校验已保存任务。"""
    _, database = resources()
    task = database.get_task(task_id)
    if not task:
        raise typer.BadParameter("任务不存在")
    TaskDefinition.model_validate(task.config)
    typer.echo(f"任务配置有效：{task.name}")


@task_app.command("enable")
def enable_task(task_id: str):
    """启用任务并安排下一次执行。"""
    _, database = resources()
    task = database.get_task(task_id)
    if not task:
        raise typer.BadParameter("任务不存在")
    next_run = next_run_for(schedule_from_task(task))
    database.update_task(task.id, enabled=True, next_run_at=next_run)
    typer.echo(f"任务已启用，下次执行：{next_run}")


@task_app.command("disable")
def disable_task(task_id: str):
    """停用任务，不影响当前已经开始的执行。"""
    _, database = resources()
    task = database.get_task(task_id)
    if not task:
        raise typer.BadParameter("任务不存在")
    database.update_task(task.id, enabled=False)
    typer.echo(f"任务已停用：{task.name}")


@task_app.command("run")
def run_task(task_id: str):
    """立即执行一次任务。"""
    settings, database = resources()
    service = CheckinService(settings, database)

    async def execute() -> bool:
        await service.start()
        try:
            return await service.run_task(task_id)
        finally:
            service.scheduler.shutdown(wait=False)
            await service.pool.close()

    success = asyncio.run(execute())
    if not success:
        raise typer.Exit(code=1)
    typer.echo("签到执行成功")


@task_app.command("cancel")
def cancel_task(task_id: str):
    """取消正在执行的任务。"""
    settings, database = resources()
    service = CheckinService(settings, database)

    async def cancel() -> bool:
        return await service.cancel_task(task_id)

    if asyncio.run(cancel()):
        typer.echo("已发送取消请求")
    else:
        typer.echo("任务当前没有运行实例")


@task_app.command("history")
def task_history(task_id: str):
    """查看任务执行历史。"""
    _, database = resources()
    task = database.get_task(task_id)
    if not task:
        raise typer.BadParameter("任务不存在")
    for run in database.task_history(task.id):
        typer.echo(f"{run.id}  {run.status}  attempts={run.attempts}  {run.started_at}  {run.error or ''}")


@task_app.command("export")
def export_task(task_id: str, output: Path = typer.Option(..., "--output")):
    """导出任务 YAML，不包含 session 或 API 凭证。"""
    _, database = resources()
    task = database.get_task(task_id)
    if not task:
        raise typer.BadParameter("任务不存在")
    output.write_text(TaskDefinition.model_validate(task.config).to_yaml(), encoding="utf-8")
    typer.echo(f"任务已导出：{output}")


@task_app.command("import")
def import_task(config: Path = typer.Option(..., "--config", exists=True, readable=True)):
    """从 YAML 导入任务。"""
    create_task(config)


@app.command()
def serve():
    """启动常驻调度服务。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings, database = resources()
    asyncio.run(CheckinService(settings, database).run_forever())
