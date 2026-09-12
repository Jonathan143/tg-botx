from __future__ import annotations

import ast
import hashlib
import re
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from tg_botx.features.bot.executors.templates import validate_templates

FORBIDDEN_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "upgrade",
    "te",
    "trailer",
    "proxy-authorization",
    "proxy-connection",
    "accept-encoding",
}
SECRET_HEADERS = {"authorization", "cookie", "x-api-key", "api-key"}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def parsed_url(value: str) -> httpx.URL:
    if (
        len(value) > 2048
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or any(token in value for token in ("\\", "{{", "}}", "#"))
    ):
        raise ValueError("URL 不能包含空白、控制字符、模板、反斜杠或 fragment")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValueError("URL 格式无效") from exc
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or "@" in value.split("://", 1)[-1].split("/", 1)[0]
        or "%" in url.host
        or url.host.endswith(".")
        or (url.port is not None and not 1 <= url.port <= 65535)
    ):
        raise ValueError("仅允许无凭据且主机固定的 HTTP(S) URL")
    return url


def origin(value: str) -> str:
    url = parsed_url(value)
    host = f"[{url.host}]" if ":" in url.host else url.host
    port = url.port or (443 if url.scheme == "https" else 80)
    return f"{url.scheme}://{host}:{port}"


def validate_headers(headers: dict[str, str], *, credentials: bool = False) -> dict[str, str]:
    if len(headers) > 32 or len({name.casefold() for name in headers}) != len(headers):
        raise ValueError("请求头数量超限或重复")
    forbidden = FORBIDDEN_HEADERS if credentials else FORBIDDEN_HEADERS | SECRET_HEADERS
    for name, value in headers.items():
        if (
            len(name) > 100
            or not _HEADER_NAME.fullmatch(name)
            or name.casefold() in forbidden
            or len(value) > 4096
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise ValueError("请求头无效；鉴权值必须通过 credentialRef 配置")
    return headers


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)
    version: Literal[1] = 1


class HttpResponseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)
    type: Literal["text", "json"] = "text"
    text_path: str | None = Field(default=None, alias="textPath", max_length=256)

    @model_validator(mode="after")
    def check_path(self) -> Self:
        if self.type == "json" and (
            not self.text_path
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", self.text_path)
        ):
            raise ValueError("JSON 响应必须指定点分隔字段路径 textPath")
        if self.type == "text" and self.text_path is not None:
            raise ValueError("文本响应不能配置 textPath")
        return self


class HttpConfig(ConfigModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    url: str = Field(min_length=1, max_length=2048)
    query: dict[str, str] = Field(default_factory=dict, max_length=32)
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    json_body: JsonValue | None = Field(default=None, alias="jsonBody")
    text_body: str | None = Field(default=None, alias="textBody", max_length=16_384)
    credential_ref: str | None = Field(
        default=None, alias="credentialRef", pattern=r"^[a-zA-Z0-9_-]{1,64}$"
    )
    timeout_seconds: int = Field(default=10, alias="timeoutSeconds", ge=1, le=30)
    response: HttpResponseConfig = Field(default_factory=HttpResponseConfig)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed_url(value)
        return value

    @field_validator("headers")
    @classmethod
    def check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_headers(value)

    @model_validator(mode="after")
    def check_payload(self) -> Self:
        if self.json_body is not None and self.text_body is not None:
            raise ValueError("jsonBody 与 textBody 不能同时配置")
        if self.method in {"GET", "HEAD"} and (
            self.json_body is not None or self.text_body is not None
        ):
            raise ValueError("GET/HEAD 不接受请求体")
        validate_templates(self.query)
        validate_templates(self.headers)
        validate_templates(self.json_body)
        validate_templates(self.text_body)
        return self


class BuiltinConfig(ConfigModel):
    function: Literal["echo", "utc_time", "my_points", "system_status"]
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=3500)


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PythonConfig(ConfigModel):
    code: str = Field(min_length=1, max_length=24_000)
    timeout_seconds: int = Field(default=3, alias="timeoutSeconds", ge=1, le=10)

    @field_validator("code")
    @classmethod
    def check_code(cls, value: str) -> str:
        # Syntax and entry-point validation only; NOT a security sandbox.
        try:
            tree = ast.parse(value)
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ValueError("Python 脚本语法无效") from exc
        entries = [
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
        ]
        if len(entries) != 1:
            raise ValueError("脚本必须定义唯一的同步入口 main(ctx)")
        entry = entries[0]
        if (
            len(entry.args.posonlyargs) + len(entry.args.args) != 1
            or entry.args.vararg
            or entry.args.kwarg
            or entry.args.kwonlyargs
            or entry.decorator_list
        ):
            raise ValueError("入口应为无装饰器的 def main(ctx)")
        return value


def code_hash(config: dict[str, Any]) -> str | None:
    code = config.get("code")
    try:
        return hashlib.sha256(code.encode()).hexdigest() if isinstance(code, str) else None
    except UnicodeError:
        return None


CONFIG_MODELS: dict[str, type[ConfigModel]] = {
    "http": HttpConfig,
    "builtin_function": BuiltinConfig,
    "python": PythonConfig,
}
