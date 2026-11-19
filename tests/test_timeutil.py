"""Timezone formatting: real America/Los_Angeles rules, never a fixed UTC-7."""
from datetime import datetime, timezone

from timeutil import fmt_duration, fmt_local, parse_utc, to_local


def test_winter_is_pst_utc_minus_8():
    # the incident: 2026-11-11 02:10:09 UTC == Nov 10, 6:10 PM PST
    assert fmt_local("2026-11-11 02:10:09") == "Nov 10, 06:10 PM PST"
    assert to_local("2026-11-11 02:10:09").utcoffset().total_seconds() == -8 * 3600


def test_summer_is_pdt_utc_minus_7():
    assert fmt_local("2026-07-15 19:00:00") == "Jul 15, 12:00 PM PDT"
    assert to_local("2026-07-15 19:00:00").utcoffset().total_seconds() == -7 * 3600


def test_dst_transitions_2026():
    # DST ends 2026-11-01 09:00 UTC (2:00 PDT -> 1:00 PST)
    assert fmt_local("2026-11-01 08:59:00", "%H:%M %Z") == "01:59 PDT"
    assert fmt_local("2026-11-01 09:00:00", "%H:%M %Z") == "01:00 PST"
    # DST starts 2026-03-08 10:00 UTC (2:00 PST -> 3:00 PDT)
    assert fmt_local("2026-03-08 09:59:00", "%H:%M %Z") == "01:59 PST"
    assert fmt_local("2026-03-08 10:00:00", "%H:%M %Z") == "03:00 PDT"


def test_parse_utc_accepts_all_stored_formats():
    expected = datetime(2026, 11, 19, 6, 42, 48, tzinfo=timezone.utc)
    assert parse_utc("2026-11-19 06:42:48") == expected                    # SQLite
    assert parse_utc("2026-11-19 06:42:48.000000000") == expected          # Whisker activity feed
    assert parse_utc("2026-11-19T06:42:48Z") == expected
    assert parse_utc("2026-11-19T06:42:48.163Z").replace(microsecond=0) == expected
    assert parse_utc("2026-11-18T22:42:48-08:00") == expected
    assert parse_utc(expected.timestamp()) == expected
    assert parse_utc(datetime(2026, 11, 19, 6, 42, 48)) == expected        # naive = UTC


def test_other_timezone_and_bad_name_falls_back():
    assert fmt_local("2026-11-11 02:10:09", "%H:%M %Z", "America/New_York") == "21:10 EST"
    assert fmt_local("2026-11-11 02:10:09", "%H:%M %Z", "Not/AZone") == "18:10 PST"


def test_fmt_duration():
    assert fmt_duration(45) == "45s"
    assert fmt_duration(300) == "5 min"
    assert fmt_duration(2 * 3600 + 5 * 60) == "2h 05m"
    assert fmt_duration(3 * 86400 + 3600) == "3d 1h"
