from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from tg_botx.features.checkin.condition import ConditionVariable, RegexBudget, evaluate_rule
from tg_botx.features.checkin.executor import CheckinError, CheckinExecutor, ExecutionContext
from tg_botx.schemas import TaskDefinition


def http_step(node_id="http1", **kwargs):
    return {"type": "http_request", "node_id": node_id, "url": "https://example.test", **kwargs}


def extraction(**kwargs):
    return {
        "type": "extract_variable",
        "name": "balance",
        "source": "http_body",
        "source_node_id": "http1",
        "path": "balance",
        "value_type": "number",
        **kwargs,
    }


def condition(if_steps=None, else_steps=None):
    return {
        "type": "condition",
        "node_id": "condition1",
        "schema_version": 2,
        "extracts": [],
        "branches": [
            {
                "kind": "if",
                "conditions": [
                    {
                        "variable": "balance",
                        "value_type": "number",
                        "operator": "eq",
                        "operands": [{"source": "literal", "value": "100"}],
                    }
                ],
                "steps": if_steps or [],
            },
            {"kind": "else", "steps": else_steps or []},
        ],
    }


def validate(steps):
    return TaskDefinition.model_validate(
        {
            "name": "review",
            "target": "@review",
            "schedule": {"type": "fixed", "time": "09:00"},
            "steps": steps,
        }
    )


def context():
    return ExecutionContext(entity=None, bot_id=None, timezone=ZoneInfo("UTC"), baseline=0)


@pytest.mark.parametrize(
    "consumer",
    [
        {"type": "send_message", "text": "{{ balance }}"},
        http_step(
            "http2",
            url="https://example.test/{{ balance }}",
            headers='{"X-Balance":"{{ balance }}"}',
            body="{{ balance }}",
        ),
        condition(),
    ],
)
def test_extracted_variable_can_be_referenced(consumer):
    validate([http_step(), extraction(), consumer])


@pytest.mark.parametrize("name", ["class", "__private", "balance"])
def test_extraction_rejects_reserved_or_duplicate_names(name):
    with pytest.raises(ValidationError):
        validate([http_step(), extraction(), extraction(name=name)])


def test_variables_merge_across_exclusive_branches():
    branches = condition([extraction(name="result")], [extraction(name="result")])
    validate(
        [http_step(), extraction(), branches, {"type": "send_message", "text": "{{ result }}"}]
    )
    branches["branches"][1]["steps"][0]["value_type"] = "text"
    with pytest.raises(ValidationError, match="不同类型"):
        validate([http_step(), extraction(), branches])


@pytest.mark.parametrize(
    "steps",
    [
        [http_step("other"), extraction()],
        [extraction(), http_step()],
        [{"type": "wait_message", "node_id": "http1"}, extraction()],
        [
            http_step(),
            extraction(),
            condition([http_step("branch-http")]),
            extraction(name="result", source_node_id="branch-http"),
        ],
        [
            http_step(),
            extraction(),
            condition(
                [http_step("branch-http")],
                [extraction(name="result", source_node_id="branch-http")],
            ),
        ],
    ],
)
def test_source_must_be_a_reachable_prior_node_of_correct_type(steps):
    with pytest.raises(ValidationError, match="source_node_id"):
        validate(steps)


def test_source_is_inherited_inside_nested_branch():
    nested = condition([extraction(name="result")])
    nested["node_id"] = "nested"
    validate([http_step(), extraction(), condition([nested])])


@pytest.mark.parametrize(
    "patch",
    [
        {"pattern": "("},
        {"pattern": ""},
        {"capture_group": []},
        {"capture_group": -1},
        {"regex": {"match_mode": "invalid"}},
        {"regex": "invalid"},
    ],
)
def test_standalone_regex_is_validated_before_save(patch):
    step = {
        "type": "extract_variable",
        "name": "amount",
        "source": "wait_message_text",
        "source_node_id": "wait1",
        "mode": "regex_capture",
        "pattern": r"(\d+)",
        **patch,
    }
    with pytest.raises(ValidationError):
        validate([{"type": "wait_message", "node_id": "wait1"}, step])


