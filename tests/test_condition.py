from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tg_botx.features.checkin.condition import (
    ConditionEvaluationError,
    ConditionInput,
    ConditionVariable,
    RegexBudget,
    callback_data_values,
    evaluate_rule,
    execute_regex,
    parse_datetime,
    parse_number,
    render_template,
    select_branch,
)
from tg_botx.features.checkin.executor import CheckinExecutor
from tg_botx.schemas import TaskDefinition


def condition_step(*, strict: bool = False) -> dict[str, object]:
    return {
        "type": "condition",
        "node_id": "condition-balance",
        "schema_version": 2,
        "strict": strict,
        "extracts": [
            {
                "name": "balance",
                "source": "message_text",
                "mode": "first_number",
                "value_type": "number",
            }
        ],
        "branches": [
            {
                "kind": "if",
                "name": "余额充足",
                "logic": "and",
                "conditions": [
                    {
                        "variable": "balance",
                        "value_type": "number",
                        "operator": "gt",
                        "operands": [{"source": "literal", "value": "1000"}],
                    }
                ],
                "steps": [
                    {
                        "type": "send_message",
                        "node_id": "send-rich",
                        "text": "余额 {{ balance }}",
                    }
                ],
            },
            {"kind": "else", "steps": []},
        ],
    }


def test_precise_number_parsing_and_group_validation():
    assert parse_number("-1,234.50") == Decimal("-1234.50")
    assert parse_number("+1\u202f234.5") == Decimal("1234.5")
    with pytest.raises(ConditionEvaluationError, match="千分位"):
        parse_number("12,34.5")
    with pytest.raises(ConditionEvaluationError, match="混用"):
        parse_number("1,234 567")


def test_datetime_uses_task_timezone_and_rejects_dst_ambiguity():
    shanghai = ZoneInfo("Asia/Shanghai")
    parsed = parse_datetime("2026-08-31 10:30:00", shanghai)
    assert parsed.isoformat() == "2026-08-31T10:30:00+08:00"
    assert parse_datetime("1788143400", shanghai).tzinfo == shanghai
    with pytest.raises(ConditionEvaluationError, match="歧义"):
        parse_datetime("2026-11-01 01:30:00", ZoneInfo("America/New_York"))


def test_template_uses_raw_value_and_missing_variable_fails():
    variables = {"balance": ConditionVariable("balance", "number", "1,234.50", Decimal("1234.50"))}
    assert (
        render_template(r"余额 {{ balance }}，字面量 \{{ok}}", variables)
        == "余额 1,234.50，字面量 {{ok}}"
    )
    with pytest.raises(ConditionEvaluationError, match="missing"):
        render_template("{{ missing }}", variables)


def test_callback_metadata_supports_text_and_binary_payloads():
    assert callback_data_values("checkin") == ("checkin", "Y2hlY2tpbg==")
    assert callback_data_values(b"\xff\x00") == (None, "/wA=")


def test_text_length_and_set_membership_use_normalization():
    variables = {"name": ConditionVariable("name", "text", "  A  B  ", "  A  B  ")}
    common = {
        "variable": "name",
        "value_type": "text",
        "normalization": {
            "trim": True,
            "ignore_case": True,
            "collapse_whitespace": True,
            "strip_markdown": False,
        },
    }
    assert evaluate_rule(
        {**common, "operator": "in", "operands": [{"source": "literal", "value": "a b"}]},
        variables,
        ZoneInfo("UTC"),
        RegexBudget(),
    )


def test_condition_schema_requires_prior_wait_and_accepts_default_shape():
    payload = {
        "name": "condition",
        "target": "checkin_bot",
        "schedule": {"type": "fixed", "timezone": "UTC", "time": "12:00:00"},
        "steps": [
            {"type": "wait_message", "node_id": "wait", "timeout_seconds": 60},
            condition_step(),
        ],
    }
    assert TaskDefinition.model_validate(payload).steps[1]["schema_version"] == 2
    payload["steps"] = [condition_step()]
    with pytest.raises(ValueError, match="先成功等待消息"):
        TaskDefinition.model_validate(payload)


