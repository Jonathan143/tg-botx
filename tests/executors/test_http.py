import gzip
import json
from unittest.mock import AsyncMock

import httpcore
import pytest
from pydantic import ValidationError

from tg_botx.features.bot.executors.base import ExecutionError
from tg_botx.features.bot.executors.http import HttpExecutor
from tg_botx.features.bot.executors.policy import ExecutorPolicy, HttpCredential
from tg_botx.features.bot.executors.schemas import HttpConfig, origin
from tg_botx.integrations.safe_http import PublicNetworkBackend, SafeHttpClient, public_ip


class Stream(httpcore.AsyncNetworkStream):
    def __init__(self, raw):
        self.raw = raw
        self.sent = bytearray()
        self.sni = None
        self.closed = False

    async def read(self, max_bytes, timeout=None):
        chunk, self.raw = self.raw[:max_bytes], self.raw[max_bytes:]
        return chunk

    async def write(self, buffer, timeout=None):
        self.sent.extend(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni = server_hostname
        assert ssl_context.check_hostname
        return self

    def get_extra_info(self, info):
        return None


def client_for(body=b"ok", *, status=200, extra=b"", addresses=None, policy=None):
    stream = Stream(
        b"HTTP/1.1 "
        + str(status).encode()
        + b" Test\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n"
        + extra
        + b"\r\n"
        + body
    )
    raw_backend = AsyncMock(spec=httpcore.AsyncNetworkBackend)
    raw_backend.connect_tcp.return_value = stream
    resolver = AsyncMock(return_value=addresses or ["93.184.216.34"])
    backend = PublicNetworkBackend(backend=raw_backend, resolver=resolver)
    policy = policy or ExecutorPolicy(
        allowed_origins=frozenset({origin("https://api.example.test")})
    )
    return SafeHttpClient(policy, backend=backend), stream, raw_backend, resolver


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "168.63.129.16",
        "198.18.0.1",
        "224.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
        "2002:a00:1::1",
        "2001:db8::1",
    ],
)
def test_reserved_networks_are_blocked(address):
    assert not public_ip(address)


@pytest.mark.parametrize(
    "config",
    [
        {"url": "file:///etc/passwd"},
        {"url": "https://user:secret@api.example.test"},
        {"url": "https://api.example.test/#fragment"},
        {"url": "https://{{ argument }}"},
        {"url": "https://api.example.test", "headers": {"Host": "elsewhere"}},
        {"url": "https://api.example.test", "headers": {"Authorization": "secret"}},
        {"url": "https://api.example.test", "headers": {"X-A": "\r\nB: c"}},
        {"url": "https://api.example.test", "followRedirects": True},
        {"url": "https://api.example.test", "timeoutSeconds": True},
        {"url": "https://api.example.test", "jsonBody": {}, "textBody": "x"},
    ],
)
def test_strict_http_schema(config):
    with pytest.raises(ValidationError):
        HttpConfig.model_validate(config)


async def test_actual_connection_pins_ip_and_preserves_tls_sni_and_json(context):
    client, stream, backend, resolver = client_for(b'{"data":{"reply":"success"}}')
    try:
        result = await HttpExecutor(client).execute(
            {
                "url": "https://api.example.test/v1",
                "method": "POST",
                "query": {"q": "{{ argument }}"},
                "jsonBody": {"text": "{{ argument }}"},
                "response": {"type": "json", "textPath": "data.reply"},
            },
            context,
        )
        assert result.text == "success"
        assert backend.connect_tcp.await_args.args[0] == "93.184.216.34"
        assert stream.sni == "api.example.test"
        assert b"Host: api.example.test" in stream.sent
        assert b"q=a%22b%26c%2F" in stream.sent
        body = bytes(stream.sent).split(b"\r\n\r\n", 1)[1]
        assert json.loads(body)["text"] == context.argument
        assert stream.closed and resolver.await_count == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::ffff:127.0.0.1"]]
)
async def test_mixed_dns_response_blocks_before_socket(addresses, context):
    client, stream, backend, resolver = client_for(addresses=addresses)
    try:
        with pytest.raises(ExecutionError) as error:
            await HttpExecutor(client).execute({"url": "https://api.example.test"}, context)
        assert error.value.code == "HTTP_TARGET_BLOCKED"
        backend.connect_tcp.assert_not_awaited()
    finally:
        await client.close()


async def test_dns_rebinding_cannot_reuse_a_previously_allowed_resolution(context):
    client, stream, backend, resolver = client_for()
    resolver.side_effect = [["93.184.216.34"], ["127.0.0.1"]]
    try:
        await HttpExecutor(client).execute({"url": "https://api.example.test"}, context)
        with pytest.raises(ExecutionError):
            await HttpExecutor(client).execute({"url": "https://api.example.test"}, context)
        assert backend.connect_tcp.await_count == 1
    finally:
        await client.close()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 500])
async def test_no_redirect_or_retry(status, context):
    client, stream, backend, resolver = client_for(
        status=status, extra=b"Location: http://127.0.0.1/\r\n"
    )
    try:
        with pytest.raises(ExecutionError):
            await HttpExecutor(client).execute({"url": "https://api.example.test"}, context)
        assert backend.connect_tcp.await_count == 1 and stream.closed
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body,extra,code",
    [
        (b"x" * 65537, b"", "OUTPUT_TOO_LARGE"),
        (gzip.compress(b"x" * 1000000), b"Content-Encoding: gzip\r\n", "OUTPUT_TOO_LARGE"),
        (b"bad", b"Content-Encoding: gzip\r\n", "HTTP_RESPONSE_INVALID"),
        (b"bad", b"Content-Encoding: br\r\n", "HTTP_RESPONSE_INVALID"),
    ],
    ids=["raw-limit", "gzip-bomb", "invalid-gzip", "unsupported-encoding"],
)
async def test_response_raw_and_decoded_limits(body, extra, code, context):
    client, stream, backend, resolver = client_for(body, extra=extra)
    try:
        with pytest.raises(ExecutionError) as error:
            await HttpExecutor(client).execute({"url": "https://api.example.test"}, context)
        assert error.value.code == code and stream.closed
    finally:
        await client.close()


async def test_credentials_bound_to_origin_and_never_returned(context):
    credential = HttpCredential(
        origin="https://api.example.test", headers={"Authorization": "Bearer fake-test-secret"}
    )
    policy = ExecutorPolicy(
        allowed_origins=frozenset({origin("https://api.example.test")}),
        credentials={"test": credential},
    )
    client, stream, backend, resolver = client_for(policy=policy)
    try:
        result = await HttpExecutor(client).execute(
            {"url": "https://api.example.test", "credentialRef": "test"}, context
        )
        assert b"Authorization: Bearer fake-test-secret" in stream.sent
        assert "fake-test-secret" not in repr(policy) + repr(credential) + result.text
        with pytest.raises(ExecutionError):
            await HttpExecutor(client).execute(
                {"url": "https://api.example.test", "credentialRef": "missing"}, context
            )
    finally:
        await client.close()
