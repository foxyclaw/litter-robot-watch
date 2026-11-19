"""Timezone helpers — local times are always rendered with the real zone
abbreviation (PST/PDT), never a hard-coded UTC offset."""
from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Los_Angeles"


def get_tz(name: Optional[str] = None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def parse_utc(value: Union[str, datetime, float, int]) -> datetime:
    """Parse a DB timestamp ('YYYY-MM-DD HH:MM:SS[.fff]', ISO, epoch) as aware UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        s = str(value).strip().replace("T", " ")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # Whisker activity timestamps carry nanoseconds; trim to microseconds.
        if "." in s:
            head, frac = s.split(".", 1)
            tz_part = ""
            for sep in ("+", "-"):
                if sep in frac:
                    frac, rest = frac.split(sep, 1)
                    tz_part = sep + rest
                    break
            s = f"{head}.{frac[:6]}{tz_part}"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(value: Union[str, datetime, float, int], tz_name: Optional[str] = None) -> datetime:
    return parse_utc(value).astimezone(get_tz(tz_name))


def fmt_local(value: Union[str, datetime, float, int], fmt: str = "%b %d, %I:%M %p %Z",
              tz_name: Optional[str] = None) -> str:
    """Format a UTC timestamp in local time, e.g. 'Nov 10, 06:10 PM PST'."""
    return to_local(value, tz_name).strftime(fmt)


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    return f"{hours // 24}d {hours % 24}h"
