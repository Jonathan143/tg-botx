from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from tg_botx.infrastructure.persistence.db import Task
from tg_botx.schemas import ScheduleConfig


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    return local.astimezone(timezone.utc)


def _localize(naive: datetime, zone: ZoneInfo) -> datetime:
    candidate = naive.replace(tzinfo=zone, fold=0)
    roundtrip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if roundtrip == naive:
        return candidate
    alternate = naive.replace(tzinfo=zone, fold=1)
    alternate_roundtrip = alternate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if alternate_roundtrip == naive:
        return candidate  # ambiguous wall time: choose the first occurrence
    # Nonexistent wall time: zoneinfo's fold=0 UTC roundtrip is the next valid time.
    return candidate.astimezone(timezone.utc).astimezone(zone)


def _seed_for(schedule: ScheduleConfig, seed: str | None) -> str:
    if seed:
        return seed
    payload = json.dumps(schedule.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return payload


def random_local_datetime(
    day: datetime,
    schedule: ScheduleConfig,
    *,
    seed: str | None = None,
    minimum: time | None = None,
) -> datetime:
    assert schedule.start is not None and schedule.end is not None
    start = parse_clock(schedule.start)
    end = parse_clock(schedule.end)
    lower = max(start, minimum) if minimum is not None else start
    start_seconds = lower.hour * 3600 + lower.minute * 60 + lower.second
    end_seconds = end.hour * 3600 + end.minute * 60 + end.second
    if start_seconds > end_seconds:
        raise ValueError("随机时间窗口在当前时刻已结束")
    key = f"{_seed_for(schedule, seed)}:{day.date().isoformat()}:{start_seconds}:{end_seconds}"
    value = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    selected = start_seconds + value % (end_seconds - start_seconds + 1)
    return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=selected)


def _candidate_time(
    day: date, schedule: ScheduleConfig, zone: ZoneInfo, *, seed: str | None, cutoff: datetime
) -> datetime | None:
    if schedule.type == "fixed":
        assert schedule.time is not None
        return _localize(datetime.combine(day, parse_clock(schedule.time)), zone)
    assert schedule.start is not None and schedule.end is not None
    naive = random_local_datetime(
        datetime.combine(day, time.min), schedule, seed=seed
    )
    # A random schedule has one stable occurrence per eligible day.  If that
    # occurrence has already passed, skip the day instead of re-randomizing a
    # second time within the same window.
    if day == cutoff.date() and naive.time() <= cutoff.time().replace(microsecond=0):
        return None
    return _localize(naive, zone)


def _is_eligible(day: date, schedule: ScheduleConfig, anchor: date) -> bool:
    if schedule.frequency == "daily":
        return True
    if schedule.frequency == "every_n_days":
        assert schedule.interval_days is not None
        return (day - anchor).days % schedule.interval_days == 0
    if schedule.frequency == "weekly":
        return day.isoweekday() in (schedule.weekdays or [])
    return day.day in (schedule.month_days or [])


def next_runs(
    schedule: ScheduleConfig,
    *,
    now: datetime | None = None,
    count: int = 5,
    seed: str | None = None,
    start_after: datetime | None = None,
) -> list[datetime]:
    if count <= 0:
        return []
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone = ZoneInfo(schedule.timezone)
    cutoff = now_utc.astimezone(zone)
    if start_after is not None:
        cutoff = max(cutoff, start_after.astimezone(zone))
    anchor = schedule.start_date or cutoff.date()
    day = max(anchor, cutoff.date())
    results: list[datetime] = []
    for _ in range(3660):
        if schedule.end_date is not None and day > schedule.end_date:
            break
        if _is_eligible(day, schedule, anchor):
            candidate = _candidate_time(day, schedule, zone, seed=seed, cutoff=cutoff)
            if candidate is not None and candidate > cutoff:
                results.append(to_utc(candidate, zone))
                if len(results) >= count:
                    break
        day += timedelta(days=1)
    return results


def next_run_for(
    schedule: ScheduleConfig,
    now: datetime | None = None,
    after: datetime | None = None,
    seed: str | None = None,
) -> datetime:
    runs = next_runs(schedule, now=now, count=1, seed=seed, start_after=after)
    if not runs:
        raise ValueError("调度规则没有可执行的未来时间")
    return runs[0]


def schedule_from_task(task: Task) -> ScheduleConfig:
    payload = task.config.get("schedule")
    if not isinstance(payload, dict):
        payload = {}
    values = {
        **payload,
        "type": payload.get("type", task.schedule_type),
        "timezone": payload.get("timezone", task.timezone),
        "time": payload.get("time", task.fixed_time),
        "start": payload.get("start", task.random_start),
        "end": payload.get("end", task.random_end),
    }
    if values.get("start_date") is None and task.created_at is not None:
        values["start_date"] = task.created_at.astimezone(ZoneInfo(values["timezone"])).date()
    return ScheduleConfig.model_validate(values)