def test_condition_schema_rejects_invalid_regex_before_persisting():
    payload = {
        "name": "invalid-regex",
        "target": "checkin_bot",
        "schedule": {"type": "fixed", "timezone": "UTC", "time": "12:00:00"},
        "steps": [
            {"type": "wait_message", "node_id": "wait", "timeout_seconds": 60},
            {
                "type": "condition",
                "node_id": "condition",
                "schema_version": 2,
                "extracts": [
                    {
                        "name": "balance",
                        "mode": "regex_capture",
                        "value_type": "number",
                        "pattern": r"余额：([\d,]+))",
                        "capture_group": 1,
                    }
                ],
                "branches": [{"kind": "else", "steps": []}],
            },
        ],
    }
    with pytest.raises(ValueError, match="unbalanced parenthesis"):
        TaskDefinition.model_validate(payload)


@pytest.mark.parametrize("name", ["class", "await", "None"])
def test_condition_schema_rejects_python_javascript_reserved_variable_names(name: str):
    payload = {
        "name": "reserved-variable",
        "target": "checkin_bot",
        "schedule": {"type": "fixed", "timezone": "UTC", "time": "12:00:00"},
        "steps": [{"type": "wait_message", "node_id": "wait", "timeout_seconds": 60}],
    }
    step = condition_step()
    step["extracts"][0]["name"] = name  # type: ignore[index]
    payload["steps"].append(step)  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="保留关键字"):
        TaskDefinition.model_validate(payload)


def test_regex_capture_accepts_balance_pattern():
    value = "你的余额：2220 积分"
    assert (
        execute_regex(r"余额：([\d,]+)", value, {}, RegexBudget(), capture_group=1)
        == "2220"
    )


def test_click_button_selector_is_exclusive_and_position_requires_both_coordinates():
    payload = {
        "name": "selectors",
        "target": "checkin_bot",
        "schedule": {"type": "fixed", "timezone": "UTC", "time": "12:00:00"},
        "steps": [{"type": "click_button", "row": 0}],
    }
    with pytest.raises(ValueError, match="同时配置"):
        TaskDefinition.model_validate(payload)
    payload["steps"] = [{"type": "click_button", "text": "签到", "callback_data": "go"}]
    with pytest.raises(ValueError, match="必须互斥"):
        TaskDefinition.model_validate(payload)


def test_strict_extraction_failure_is_promoted():
    step = condition_step(strict=True)
    variables: dict[str, ConditionVariable] = {}
    with pytest.raises(ConditionEvaluationError, match="未找到数字"):
        select_branch(
            step,
            ConditionInput("余额未知", {}, ZoneInfo("UTC")),
            variables,
        )


@pytest.mark.asyncio
async def test_executor_runs_first_selected_branch_step_and_continues_main_flow():
    sent: list[str] = []
    statuses: list[tuple[object, ...]] = []

    class Message(SimpleNamespace):
        async def get_sender(self):
            return SimpleNamespace(username="balance_bot", first_name="Balance", last_name="Bot")

    message = Message(
        id=10,
        raw_text="余额 1,234.50",
        sender_id=1,
        chat_id=1,
        date=datetime(2026, 8, 31, tzinfo=UTC),
        buttons=None,
    )

    class Client:
        async def get_entity(self, target):
            return SimpleNamespace(id=1, bot=True, username="balance_bot")

        async def get_messages(self, entity, **kwargs):
            if "ids" in kwargs:
                return message
            return []

        async def send_message(self, entity, text):
            sent.append(text)
            return SimpleNamespace(id=20 + len(sent))

    class Executor(CheckinExecutor):
        async def _wait_for_message(self, **kwargs):
            return message

    async def on_status(*args):
        statuses.append(args)

    task = SimpleNamespace(
        target="balance_bot",
        timezone="UTC",
        config={
            "steps": [
                {"type": "wait_message", "node_id": "wait", "timeout_seconds": 60},
                condition_step(),
                {"type": "send_message", "node_id": "done", "text": "/done"},
            ]
        },
    )
    result = await Executor(Client(), on_step_status=on_status).execute(task)

    assert result == "余额 1,234.50"
    assert sent == ["余额 1,234.50", "/done"]
    condition_success = next(
        item for item in statuses if item[4] == "condition-balance" and item[1] == "success"
    )
    assert condition_success[6] == {"index": 0, "kind": "if", "name": "余额充足"}
    assert any(item[4] == "send-rich" and item[1] == "success" for item in statuses)
