import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, select

from tg_botx.features.bot.commands import BotCommandService
from tg_botx.features.bot.executors.base import ExecutionError, ExecutionResult, json_bytes
from tg_botx.features.bot.executors.policy import ExecutorPolicy
from tg_botx.features.bot.executors.registry import ExecutorRegistry
from tg_botx.features.bot.executors.schemas import code_hash
from tg_botx.features.bot.executors.templates import render, validate_templates
from tg_botx.features.bot.models import (
    BotCommandConflictError,
    BotCommandForbiddenError,
    BotCommandValidationError,
)
from tg_botx.infrastructure.persistence.models import BotCommandConfig, SchemaVersion

ECHO = {"function": "echo", "arguments": {"text": "{{ argument }}"}}
CODE = {"code": 'def main(ctx):\n    return {"text": ctx["argument"]}\n'}


def create(service, name="custom", **kwargs):
    return service.create_command_config(
        name, "示例", executor_type="builtin_function", executor_config=ECHO, **kwargs
    )


def test_remove_js_none_draft_and_system_commands(db, registry):
    service = BotCommandService(db, registry)
    with pytest.raises(BotCommandValidationError):
        service.create_command_config("js", "旧脚本", executor_type="javascript")
    with pytest.raises(BotCommandValidationError):
        service.create_command_config("empty", "草稿", enabled=True)
    draft = service.create_command_config("draft", "草稿")
    assert draft["executionStatus"] == "not_configured"
    assert not draft["effectiveEnabled"]
    draft = service.create_command_config("missing", "待补全", executor_type="http")
    assert draft["executionStatus"] == "invalid_config"
    systems = [item for item in service.command_configs() if item["type"] == "system"]
    assert all(item["executorType"] == "none" and item["effectiveEnabled"] for item in systems)
    with pytest.raises(BotCommandForbiddenError):
        service.patch_command_config(
            "help", {"executorType": "http", "executorConfig": {"url": "https://api.example.test"}}
        )


@pytest.mark.parametrize(
    "kind,raw",
    [
        ("javascript", '{"code":"legacy"}'),
        ("unknown", '{"keep":123}'),
        ("python", '{"broken":'),
        ("http", '{"url": "oops"}'),
        ("builtin_function", '{"function": {}}'),
    ],
)
def test_legacy_raw_records_remain_inspectable_and_disable_is_lossless(db, registry, kind, raw):
    with db.session() as session:
        session.add(
            BotCommandConfig(
                command="old",
                description="历史",
                enabled=True,
                menu_visible=True,
                executor_type=kind,
                executor_config_json=raw,
            )
        )
        session.commit()
    service = BotCommandService(db, registry)
    item = next(item for item in service.command_configs() if item["command"] == "old")
    assert item["executorType"] == kind and not item["effectiveEnabled"]
    service.patch_command_config(
        "old", {"enabled": False, "description": "保留", "command": "saved"}
    )
    with db.session() as session:
        stored = session.scalar(select(BotCommandConfig).where(BotCommandConfig.command == "saved"))
        assert stored.executor_config_json == raw


def test_versioned_migration_disables_all_old_custom_commands_once(db, registry):
    with db.session() as session:
        session.execute(delete(SchemaVersion).where(SchemaVersion.version == 8))
        for name, kind in [("oldjs", "javascript"), ("oldhttp", "http"), ("help", "none")]:
            session.add(
                BotCommandConfig(
                    command=name,
                    description=name,
                    enabled=True,
                    menu_visible=True,
                    executor_type=kind,
                    executor_config_json='{"original":"kept"}',
                )
            )
        session.commit()
    with db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE bot_command_configs DROP COLUMN revision")
        connection.exec_driver_sql(
            "ALTER TABLE bot_command_configs DROP COLUMN confirmed_code_hash"
        )
    db.create_all()
    stored = {item.command: item for item in db.list_bot_command_configs()}
    assert stored["help"].enabled
    assert not stored["oldjs"].enabled and not stored["oldhttp"].menu_visible
    assert stored["oldjs"].executor_config_json == '{"original":"kept"}'
    service = BotCommandService(db, registry)
    service.patch_command_config(
        "oldhttp", {"executorType": "builtin_function", "executorConfig": ECHO, "enabled": True}
    )
    db.create_all()
    assert next(item for item in service.command_configs() if item["command"] == "oldhttp")[
        "effectiveEnabled"
    ]


