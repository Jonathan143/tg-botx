from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tg_botx.infrastructure.persistence.db import Task
from tg_botx.schemas import ScheduleConfig


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    return local.astimezone(UTC)


def _localize(naive: datetime, zone: ZoneInfo) -> datetime:
    candidate = naive.replace(tzinfo=zone, fold=0)
    roundtrip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if roundtrip == naive:
        return candidate
    alternate = naive.replace(tzinfo=zone, fold=1)
    alternate_roundtrip = alternate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if alternate_roundtrip == naive:
        return candidate  # ambiguous wall time: choose the first occurrence
    # Nonexistent wall time: zoneinfo's fold=0 UTC roundtrip is the next valid time.
    return candidate.astimezone(UTC).astimezone(zone)


def _seed_for(schedule: ScheduleConfig, seed: str | None) -> str:
    if seed:
        return seed
    values = schedule.model_dump(mode="json")
    if schedule.execution_count == 1:
        # Keep the stable occurrence of existing one-run schedules unchanged.
        values.pop("execution_count", None)
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


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


def _random_occurrences(
    day: date, schedule: ScheduleConfig, zone: ZoneInfo, *, seed: str | None
) -> list[datetime]:
    """Build one stable, distinct, ordered UTC plan for an eligible local day."""
    assert schedule.start is not None and schedule.end is not None
    start_clock = parse_clock(schedule.start)
    end_clock = parse_clock(schedule.end)
    start = datetime.combine(day, start_clock)
    end = datetime.combine(day, end_clock)
    lower = min(start.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1))
    upper = max(end.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1))
    offsets: range | list[int] = range(int((upper - lower).total_seconds()) + 1)
    if (
        lower.astimezone(zone).replace(tzinfo=None) != start
        or upper.astimezone(zone).replace(tzinfo=None) != end
        or lower.astimezone(zone).utcoffset() != upper.astimezone(zone).utcoffset()
    ):
        # On DST transition dates only sample real seconds inside the wall-clock
        # window. Never shift a nonexistent time outside the configured window.
        offsets = [
            offset
            for offset in offsets
            if (local := (lower + timedelta(seconds=offset)).astimezone(zone)).date() == day
            and start_clock <= local.time() <= end_clock
        ]
    if len(offsets) < schedule.execution_count:
        return []
    key = f"{_seed_for(schedule, seed)}:{day.isoformat()}:{schedule.start}:{schedule.end}"
    generator = random.Random(hashlib.sha256(key.encode()).digest())
    selected = sorted(generator.sample(offsets, schedule.execution_count))
    return [lower + timedelta(seconds=offset) for offset in selected]


def _candidate_times(
    day: date, schedule: ScheduleConfig, zone: ZoneInfo, *, seed: str | None
) -> list[datetime]:
    if schedule.type == "fixed":
        assert schedule.time is not None
        return [_localize(datetime.combine(day, parse_clock(schedule.time)), zone).astimezone(UTC)]
    if schedule.execution_count > 1:
        return _random_occurrences(day, schedule, zone, seed=seed)
    assert schedule.start is not None and schedule.end is not None
    naive = random_local_datetime(datetime.combine(day, time.min), schedule, seed=seed)
    candidate = _localize(naive, zone)
    if candidate.date() != day or not (
        parse_clock(schedule.start) <= candidate.time() <= parse_clock(schedule.end)
    ):
        return []
    return [candidate.astimezone(UTC)]


def _candidate_time(
    day: date, schedule: ScheduleConfig, zone: ZoneInfo, *, seed: str | None, cutoff: datetime
) -> datetime | None:
    return next(
        (
            candidate.astimezone(zone)
            for candidate in _candidate_times(day, schedule, zone, seed=seed)
            if candidate > cutoff.astimezone(UTC)
        ),
        None,
    )


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
    cutoff = (now or datetime.now(UTC)).astimezone(UTC)
    zone = ZoneInfo(schedule.timezone)
    if start_after is not None:
        cutoff = max(cutoff, start_after.astimezone(UTC))
    local_date = cutoff.astimezone(zone).date()
    anchor = schedule.start_date or local_date
    day = max(anchor, local_date)
    results: list[datetime] = []
    for _ in range(3660):
        if schedule.end_date is not None and day > schedule.end_date:
            break
        if _is_eligible(day, schedule, anchor):
            for candidate in _candidate_times(day, schedule, zone, seed=seed):
                if candidate > cutoff:
                    results.append(candidate)
                    if len(results) >= count:
                        return results
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
    # ``config_json`` is the editable draft. Once published, formal scheduling
    # keeps using the published schedule until the next publish operation.
    payload: object = task.config.get("schedule")
    published_schedule_json = getattr(task, "published_schedule_json", None)
    if published_schedule_json:
        try:
            published = json.loads(published_schedule_json)
        except (TypeError, json.JSONDecodeError):
            published = None
        if isinstance(published, dict):
            payload = published
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
