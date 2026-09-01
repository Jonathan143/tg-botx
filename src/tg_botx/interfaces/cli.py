from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import typer

from tg_botx.config import Settings
from tg_botx.core.time import utc_isoformat
from tg_botx.features.accounts.auth import AuthService
from tg_botx.features.admin_bot import BotManagementService
from tg_botx.features.checkin.runtime import CheckinService, TaskStateError
from tg_botx.features.checkin.schedule import next_run_for, schedule_from_task
from tg_botx.infrastructure.observability.logging import IconFormatter, SensitiveDataFilter
from tg_botx.infrastructure.persistence.db import Database, Task
from tg_botx.schemas import TaskDefinition

app = typer.Typer(help="Telegram 用户账号签到调度器")
task_app = typer.Typer(help="管理签到任务")
app.add_typer(task_app, name="task")
bot_app = typer.Typer(help="管理 Telegram 管理 Bot")
binding_app = typer.Typer(help="管理 Telegram Bot 绑定")
bot_app.add_typer(binding_app, name="binding")
app.add_typer(bot_app, name="bot")
logger = logging.getLogger(__name__)
SERVER_APP_FACTORY = "tg_botx.interfaces.cli:create_server_app"


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    formatter = IconFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    sensitive_values = [settings.api_hash or "", settings.database_url_override or ""]
    if settings.admin_key:
        sensitive_values.append(settings.admin_key.get_secret_value())
    if settings.notification_bot_token:
        sensitive_values.append(settings.notification_bot_token.get_secret_value())
    if settings.admin_bot_token:
        sensitive_values.append(settings.admin_bot_token.get_secret_value())
    sensitive_filter = SensitiveDataFilter(sensitive_values)
    file_handler.addFilter(sensitive_filter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)
    logging.basicConfig(level=level, handlers=[console_handler, file_handler], force=True)


def resources(*, create_schema: bool = True) -> tuple[Settings, Database]:
    settings = Settings()
    settings.ensure_directories()
    configure_logging(settings)
    database = Database(settings.database_url)
    if create_schema:
        database.create_all()
    return settings, database


def create_server_app():
    """Create a fresh admin application for the Uvicorn worker process."""
    settings, database = resources()
    settings.require_admin_config()

    from tg_botx.interfaces.admin.admin_api import create_admin_app

    service = CheckinService(settings, database)
    return create_admin_app(settings, database, service)


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


@binding_app.command("create")
def create_bot_binding() -> None:
    """生成一个一次性 Telegram 管理 Bot 绑定码。"""
    settings, database = resources()
    service = BotManagementService(database, CheckinService(settings, database))
    code, item = service.create_binding_code()
    typer.echo(f"绑定码：{code}")
    typer.echo(f"有效期至：{item.expires_at.isoformat()}")


@binding_app.command("list")
def list_bot_bindings() -> None:
    """查看绑定码和当前已绑定的 Telegram 用户。"""
    settings, database = resources()
    service = BotManagementService(database, CheckinService(settings, database))
    codes = service.binding_codes()
    bindings = service.bindings()
    if codes:
        typer.echo("绑定码：")
        for item in codes:
            typer.echo(
                f"  {item.id}  *{item.hint}  {item.status}  expires={item.expires_at.isoformat()}"
            )
    else:
        typer.echo("暂无绑定码")
    if bindings:
        typer.echo("已绑定用户：")
        for binding in bindings:
            name = binding.username or binding.first_name or str(binding.user_id)
            typer.echo(
                f"  {binding.id}  {name}  user_id={binding.user_id}  bound={binding.bound_at.isoformat()}"
            )
    else:
        typer.echo("暂无已绑定用户")


