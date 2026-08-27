from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from tg_botx.infrastructure.persistence.db import Task
from tg_botx.schemas import ScheduleConfig


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    return local.replace(tzinfo=zone).astimezone(timezone.utc)


def random_local_datetime(day: datetime, schedule: ScheduleConfig) -> datetime:
    start = parse_clock(schedule.start)
    end = parse_clock(schedule.end)
    start_seconds = start.hour * 3600 + start.minute * 60 + start.second
    end_seconds = end.hour * 3600 + end.minute * 60 + end.second
    # 随机到秒，包含窗口两端。
    selected = random.randint(start_seconds, end_seconds)
    return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=selected)


def next_run_for(schedule: ScheduleConfig, now: datetime | None = None, after: datetime | None = None) -> datetime:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone = ZoneInfo(schedule.timezone)
    local_now = now_utc.astimezone(zone)
    base = (after or local_now).astimezone(zone)

    if schedule.type == "fixed":
        target = parse_clock(schedule.time)
        candidate = base.replace(hour=target.hour, minute=target.minute, second=target.second, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return to_utc(candidate, zone)

    start = parse_clock(schedule.start)
    end = parse_clock(schedule.end)
    if local_now.time() < start:
        day = local_now
    elif local_now.time() < end:
        day = local_now
    else:
        day = local_now + timedelta(days=1)
    candidate = random_local_datetime(day, schedule)
    if candidate <= local_now:
        candidate = random_local_datetime(local_now + timedelta(days=1), schedule)
    return to_utc(candidate, zone)


def schedule_from_task(task: Task) -> ScheduleConfig:
    config = task.config
    return ScheduleConfig.model_validate(
        {
            "type": task.schedule_type,
            "timezone": task.timezone,
            "time": task.fixed_time,
            "start": task.random_start,
            "end": task.random_end,
        }
    )
