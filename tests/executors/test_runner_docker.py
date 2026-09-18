"""Real Linux Docker integration; enabled explicitly in the dedicated CI job."""

import os
from uuid import uuid4

import pytest

from tg_botx.features.bot.executors.base import ExecutionError
from tg_botx.runner.service import DockerSandbox, RunnerRequest, RunnerSettings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EXECUTOR_DOCKER_TESTS") != "1",
    reason="requires explicitly enabled Linux Docker integration",
)


@pytest.fixture
async def sandbox():
    manager = DockerSandbox(
        RunnerSettings(token="docker-ci-only-" + "x" * 32, instance="ci-" + uuid4().hex[:12])
    )
    assert await manager.probe(), "Required Docker limits/seccomp or sandbox image are unavailable"
    try:
        yield manager
    finally:
        await manager.sweep(startup=True)
        _, remaining = await manager.command(
            "ps", "-aq", "--filter", f"label=tg-botx.python-runner={manager.settings.instance}"
        )
        assert not remaining.strip(), "Sandbox container leaked after execution"


async def run(sandbox, code, seconds=3):
    return await sandbox.execute(
        RunnerRequest(
            executionId=str(uuid4()),
            code=code,
            context={"argument": "Hello"},
            timeoutSeconds=seconds,
        )
    )


async def test_real_docker_happy_path_and_os_isolation(sandbox):
    code = """import json, os, socket, pathlib

def main(ctx):
    checks = {"nonroot": os.getuid() == 65534, "secret_absent": "TG_BOT_RUNNER_TOKEN" not in os.environ, "no_socket": not pathlib.Path("/var/run/docker.sock").exists()}
    try:
        pathlib.Path("/forbidden-file").write_text("x")
        checks["readonly"] = False
    except OSError:
        checks["readonly"] = True
    connection = socket.socket()
    connection.settimeout(0.2)
    try:
        connection.connect(("1.1.1.1", 443))
        checks["no_network"] = False
    except OSError:
        checks["no_network"] = True
    finally:
        connection.close()
    return {"text": ctx["argument"].upper(), "data": checks}
"""
    result = await run(sandbox, code)
    assert result.text == "HELLO" and all(result.data.values())


@pytest.mark.parametrize(
    "code,seconds",
    [
        ("def main(ctx):\n    while True: pass\n", 1),
        ('def main(ctx):\n    while True: print("x" * 4096)\n', 3),
        (
            'def main(ctx):\n    huge = bytearray(256 * 1024 * 1024)\n    return {"text": str(len(huge))}\n',
            3,
        ),
        (
            'import subprocess, sys\ndef main(ctx):\n    subprocess.Popen([sys.executable, "-c", "import time;time.sleep(100)"])\n    return {"text":"child started"}\n',
            2,
        ),
    ],
    ids=["wall-timeout", "stdout-limit", "memory-limit", "child-process-cleanup"],
)
async def test_real_docker_failure_budgets_cleanup(sandbox, code, seconds):
    try:
        await run(sandbox, code, seconds)
    except ExecutionError as exc:
        assert exc.code in {
            "EXECUTION_TIMEOUT",
            "OUTPUT_TOO_LARGE",
            "EXECUTION_FAILED",
            "HTTP_RESPONSE_INVALID",
        }
    else:
        assert "Popen" in code, "Resource exhaustion unexpectedly succeeded"
    _, remaining = await sandbox.command(
        "ps", "-aq", "--filter", f"label=tg-botx.python-runner={sandbox.settings.instance}"
    )
    assert not remaining.strip()
