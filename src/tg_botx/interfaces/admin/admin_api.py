from __future__ import annotations

import time

from fastapi import FastAPI

from tg_botx.application.container import ApplicationContext, build_admin_context
from tg_botx.config import Settings
from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
)
from tg_botx.interfaces.admin.lifecycle import build_lifespan
from tg_botx.interfaces.admin.log_reader import _read_log_entries as _read_log_entries
from tg_botx.interfaces.admin.middleware import install_middleware
from tg_botx.interfaces.admin.routes import (
    accounts as account_routes,
)
from tg_botx.interfaces.admin.routes import (
    auth,
    bot_bindings,
    bot_commands,
    bot_webhook,
    dashboard,
    logs,
    runs,
    task_events,
    task_imports,
    tasks,
)
from tg_botx.interfaces.admin.routes import (
    settings as settings_routes,
)


def create_admin_app(
    settings: Settings,
    database: Database,
    service: CheckinService,
    *,
    context: ApplicationContext | None = None,
) -> FastAPI:
    dependencies = build_admin_context(context or ApplicationContext(settings, database, service))
    admin_key, admin_origin = dependencies.admin_key, dependencies.admin_origin
    keys, sessions, limiter = dependencies.keys, dependencies.sessions, dependencies.limiter
    accounts, admin_bot = dependencies.accounts, dependencies.bot
    trusted_proxies = dependencies.trusted_proxies
    shutdown_event, log_stream = dependencies.shutdown, dependencies.logs
    started_at = time.monotonic()

    app = FastAPI(
        title="tg-bot 后台管理 API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=build_lifespan(dependencies),
    )
    app.state.context = dependencies
    app.state.transport_keys = keys
    app.state.sessions = sessions
    app.state.login_flows = accounts
    app.state.database = database
    app.state.checkin_service = service
    app.state.shutdown_event = shutdown_event
    app.state.admin_bot = admin_bot

    install_middleware(app, settings, sessions, admin_origin)
    app.include_router(
        auth.build_router(
            settings=settings,
            keys=keys,
            sessions=sessions,
            limiter=limiter,
            trusted_proxies=trusted_proxies,
            admin_key=admin_key,
        )
    )
    app.include_router(bot_webhook.build_router(admin_bot=admin_bot))
    app.include_router(
        dashboard.build_router(database=database, service=service, started_at=started_at)
    )
    app.include_router(tasks.build_router(database=database, service=service))
    app.include_router(
        task_events.build_router(database=database, service=service, shutdown_event=shutdown_event)
    )
    app.include_router(task_imports.build_router(database=database, service=service))
    app.include_router(runs.build_router(database=database, service=service))
    app.include_router(
        account_routes.build_router(
            settings=settings, database=database, keys=keys, accounts=accounts
        )
    )
    app.include_router(
        settings_routes.build_router(
            settings=settings,
            keys=keys,
            admin_bot=admin_bot,
            trusted_proxies=trusted_proxies,
            app=app,
        )
    )
    app.include_router(
        logs.build_router(settings=settings, shutdown_event=shutdown_event, log_stream=log_stream)
    )
    app.include_router(
        bot_bindings.build_router(settings=settings, database=database, admin_bot=admin_bot)
    )
    app.include_router(bot_commands.build_router(admin_bot=admin_bot))
    return app