@binding_app.command("revoke")
def revoke_bot_binding(binding_id: str) -> None:
    """撤销绑定码或已绑定用户。"""
    settings, database = resources()
    service = BotManagementService(database, CheckinService(settings, database))
    if service.revoke_code(binding_id) or service.revoke_binding(binding_id):
        typer.echo("绑定已撤销")
        return
    raise typer.BadParameter("绑定码或绑定关系不存在，或已经失效")


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
    if schedule.start_date is None:
        schedule = schedule.model_copy(update={"start_date": datetime.now(ZoneInfo(schedule.timezone)).date()})
        definition = definition.model_copy(update={"schedule": schedule})
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
    logger.info("创建任务 task_id=%s name=%s enabled=%s", task.id, task.name, task.enabled)
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
        schedule = schedule_from_task(item)
        frequency = schedule.frequency
        if frequency == "every_n_days":
            frequency_label = f"每{schedule.interval_days}天"
        elif frequency == "weekly":
            frequency_label = "每周" + "/".join(str(day) for day in (schedule.weekdays or []))
        elif frequency == "monthly_dates":
            frequency_label = "每月" + "/".join(str(day) for day in (schedule.month_days or [])) + "号"
        else:
            frequency_label = "每天"
        typer.echo(
            f"{item.id}  {item.name}  {'启用' if item.enabled else '停用'}  "
            f"{item.schedule_type} {frequency_label}  next={utc_isoformat(item.next_run_at) or '-'}"
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
    if database.get_latest_workflow_version(task.id) is None:
        raise typer.BadParameter("请先发布工作流后再启用任务")
    try:
        next_run = next_run_for(schedule_from_task(task))
    except ValueError as exc:
        raise typer.BadParameter("调度规则没有可执行的未来时间") from exc
    database.update_task(task.id, enabled=True, next_run_at=next_run)
    logger.info(
        "启用任务 task_id=%s name=%s next_run_at=%s",
        task.id,
        task.name,
        utc_isoformat(next_run),
    )
    typer.echo(f"任务已启用，下次执行：{utc_isoformat(next_run)}")


@task_app.command("publish")
def publish_task(
    task_id: str,
    release_note: str | None = typer.Option(None, "--note", help="可选的发布说明"),
):
    """发布当前任务的 main 工作流。"""
    settings, database = resources()
    service = CheckinService(settings, database)
    try:
        version = service.publish_task(task_id, release_note)
    except TaskStateError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        asyncio.run(service.close())
    typer.echo(f"工作流已发布：v{version.version_number}")


@task_app.command("disable")
def disable_task(task_id: str):
    """停用任务，不影响当前已经开始的执行。"""
    _, database = resources()
    task = database.get_task(task_id)
    if not task:
        raise typer.BadParameter("任务不存在")
    database.update_task(task.id, enabled=False)
    logger.info("停用任务 task_id=%s name=%s", task.id, task.name)
    typer.echo(f"任务已停用：{task.name}")


@task_app.command("run")
def run_task(task_id: str):
    """立即执行一次任务。"""
    settings, database = resources()
    logger.info("手动执行任务 task_id=%s", task_id)
    service = CheckinService(settings, database)

    async def execute() -> bool:
        await service.start()
        try:
            return await service.run_task(task_id)
        finally:
            await service.close()

    success = asyncio.run(execute())
    logger.info("手动执行任务结束 task_id=%s success=%s", task_id, success)
    if not success:
        raise typer.Exit(code=1)
    typer.echo("签到执行成功")


@task_app.command("cancel")
def cancel_task(task_id: str):
    """取消正在执行的任务。"""
    settings, database = resources()
    service = CheckinService(settings, database)

    async def cancel() -> bool:
        try:
            return await service.cancel_task(task_id)
        finally:
            await service.close()

    if asyncio.run(cancel()):
        logger.info("取消任务 task_id=%s", task_id)
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
        typer.echo(
            f"{run.id}  {run.status}  attempts={run.attempts}  {run.started_at}  {run.error or ''}"
        )


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
def serve(
    reload: bool = typer.Option(
        False,
        "--reload",
        help="监控 Python 文件变化并自动重启服务（仅用于开发环境）",
    ),
):
    """启动常驻调度服务和后台管理 API。"""
    # The parent process needs these values before Uvicorn imports the factory.
    # The factory repeats validation and initializes process-local resources in
    # every worker, including workers restarted by the development reloader.
    settings = Settings()
    settings.require_admin_config()

    import uvicorn

    uvicorn.run(
        SERVER_APP_FACTORY,
        factory=True,
        reload=reload,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        # Query strings may contain administrator-selected log filters.  Keep
        # Uvicorn access logging disabled so request URLs cannot bypass the
        # application-wide sensitive-data redaction filter.
        access_log=False,
        # SSE endpoints intentionally keep connections open.  Do not let one
        # abandoned browser connection block process shutdown forever.
        timeout_graceful_shutdown=5,
    )