@pytest.mark.parametrize(
    "patch",
    [
        {"headers": "{"},
        {"headers": '{"X":123}'},
        {"timeout_seconds": "bad"},
        {"timeout_seconds": 0},
    ],
)
def test_http_configuration_is_validated(patch):
    with pytest.raises(ValidationError):
        validate([http_step(**patch)])


async def test_http_to_extraction_to_condition_execution(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"balance": 100})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "tg_botx.features.checkin.executor.httpx.AsyncClient",
        lambda: client_type(transport=httpx.MockTransport(handler)),
    )
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=1)))
    ctx = context()
    ctx.variables["token"] = ConditionVariable("token", "text", 'secret"token', 'secret"token')
    steps = [
        http_step(headers='{"Authorization":"Bearer {{ token }}"}'),
        extraction(),
        condition([{"type": "send_message", "text": "{{ balance }}"}]),
    ]
    await CheckinExecutor(client)._execute_steps(steps, ctx, "steps")
    assert requests[0].headers["Authorization"] == 'Bearer secret"token'
    assert ctx.variables["balance"].value == Decimal("100")
    client.send_message.assert_awaited_once_with(None, "100")


@pytest.mark.parametrize(
    ("field", "value_type", "value", "operator", "expected"),
    [
        ("balance", "number", 100, "eq", "100"),
        ("balance", "number", 100, "gt", "99"),
        ("date", "datetime", "2026-09-06T12:00:00+00:00", "after", "2026-09-05T00:00:00+00:00"),
    ],
)
async def test_http_extraction_converts_typed_values(field, value_type, value, operator, expected):
    ctx = context()
    ctx.http_responses["http1"] = httpx.Response(200, json={field: value})
    await CheckinExecutor(None)._execute_steps(
        [extraction(path=field, value_type=value_type)], ctx, "steps"
    )
    assert evaluate_rule(
        {
            "variable": "balance",
            "value_type": value_type,
            "operator": operator,
            "operands": [{"source": "literal", "value": expected}],
        },
        ctx.variables,
        ctx.timezone,
        RegexBudget(),
    )


async def test_invalid_number_fails_at_extraction():
    ctx = context()
    ctx.http_responses["http1"] = httpx.Response(200, json={"balance": "not a number"})
    with pytest.raises(CheckinError, match="变量提取失败"):
        await CheckinExecutor(None)._execute_steps([extraction()], ctx, "steps")


@pytest.mark.parametrize("source", ["http_body", "wait_message_text"])
async def test_explicit_missing_source_never_falls_back(source):
    ctx = context()
    ctx.http_response = httpx.Response(200, json={"balance": 999})
    ctx.last_wait_text = "999"
    ctx.last_wait_metadata = {"message.id": 999}
    step = extraction(source=source, source_node_id="missing")
    if source == "wait_message_text":
        step.update(mode="metadata", field="message.id")
    with pytest.raises(CheckinError, match="未执行或不存在"):
        await CheckinExecutor(None)._execute_steps([step], ctx, "steps")
    assert not ctx.variables


async def test_omitted_source_keeps_legacy_latest_response_behavior():
    ctx = context()
    ctx.http_response = httpx.Response(200, json={"balance": 100})
    step = extraction()
    del step["source_node_id"]
    validate([http_step(), step])
    await CheckinExecutor(None)._execute_steps([step], ctx, "steps")
    assert ctx.variables["balance"].value == Decimal("100")


@pytest.mark.parametrize("config", [{}, {"regex": None}, {"regex": {}}])
async def test_regex_defaults_work_in_standalone_extraction(config):
    step = {
        "type": "extract_variable",
        "name": "amount",
        "source": "wait_message_text",
        "source_node_id": "wait1",
        "mode": "regex_capture",
        "pattern": r"(\d+)",
        "value_type": "number",
        **config,
    }
    validate([{"type": "wait_message", "node_id": "wait1"}, step])
    ctx = context()
    ctx.wait_messages["wait1"] = "balance 123"
    await CheckinExecutor(None)._execute_steps([step], ctx, "steps")
    assert ctx.variables["amount"].value == Decimal("123")
