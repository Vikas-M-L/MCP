"""
MeetingResolver — Detects meeting requests in emails and computes free slots.

Called by PlannerAgent._build_prompts() when an email looks like a scheduling
request. The resolver fetches the calendar, finds conflicts, and formats a
compact context block that the LLM uses to draft a reschedule email.

Core logic:
  • Calendar FREE at requested time  → Planner chooses create_event (auto-schedule)
  • Calendar BUSY at requested time  → Planner chooses send_email (conflict reply)
    and Executor calls the user to notify about the conflict.

Scenarios handled:
  1. Direct conflict        → suggest next free slot same / next day
  2. Entire day packed      → suggest first free slot in next 3 days
  3. Partial overlap        → suggest shifted time same day
  4. No specific time asked → list 2-3 available slots
  5. Next day also busy     → push to day after
  6. Recurring conflict     → note recurring block, suggest alternative day
  7. Too early / too late   → suggest slot within working hours
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# IST offset (India — no DST so a fixed offset is correct)
_IST = timezone(timedelta(hours=5, minutes=30))

WORK_START_HOUR = 9   # 09:00 local time
WORK_END_HOUR   = 18  # 18:00 local time
SLOT_STEP_HOURS = 1   # granularity when scanning for free slots

# Keywords that suggest the email is about scheduling a meeting / call
MEETING_KEYWORDS = [
    "can we meet", "let's meet", "lets meet",
    "meeting", "schedule a call", "set up a call",
    "catch up", "sync up", "hop on a call",
    "zoom", "google meet", "teams", "skype",
    "available", "free slot", "book a time",
    "calendar invite", "schedule time",
    "when are you free", "are you available", "can you make it",
    "reschedule", "postpone", "move the meeting",
    "call tomorrow", "call today", "quick call",
    "discuss", "connect", "appointment",
    "fix meeting", "book meeting", "set meeting",
]

# Invitations / social — same pipeline as meetings (check calendar → book or reply)
SCHEDULING_EXTRA = [
    "party", "invitation", "invite ", "rsvp", "can you come",
    "join us", "save the date", "you're invited",
]


# ── Public API ─────────────────────────────────────────────────────────────────

def is_meeting_request(email_payload: dict[str, Any]) -> bool:
    """
    True if the email looks like a scheduling / time-boxed invitation.
    Includes meetings, parties, and explicit date+time in subject/body.
    """
    text = " ".join([
        email_payload.get("subject", ""),
        email_payload.get("snippet", ""),
        email_payload.get("body", ""),
    ]).lower()
    if any(kw in text for kw in MEETING_KEYWORDS):
        return True
    if any(k in text for k in SCHEDULING_EXTRA):
        return True
    # Heuristic: month + day + clock time (e.g. "April 20 ... 4 pm")
    if re.search(
        r"(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d{1,2}",
        text,
        re.I,
    ) and re.search(r"(?:from\s+)?\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)|\bat\s+\d", text, re.I):
        return True
    return False


def detect_requested_time(
    email_payload: dict[str, Any],
    now: datetime | None = None,
) -> datetime | None:
    """
    Try to extract the requested meeting time from the email text.

    Handles patterns like:
      "at 3pm", "at 11 am", "at 3:30 PM",
      "tomorrow at 3pm", "today at 11", "Monday at 2pm",
      "11 am tomorrow", "3 pm on Friday"

    Returns a timezone-aware datetime in IST, or None if no time could be parsed.
    """
    text = " ".join([
        email_payload.get("subject", ""),
        email_payload.get("snippet", ""),
        email_payload.get("body", ""),
    ]).lower()

    if now is None:
        now = datetime.now(_IST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── 1. Extract hour / minute ─────────────────────────────────────────────
    time_match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        text,
    )
    if not time_match:
        # Try bare hour with no am/pm ("at 10", "at 3")
        time_match = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b", text)
        if not time_match:
            return None
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        # Assume PM for hours 1-6 (most meeting times), AM otherwise
        if 1 <= hour <= 6:
            hour += 12
    else:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        meridiem = time_match.group(3)
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0

    # ── 2. Extract day offset ────────────────────────────────────────────────
    day_offset = 0
    _WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    if "tomorrow" in text:
        day_offset = 1
    elif "today" in text or "now" in text:
        day_offset = 0
    else:
        for i, name in enumerate(_WEEKDAYS):
            if name in text:
                target_weekday = i
                current_weekday = today.weekday()
                delta = (target_weekday - current_weekday) % 7
                if delta == 0:
                    delta = 7  # "Monday" when today is Monday → next Monday
                day_offset = delta
                break

    target_day = today + timedelta(days=day_offset)
    requested = target_day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If the time has already passed today, push to tomorrow
    if day_offset == 0 and requested <= now:
        requested += timedelta(days=1)

    return requested  # timezone-aware IST datetime


def has_conflict(
    requested_dt: datetime | None,
    duration_hours: float,
    busy_slots: list[tuple[datetime, datetime]],
) -> bool:
    """
    Return True if requested_dt overlaps with any busy calendar slot.
    When requested_dt is None, returns False (legacy — prefer has_time_conflict + parse_meeting_window).
    """
    if requested_dt is None:
        return False
    req_start = requested_dt.astimezone(timezone.utc)
    req_end   = req_start + timedelta(hours=duration_hours)
    return has_time_conflict(req_start, req_end, busy_slots)


def has_time_conflict(
    req_start_utc: datetime,
    req_end_utc: datetime,
    busy_slots: list[tuple[datetime, datetime]],
) -> bool:
    """True if [req_start_utc, req_end_utc) overlaps any busy interval."""
    return any(
        not (req_end_utc <= bs or req_start_utc >= be)
        for bs, be in busy_slots
    )


def _month_token_to_num(tok: str) -> int | None:
    t = tok.lower().rstrip(".")[:3]
    m = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    return m.get(t)


def _wall_to_12h(hour: int, minute: int, meridiem: str) -> tuple[int, int]:
    h = hour
    if meridiem.lower() == "pm" and h != 12:
        h += 12
    elif meridiem.lower() == "am" and h == 12:
        h = 0
    return h, minute


def parse_meeting_window(
    email_payload: dict[str, Any],
    now: datetime | None = None,
    tz_name: str | None = None,
) -> tuple[datetime, datetime] | None:
    """
    Parse explicit calendar date + time range from email text, e.g.:
      "April 10 1pm to 2pm", "10 Apr 2026 13:00-14:00", "on the April 10 at 1pm"

    Returns (start_utc, end_utc) or None if no concrete window found.
    Wall times use `tz_name` (default: Settings.meeting_parse_timezone, e.g. UTC).
    """
    if tz_name is None:
        try:
            from config.settings import get_settings
            tz_name = get_settings().meeting_parse_timezone
        except Exception:
            tz_name = "UTC"
    try:
        tz = ZoneInfo(tz_name.strip() or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")

    text = " ".join([
        email_payload.get("subject", ""),
        email_payload.get("snippet", ""),
        email_payload.get("body", ""),
    ])
    low = text.lower()

    if now is None:
        now = datetime.now(tz)

    # ── Month + day ─────────────────────────────────────────────────────────
    month_num: int | None = None
    day_num: int | None = None

    mon_pat = (
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?\b"
    )
    m1 = re.search(mon_pat, low, re.I)
    if m1:
        month_num = _month_token_to_num(m1.group(1))
        day_num = int(m1.group(2))
    else:
        m2 = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"(january|february|march|april|may|june|july|august|september|october|november|december|"
            r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\b",
            low,
            re.I,
        )
        if m2:
            day_num = int(m2.group(1))
            month_num = _month_token_to_num(m2.group(2))

    if month_num is None or day_num is None:
        return None

    # Optional explicit 4-digit year
    y_match = re.search(r"\b(20\d{2})\b", low)
    year = int(y_match.group(1)) if y_match else now.year

    # ── Time range: "1pm to 2pm", "from 4 pm to 10 pm", "4pm-10pm" ────────────
    tr = re.search(
        r"(?:\bfrom\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:to|-|–|through|until)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        low,
        re.I,
    )
    single_time = False
    if tr:
        h1, mi1 = _wall_to_12h(int(tr.group(1)), int(tr.group(2) or 0), tr.group(3))
        h2, mi2 = _wall_to_12h(int(tr.group(4)), int(tr.group(5) or 0), tr.group(6))
    else:
        st = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low, re.I)
        if not st:
            return None
        h1, mi1 = _wall_to_12h(int(st.group(1)), int(st.group(2) or 0), st.group(3))
        h2, mi2 = h1, mi1
        single_time = True

    try:
        start_local = datetime(year, month_num, day_num, h1, mi1, 0, tzinfo=tz)
        if single_time:
            end_local = start_local + timedelta(hours=1)
        else:
            end_local = datetime(year, month_num, day_num, h2, mi2, 0, tzinfo=tz)
    except ValueError:
        return None

    if not single_time and end_local <= start_local:
        end_local += timedelta(days=1)

    # If the date is in the past (same year guess), roll to next year
    if start_local.date() < now.date() - timedelta(days=1):
        try:
            y2 = year + 1
            start_local = datetime(y2, month_num, day_num, h1, mi1, 0, tzinfo=tz)
            if single_time:
                end_local = start_local + timedelta(hours=1)
            else:
                end_local = datetime(y2, month_num, day_num, h2, mi2, 0, tzinfo=tz)
                if end_local <= start_local:
                    end_local += timedelta(days=1)
        except ValueError:
            return None

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return start_utc, end_utc


def parse_busy_slots(
    calendar_events: list[dict[str, Any]],
) -> list[tuple[datetime, datetime]]:
    """
    Convert Google Calendar event list to sorted (start_utc, end_utc) pairs.
    Skips all-day events (no 'T' in the datetime string).
    """
    slots: list[tuple[datetime, datetime]] = []
    for ev in calendar_events:
        start_str = ev.get("start", "")
        end_str   = ev.get("end", "")
        if not start_str or "T" not in start_str:
            continue  # all-day event — skip
        try:
            slots.append((_parse_dt(start_str), _parse_dt(end_str)))
        except Exception:
            continue
    return sorted(slots)


def find_free_slots(
    busy_slots: list[tuple[datetime, datetime]],
    from_dt: datetime,
    n: int = 3,
    duration_hours: float = 1.0,
) -> list[datetime]:
    """
    Return up to `n` available start times (UTC) of `duration_hours` length
    within working hours (9 AM – 6 PM local) over the next 3 days.
    """
    duration = timedelta(hours=duration_hours)
    found: list[datetime] = []

    # Start search from next whole hour
    check = from_dt.astimezone(_IST).replace(minute=0, second=0, microsecond=0)
    if from_dt.minute > 0:
        check += timedelta(hours=1)

    # Scan up to 3 days × 24 hours
    for _ in range(3 * 24):
        local_hour = check.hour
        end_check  = check + duration

        # Skip outside working hours
        if local_hour < WORK_START_HOUR:
            check = check.replace(hour=WORK_START_HOUR, minute=0)
            continue
        if local_hour >= WORK_END_HOUR or end_check.hour > WORK_END_HOUR:
            # Roll to next day morning
            check = (check + timedelta(days=1)).replace(
                hour=WORK_START_HOUR, minute=0, second=0, microsecond=0
            )
            continue

        # Convert to UTC for conflict comparison
        start_utc = check.astimezone(timezone.utc)
        end_utc   = end_check.astimezone(timezone.utc)

        conflict = any(
            not (end_utc <= bs or start_utc >= be)
            for bs, be in busy_slots
        )
        if not conflict:
            found.append(start_utc)
            if len(found) >= n:
                break

        check += timedelta(hours=SLOT_STEP_HOURS)

    return found


def format_calendar_for_prompt(
    calendar_events: list[dict[str, Any]],
    free_slots: list[datetime],
) -> str:
    """
    Return a compact calendar context string for injection into the LLM prompt.
    All times are shown in IST for readability.
    """
    lines = ["=== Calendar (next 3 days, IST) ==="]

    if not calendar_events:
        lines.append("  No events scheduled.")
    else:
        for ev in calendar_events[:12]:  # cap to avoid prompt bloat
            start = ev.get("start", "")
            end   = ev.get("end", "")
            # Convert to IST display if possible
            if "T" in start:
                try:
                    start = _parse_dt(start).astimezone(_IST).strftime("%a %d %b %I:%M %p")
                    end   = _parse_dt(end).astimezone(_IST).strftime("%I:%M %p")
                    lines.append(f"  BUSY  {start} – {end}  |  {ev.get('summary','?')}")
                except Exception:
                    lines.append(f"  BUSY  {ev.get('start','')} – {ev.get('end','')}  |  {ev.get('summary','?')}")
            else:
                lines.append(f"  BUSY  {start} (all day)  |  {ev.get('summary','?')}")

    lines.append("")
    if free_slots:
        lines.append("Available free slots (IST):")
        for slot in free_slots:
            ist = slot.astimezone(_IST)
            lines.append(f"  FREE  {ist.strftime('%A %d %b, %I:%M %p – ') + (ist + timedelta(hours=1)).strftime('%I:%M %p')} IST")
    else:
        lines.append("No free 1-hour slots found in next 3 days during working hours.")

    lines.append("===================================")
    return "\n".join(lines)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _parse_dt(s: str) -> datetime:
    """
    Parse ISO 8601 datetime string to timezone-aware UTC datetime.
    Handles both '+05:30' and '+0530' offset formats.
    """
    # Python < 3.11 fromisoformat doesn't accept colon in UTC offset → remove it
    s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s.strip())
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Strip timezone and treat as UTC
        dt = datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