def test_patch_validates_merged_config_before_rename_and_is_optimistic(db, registry):
    service = BotCommandService(db, registry)
    row = create(service)
    with pytest.raises(BotCommandValidationError):
        service.patch_command_config("custom", {"command": "renamed", "allowedRoles": ["owner"]})
    assert "custom" in {item.command for item in db.list_bot_command_configs()}
    with pytest.raises(BotCommandValidationError):
        service.patch_command_config("custom", {"command": "renamed", "executorType": "http"})
    row2 = service.patch_command_config(
        "custom", {"enabled": True, "expectedRevision": row["revision"]}
    )
    assert row2["effectiveEnabled"] and row2["revision"] == row["revision"] + 1
    with pytest.raises(BotCommandConflictError):
        service.patch_command_config(
            "custom", {"command": "renamed", "expectedRevision": row["revision"]}
        )
    create(service, "other")
    with pytest.raises(BotCommandConflictError):
        service.patch_command_config("custom", {"command": "other"})
    assert len(db.list_bot_command_configs()) == 2


def test_concurrent_rename_or_creation_is_atomic(db, registry):
    service = BotCommandService(db, registry)

    def make(_):
        try:
            create(service, "race")
            return True
        except BotCommandConflictError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(make, range(4))) == 1
    assert len(db.list_bot_command_configs()) == 1


def test_python_deployment_health_and_code_confirmation(db):
    registry = ExecutorRegistry(ExecutorPolicy(python_enabled=True))
    service = BotCommandService(db, registry)
    created = service.create_command_config(
        "py", "脚本", executor_type="python", executor_config=CODE
    )
    assert created["codeHash"] == code_hash(CODE) and not created["codeConfirmed"]
    with pytest.raises(BotCommandValidationError):
        service.patch_command_config("py", {"enabled": True})
    with pytest.raises(BotCommandValidationError):
        service.patch_command_config("py", {"enabled": True, "confirmCodeHash": code_hash(CODE)})
    registry.python_available = True
    row = service.patch_command_config("py", {"enabled": True, "confirmCodeHash": code_hash(CODE)})
    assert row["effectiveEnabled"]
    changed = {"code": CODE["code"] + "# changed\n"}
    row = service.patch_command_config("py", {"executorConfig": changed})
    assert not row["enabled"] and not row["codeConfirmed"]
    with pytest.raises(BotCommandValidationError):
        service.patch_command_config("py", {"enabled": True, "confirmCodeHash": code_hash(CODE)})
    assert ExecutorRegistry().status("python", CODE, code_hash(CODE)).state == "blocked_by_policy"


def test_builtin_role_intersection_and_reserved_alias(db, registry):
    service = BotCommandService(db, registry)
    with pytest.raises(BotCommandValidationError):
        service.create_command_config(
            "stats",
            "状态",
            enabled=True,
            allowed_roles=["user"],
            executor_type="builtin_function",
            executor_config={"function": "system_status"},
        )
    row = service.create_command_config(
        "stats",
        "状态",
        enabled=True,
        executor_type="builtin_function",
        executor_config={"function": "system_status"},
    )
    assert row["effectiveAllowedRoles"] == ["admin"]
    with pytest.raises(BotCommandConflictError):
        create(service, "task")


@pytest.mark.parametrize(
    "template",
    [
        "{{ user.__class__ }}",
        "{{ unknown }}",
        "{{ argument.upper() }}",
        "{{ 1+1 }}",
        "{{ argument",
        "bad }}",
    ],
)
def test_no_expression_or_unknown_template(template):
    with pytest.raises(ValueError):
        validate_templates(template)


def test_template_data_encoding_is_not_json_source_interpolation(context):
    result = render({"text": "{{ argument }}", "id": "{{ user.id }}"}, context)
    assert json.loads(json_bytes(result))["text"] == context.argument
    assert result["id"] == "123"


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {1: "x"}, {"x": b"secret"}, "\ud800", [0] * 5000]
)
def test_json_limits(value):
    with pytest.raises(ExecutionError):
        json_bytes(value)


def test_result_limits_account_for_telegram_utf16():
    assert ExecutionResult("😀" * 1750).validated()
    with pytest.raises(ExecutionError):
        ExecutionResult("😀" * 1751).validated()
    with pytest.raises(ExecutionError):
        ExecutionResult.from_payload({"text": "ok", "untrusted": "extra"})
