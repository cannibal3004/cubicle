"""Scheduled tasks: a prompt that runs in a bot's chat at set times.

A schedule belongs to one bot and carries a name, the prompt to run, and a
cadence: once at a time, every N minutes, daily at a time, or weekly on
chosen days at a time. Times are in the app's timezone (SCHEDULE_TIMEZONE,
or TZ). When a schedule is due the scheduler (scheduler.py) posts the prompt
into the bot's chat as a user message marked as scheduled and starts an
ordinary turn: the bot has its tools, notes and to-do list, approvals work
as they do for any turn, and the reply lands in the chat.

The user manages schedules in the bot editor. The bot can create and delete
them too, through schedule_* tools that pause for approval, so "remind me
tomorrow" and "check this every morning" are things a bot can set up. Both
routes go through this module; the rows live in the `schedules` table.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.storage_service import StorageService, storage_service

logger = logging.getLogger(__name__)

KINDS = ("once", "every", "daily", "weekly")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_NAME_CHARS = 80
MAX_PROMPT_CHARS = 4000
MAX_NOTE_CHARS = 300
MAX_PER_BOT = settings.SCHEDULES_MAX_PER_BOT
MIN_EVERY_MINUTES = settings.SCHEDULE_MIN_MINUTES
MAX_EVERY_MINUTES = 60 * 24 * 7
LIST_ACTION = "schedule.list"
CREATE_ACTION = "schedule.create"
DELETE_ACTION = "schedule.delete"


class ScheduleError(RuntimeError):
    pass


# ---- time ------------------------------------------------------------------


def app_timezone() -> tzinfo:
    """The zone schedules are written in; UTC when the configured name is unknown."""
    name = (settings.SCHEDULE_TIMEZONE or "").strip()
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # Unknown name, or no tz database on this host.
        logger.warning("Unknown timezone %r; schedules use UTC", name)
        return timezone.utc


def now_local(tz: Optional[tzinfo] = None) -> datetime:
    return datetime.now(tz or app_timezone())


def _parse_time(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ScheduleError("'time' must be HH:MM, e.g. 08:30.") from exc
    return parsed.strftime("%H:%M")


def _parse_at(value: Any, tz: tzinfo) -> datetime:
    text = str(value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ScheduleError("'at' must be a date and time such as 2026-10-15 09:00.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def clean_cadence(cadence: Any, *, tz: Optional[tzinfo] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """A valid cadence dict, or ScheduleError."""
    tz = tz or app_timezone()
    if not isinstance(cadence, dict):
        raise ScheduleError("A cadence is required: once, every, daily or weekly.")
    kind = str(cadence.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise ScheduleError("'kind' must be once, every, daily or weekly.")
    if kind == "once":
        if cadence.get("in_minutes") is not None and not cadence.get("at"):
            try:
                minutes = int(cadence["in_minutes"])
            except (TypeError, ValueError) as exc:
                raise ScheduleError("'in_minutes' must be a whole number of minutes.") from exc
            if minutes < 1 or minutes > MAX_EVERY_MINUTES * 52:
                raise ScheduleError("'in_minutes' must be between 1 and a year.")
            at = (now or now_local(tz)) + timedelta(minutes=minutes)
        else:
            at = _parse_at(cadence.get("at"), tz)
        if at <= (now or now_local(tz)) - timedelta(minutes=1):
            raise ScheduleError(f"'at' is in the past ({at.strftime('%Y-%m-%d %H:%M')}).")
        return {"kind": "once", "at": at.astimezone(tz).strftime("%Y-%m-%d %H:%M")}
    if kind == "every":
        raw = cadence.get("minutes")
        if raw is None and cadence.get("hours") is not None:
            try:
                raw = float(cadence["hours"]) * 60
            except (TypeError, ValueError) as exc:
                raise ScheduleError("'hours' must be a number.") from exc
        try:
            minutes = int(round(float(raw)))
        except (TypeError, ValueError) as exc:
            raise ScheduleError("'minutes' must be a whole number of minutes.") from exc
        if minutes < MIN_EVERY_MINUTES or minutes > MAX_EVERY_MINUTES:
            raise ScheduleError(f"'minutes' must be between {MIN_EVERY_MINUTES} and {MAX_EVERY_MINUTES} (a week).")
        return {"kind": "every", "minutes": minutes}
    time_text = _parse_time(cadence.get("time"))
    if kind == "daily":
        return {"kind": "daily", "time": time_text}
    days = cadence.get("days")
    if isinstance(days, str):
        days = [d for d in days.replace(",", " ").split() if d]
    if not isinstance(days, (list, tuple)) or not days:
        raise ScheduleError("'days' must list the weekdays, e.g. [0, 2, 4] or [\"mon\", \"wed\"] (0 is Monday).")
    cleaned: List[int] = []
    for day in days:
        index: Optional[int] = None
        if isinstance(day, int) and not isinstance(day, bool):
            index = day
        elif isinstance(day, str):
            text = day.strip().lower()
            if text.isdigit():
                index = int(text)
            else:
                index = next((i for i, name in enumerate(DAY_NAMES) if text.startswith(name.lower())), None)
        if index is None or index < 0 or index > 6:
            raise ScheduleError(f"Unknown weekday {day!r}; use 0-6 (0 is Monday) or names.")
        if index not in cleaned:
            cleaned.append(index)
    return {"kind": "weekly", "days": sorted(cleaned), "time": time_text}


def describe_cadence(cadence: Dict[str, Any]) -> str:
    kind = cadence.get("kind")
    if kind == "once":
        return f"once at {cadence.get('at')}"
    if kind == "every":
        minutes = int(cadence.get("minutes") or 0)
        if minutes % 1440 == 0:
            n = minutes // 1440
            return "every day" if n == 1 else f"every {n} days"
        if minutes % 60 == 0:
            n = minutes // 60
            return "every hour" if n == 1 else f"every {n} hours"
        return f"every {minutes} minutes"
    if kind == "daily":
        return f"daily at {cadence.get('time')}"
    if kind == "weekly":
        names = ", ".join(DAY_NAMES[d] for d in cadence.get("days") or [] if 0 <= int(d) <= 6)
        return f"weekly on {names} at {cadence.get('time')}"
    return "unknown"


def _at_time(day: datetime, time_text: str) -> datetime:
    hour, minute = (int(part) for part in time_text.split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_run(cadence: Dict[str, Any], *, after: datetime, anchor: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """The first run strictly after `after`, or None when there is none (a past once).

    `anchor` is where an 'every' cadence counts from: the last run, else when
    the schedule was created. Results are in the app's timezone.
    """
    tz = tz or app_timezone()
    after = after.astimezone(tz)
    kind = cadence.get("kind")
    if kind == "once":
        at = _parse_at(cadence.get("at"), tz)
        return at if at > after else None
    if kind == "every":
        step = timedelta(minutes=int(cadence.get("minutes") or MIN_EVERY_MINUTES))
        base = (anchor or after).astimezone(tz)
        candidate = base + step
        if candidate <= after:
            missed = int((after - base) / step)
            candidate = base + step * (missed + 1)
        return candidate
    time_text = str(cadence.get("time") or "00:00")
    if kind == "daily":
        candidate = _at_time(after, time_text)
        if candidate <= after:
            candidate = _at_time(after + timedelta(days=1), time_text)
        return candidate
    if kind == "weekly":
        days = sorted(int(d) for d in cadence.get("days") or [])
        if not days:
            return None
        for offset in range(0, 8):
            day = after + timedelta(days=offset)
            if day.weekday() in days:
                candidate = _at_time(day, time_text)
                if candidate > after:
                    return candidate
        return None
    return None


# ---- records ---------------------------------------------------------------


def _clean_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise ScheduleError("A schedule needs a name.")
    if len(text) > MAX_NAME_CHARS:
        raise ScheduleError(f"The name is limited to {MAX_NAME_CHARS} characters.")
    return text


def _clean_prompt(prompt: Any) -> str:
    text = str(prompt or "").replace("\r\n", "\n").strip()
    if not text:
        raise ScheduleError("A schedule needs the prompt to run.")
    if len(text) > MAX_PROMPT_CHARS:
        raise ScheduleError(f"The prompt is limited to {MAX_PROMPT_CHARS} characters.")
    return text


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="minutes") if value else None


def _parse_iso(value: Optional[str], tz: tzinfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def public(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record as the API and the model see it."""
    cadence = record.get("cadence") or {}
    return {
        "id": record["id"],
        "bot_id": record["bot_id"],
        "name": record["name"],
        "prompt": record["prompt"],
        "cadence": cadence,
        "cadence_text": describe_cadence(cadence),
        "enabled": bool(record.get("enabled", True)),
        "created_by": record.get("created_by") or "user",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "next_run_at": record.get("next_run_at"),
        "last_run_at": record.get("last_run_at"),
        "last_status": record.get("last_status"),
        "last_note": record.get("last_note"),
    }


