from __future__ import annotations

import uvicorn

from tg_botx.runner.service import RunnerSettings, create_runner_app


def main() -> None:
    settings = RunnerSettings()
    uvicorn.run(
        create_runner_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        ssl_certfile=str(settings.tls_certfile) if settings.tls_certfile else None,
        ssl_keyfile=str(settings.tls_keyfile) if settings.tls_keyfile else None,
    )


if __name__ == "__main__":
    main()
