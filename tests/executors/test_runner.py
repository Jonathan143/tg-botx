import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tg_botx.features.bot.executors.base import ExecutionError
from tg_botx.integrations.python_runner import PythonRunnerClient, validate_runner_url
from tg_botx.runner.service import DockerSandbox, RunnerRequest, RunnerSettings, create_runner_app

TOKEN = "test-only-runner-token-" + "x" * 32
CODE = 'def main(ctx):\n    return {"text": ctx["argument"]}\n'


class FakeDocker(DockerSandbox):
    def __init__(self, settings):
        super().__init__(settings)
        self.commands = []
        self.start_error = None
        self.create_wait = None
        self.cleanup_fails = False

    async def command(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if args[0] == "info":
            return 0, json.dumps(
                {
                    "OSType": "linux",
                    "MemoryLimit": True,
                    "PidsLimit": True,
                    "CPUCfsQuota": True,
                    "SecurityOptions": ["name=seccomp,profile=builtin"],
                }
            ).encode()
        if args[0] == "image":
            return 0, ("sha256:" + "a" * 64).encode()
        if args[0] == "ps":
            return (0, b"still-exists") if self.cleanup_fails else (0, b"")
        if args[0] == "create" and self.create_wait is not None:
            await self.create_wait.wait()
        if args[0] == "start":
            if self.start_error:
                raise self.start_error
            return 0, b'{"text":"sandbox result"}'
        if args[0] == "rm" and self.cleanup_fails:
            return 1, b""
        return 0, b""


@pytest.fixture
def manager():
    return FakeDocker(RunnerSettings(token=TOKEN))


def request():
    return RunnerRequest(executionId=str(uuid4()), code=CODE, context={"argument": "x"})


@pytest.mark.parametrize(
    "url",
    [
        "http://runner.example.test",
        "http://localhost:8766",
        "https://user:pass@runner.example.test",
        "https://runner.example.test/path",
        "https://runner.example.test?token=x",
    ],
)
def test_runner_transport_rejects_cleartext_remote_or_credential_urls(url):
    with pytest.raises(ValueError):
        validate_runner_url(url)


def test_runner_requires_strong_deployment_secret_and_unix_docker():
    with pytest.raises(ValidationError):
        RunnerSettings(token="short")
    with pytest.raises(ValidationError):
        RunnerSettings(token=TOKEN, docker_host="tcp://0.0.0.0:2375")


async def test_runner_readiness_and_immutable_security_flags(manager):
    assert await manager.probe()
    body = request()
    assert (await manager.execute(body)).text == "sandbox result"
    args = next(args for args, kwargs in manager.commands if args[0] == "create")
    for key, value in [
        ("--network", "none"),
        ("--user", "65534:65534"),
        ("--memory", "128m"),
        ("--memory-swap", "128m"),
        ("--pids-limit", "16"),
        ("--cap-drop", "ALL"),
    ]:
        assert args[args.index(key) + 1] == value
    assert "--read-only" in args and "no-new-privileges:true" in args
    assert "sha256:" + "a" * 64 in args
    assert not {"--privileged", "--volume", "-v", "--env", "-e"}.intersection(args)
    assert TOKEN not in repr(args) + repr(manager.environment)
    assert manager.commands[-1][0][:2] == ("rm", "--force")
    assert not manager.active
    with pytest.raises(ExecutionError):
        await manager.execute(body)


@pytest.mark.parametrize("code", ["EXECUTION_TIMEOUT", "OUTPUT_TOO_LARGE", "EXECUTION_FAILED"])
async def test_runner_cleans_whole_workload_after_failures(manager, code):
    await manager.probe()
    manager.start_error = ExecutionError(code)
    with pytest.raises(ExecutionError) as error:
        await manager.execute(request())
    assert error.value.code == code
    assert manager.commands[-1][0][:2] == ("rm", "--force")
    assert not manager.active and not manager.slots.locked()


async def test_runner_quarantines_if_container_cannot_be_removed(manager):
    await manager.probe()
    manager.cleanup_fails = True
    with pytest.raises(ExecutionError):
        await manager.execute(request())
    assert manager.quarantined
    assert not await manager.probe()  # Engine health must not override cleanup quarantine.


async def test_cancel_during_creation_never_starts_workload(manager):
    await manager.probe()
    body = request()
    manager.create_wait = asyncio.Event()
    pending = asyncio.create_task(manager.execute(body))
    await asyncio.sleep(0)
    assert body.execution_id in manager.active
    manager.cancel_requested.add(body.execution_id)
    manager.create_wait.set()
    with pytest.raises(ExecutionError) as error:
        await pending
    assert error.value.code == "EXECUTION_INTERRUPTED"
    assert not any(args[0] == "start" for args, kwargs in manager.commands)
    assert not manager.active and not manager.cancel_requested


def test_runner_api_auth_body_limit_and_validation_dont_echo_source(manager):
    with TestClient(create_runner_app(manager.settings, sandbox=manager)) as client:
        assert client.get("/v1/health").status_code == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/v1/health", headers=headers).json()["ready"]
        secret = "never-echo-this-source"
        response = client.post("/v1/executions", headers=headers, json={"code": secret})
        assert response.status_code == 422 and secret not in response.text
        response = client.post("/v1/executions", headers=headers, content=b"x" * 32769)
        assert response.status_code == 413
        response = client.post(
            "/v1/executions", headers=headers, json=request().model_dump(by_alias=True)
        )
        assert response.status_code == 200 and response.json()["text"] == "sandbox result"


async def test_runner_client_uses_authenticated_fixed_protocol(context):
    calls = []

    async def handler(req):
        calls.append(req)
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        data = (
            {"ready": True, "isolated": True, "protocolVersion": 1}
            if req.url.path.endswith("health")
            else {"text": "result", "data": {"ok": True}}
        )
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))

    client = PythonRunnerClient(
        "http://127.0.0.1:8766", TOKEN, transport=httpx.MockTransport(handler)
    )
    try:
        assert await client.healthy()
        assert (await client.execute({"code": CODE}, context)).text == "result"
        payload = json.loads(calls[-1].content)
        assert payload["context"]["user"]["id"] == 123
        assert set(payload) == {"executionId", "code", "context", "timeoutSeconds"}
    finally:
        await client.close()


async def test_runner_client_cancellation_requests_remote_cleanup(context):
    entered, deleted = asyncio.Event(), asyncio.Event()

    async def handler(req):
        if req.method == "POST":
            entered.set()
            await asyncio.Event().wait()
        deleted.set()
        return httpx.Response(200, stream=httpx.ByteStream(b'{"cancelled":true}'))

    client = PythonRunnerClient(
        "http://127.0.0.1:8766", TOKEN, transport=httpx.MockTransport(handler)
    )
    pending = asyncio.create_task(client.execute({"code": CODE}, context))
    await asyncio.wait_for(entered.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert deleted.is_set()
    await client.close()