def _storage(storage: Optional[StorageService]) -> StorageService:
    return storage or storage_service


def list_for_bot(bot_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    return [public(r) for r in _storage(storage).list_schedules(bot_id)]


def get_schedule(schedule_id: str, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    record = _storage(storage).get_schedule(schedule_id)
    if record is None:
        raise ScheduleError("There is no schedule with that id.")
    return record


def create_schedule(
    bot_id: str,
    name: Any,
    prompt: Any,
    cadence: Any,
    *,
    created_by: str = "user",
    storage: Optional[StorageService] = None,
    tz: Optional[tzinfo] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    storage = _storage(storage)
    tz = tz or app_timezone()
    now = now or now_local(tz)
    if not any(b.get("id") == bot_id for b in storage.get_bots()):
        raise ScheduleError("The bot does not exist.")
    if len(storage.list_schedules(bot_id)) >= MAX_PER_BOT:
        raise ScheduleError(f"A bot may have at most {MAX_PER_BOT} schedules; delete one first.")
    cleaned = clean_cadence(cadence, tz=tz, now=now)
    record = {
        "id": f"sch-{uuid.uuid4().hex[:8]}",
        "bot_id": bot_id,
        "name": _clean_name(name),
        "prompt": _clean_prompt(prompt),
        "cadence": cleaned,
        "enabled": True,
        "created_by": created_by if created_by in ("user", "bot") else "user",
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "next_run_at": _iso(next_run(cleaned, after=now, anchor=now, tz=tz)),
        "last_run_at": None,
        "last_status": None,
        "last_note": None,
    }
    storage.save_schedule(record)
    return public(record)


def update_schedule(
    schedule_id: str,
    *,
    name: Any = None,
    prompt: Any = None,
    cadence: Any = None,
    enabled: Optional[bool] = None,
    storage: Optional[StorageService] = None,
    tz: Optional[tzinfo] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    storage = _storage(storage)
    tz = tz or app_timezone()
    now = now or now_local(tz)
    record = get_schedule(schedule_id, storage)
    if name is not None:
        record["name"] = _clean_name(name)
    if prompt is not None:
        record["prompt"] = _clean_prompt(prompt)
    if cadence is not None:
        record["cadence"] = clean_cadence(cadence, tz=tz, now=now)
        record["next_run_at"] = _iso(next_run(record["cadence"], after=now, anchor=now, tz=tz))
    if enabled is not None:
        record["enabled"] = bool(enabled)
        if record["enabled"] and not record.get("next_run_at"):
            record["next_run_at"] = _iso(next_run(record["cadence"], after=now, anchor=now, tz=tz))
    record["updated_at"] = _iso(now)
    storage.save_schedule(record)
    return public(record)


def delete_schedule(schedule_id: str, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = _storage(storage)
    record = get_schedule(schedule_id, storage)
    storage.delete_schedule(schedule_id)
    return public(record)


def due_schedules(now: Optional[datetime] = None, storage: Optional[StorageService] = None, tz: Optional[tzinfo] = None) -> List[Dict[str, Any]]:
    """Enabled schedules whose next run is now or earlier (raw records)."""
    tz = tz or app_timezone()
    now = now or now_local(tz)
    due = []
    for record in _storage(storage).list_schedules():
        if not record.get("enabled", True):
            continue
        when = _parse_iso(record.get("next_run_at"), tz)
        if when is not None and when <= now:
            due.append(record)
    return due


def record_run(
    schedule_id: str,
    status: str,
    note: str = "",
    *,
    storage: Optional[StorageService] = None,
    tz: Optional[tzinfo] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Note the outcome of a run and move the schedule on; a once is finished."""
    storage = _storage(storage)
    tz = tz or app_timezone()
    now = now or now_local(tz)
    record = storage.get_schedule(schedule_id)
    if record is None:
        return None
    record["last_run_at"] = _iso(now)
    record["last_status"] = status
    record["last_note"] = (note or "")[:MAX_NOTE_CHARS]
    upcoming = next_run(record["cadence"], after=now, anchor=now, tz=tz)
    record["next_run_at"] = _iso(upcoming)
    if record["cadence"].get("kind") == "once":
        record["enabled"] = False
    record["updated_at"] = _iso(now)
    storage.save_schedule(record)
    return public(record)


def scheduled_message(record: Dict[str, Any], *, message_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The user message a run posts into the chat."""
    when = now or now_local()
    return {
        "id": message_id,
        "thread_id": record["bot_id"],
        "bot_id": record["bot_id"],
        "sender": "user",
        "text": record["prompt"],
        "created_at": datetime.now().isoformat(),
        "item_type": "user_text",
        "raw_payload": {
            "scheduled": {"id": record["id"], "name": record["name"], "at": when.strftime("%Y-%m-%d %H:%M"), "cadence": describe_cadence(record["cadence"])}
        },
    }


# ---- gateway actions (the bot's tools) ---------------------------------------


def _bot_id(call: ActionInvocation) -> str:
    bot_id = call.arguments.get("bot_id")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ScheduleError("Schedule actions require a bot id.")
    return bot_id


def _clock() -> Dict[str, Any]:
    now = now_local()
    return {"now": now.strftime("%Y-%m-%d %H:%M"), "weekday": DAY_NAMES[now.weekday()], "timezone": settings.SCHEDULE_TIMEZONE or "UTC"}


async def execute_list(call: ActionInvocation) -> Dict[str, Any]:
    return {"schedules": list_for_bot(_bot_id(call)), **_clock()}


async def execute_create(call: ActionInvocation) -> Dict[str, Any]:
    args = call.arguments
    created = create_schedule(_bot_id(call), args.get("name"), args.get("prompt"), args.get("cadence"), created_by="bot")
    return {"schedule": created, **_clock()}


async def execute_delete(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = _bot_id(call)
    record = get_schedule(str(call.arguments.get("id") or ""))
    if record["bot_id"] != bot_id:
        raise ScheduleError("That schedule belongs to another bot.")
    return {"deleted": delete_schedule(record["id"]), **_clock()}


def register_schedule_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(name=LIST_ACTION, tool="schedule", action="list", intent="Show the bot its schedules.", risk="read", requires_approval=False),
        execute_list,
    )
    target.register_action(
        ActionDefinition(name=CREATE_ACTION, tool="schedule", action="create", intent="Schedule a prompt to run later in this chat.", risk="write"),
        execute_create,
    )
    target.register_action(
        ActionDefinition(name=DELETE_ACTION, tool="schedule", action="delete", intent="Delete one of the bot's schedules.", risk="write"),
        execute_delete,
    )


register_schedule_actions()
