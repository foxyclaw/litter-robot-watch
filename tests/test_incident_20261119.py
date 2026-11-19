"""Replay of the 2026-11-19 incident (offline, clock injected).

Stage 1, 06:46: a clean cycle stalls on globe-motor over-torque, the fault
latches at FAULT_TIMEOUT and the litter level reads 90% -> 40% while the waste
drawer stays at 16%.  Stage 2, 08:00: the next cycle — started from idle, with
no cat detection — sends the litter into the drawer (16% -> 75%).

What went wrong that morning and is asserted here:
  * the stage-1 alert said "dumped into the waste drawer" before the drawer moved;
  * a "litter level is REFILL" alert duplicated it at the same moment;
  * polling fell back to 5 min at ~07:00 although an incident was open and the
    fault was latched, so the 08:00 cycle was not observed.
"""
import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from conftest import make_raw

import checks.litter_robot as lr
from checks.litter_robot import LitterRobotCheck
from detection import Detector, DetectionConfig, extract_fields, fast_poll_reason

RID = "robot-1"
T0 = 1_700_000_000.0    # in the past: the wall clock can never keep the fast-poll hold alive


def hms(text):
    h, m, s = (int(x) for x in text.split(":"))
    return h * 3600 + m * 60 + s


IDLE = dict(DFILevelPercent=16, litterLevel=430, odometerCleanCycles=3759)
AFTER_STAGE1 = dict(
    DFILevelPercent=16, litterLevel=480, litterLevelPercentage=0.4, litterLevelState="REFILL",
    globeMotorFaultStatus="FAULT_TIMEOUT", displayCode="DC_MODE_CYCLE", odometerCleanCycles=3760,
)
CYCLING = dict(robotStatus="ROBOT_CLEAN", robotCycleState="CYCLE_STATE_PROCESS",
               displayCode="DC_MODE_CYCLE")


def robot_at(t):
    """(status, raw) of the robot at second-of-day ``t`` as recorded that morning."""
    if t < hms("06:37:21"):
        return "READY", make_raw(**IDLE)
    if t < hms("06:40:02"):
        return "CAT_DETECTED", make_raw(**IDLE, displayCode="DC_CAT_DETECT", catDetect="CAT_DETECT")
    if t < hms("06:46:32"):
        return "CAT_SENSOR_TIMING", make_raw(**IDLE, robotStatus="ROBOT_CAT_DETECT_DELAY")
    if t < hms("06:47:00"):     # dump rotation, over-torque retries
        return "CLEAN_CYCLE", make_raw(**{**IDLE, **CYCLING, "odometerCleanCycles": 3760},
                                       robotCycleStatus="CYCLE_DUMP")
    if t < hms("06:47:30"):
        return "CLEAN_CYCLE", make_raw(**{**IDLE, **CYCLING, "odometerCleanCycles": 3760,
                                          "robotCycleState": "CYCLE_STATE_FAULT_RETRY"},
                                       robotCycleStatus="CYCLE_DUMP")
    if t < hms("06:48:53"):     # gave up, returning to level
        return "CLEAN_CYCLE", make_raw(**{**IDLE, **CYCLING, "odometerCleanCycles": 3760},
                                       robotCycleStatus="CYCLE_LEVEL",
                                       globeMotorFaultStatus="FAULT_OVERTORQUE_AMP")
    if t < hms("07:58:40"):     # "idle": fault latched, litter 40%, drawer unchanged
        return "READY", make_raw(**AFTER_STAGE1)
    if t < hms("08:00:43"):
        return "READY", make_raw(**{**AFTER_STAGE1, "displayCode": "DCX_REFRESH"})
    if t < hms("08:02:44"):     # the next cycle, from idle, no cat detection
        waste = 16 if t < hms("08:01:45") else 75
        return "CLEAN_CYCLE", make_raw(**{**AFTER_STAGE1, **CYCLING, "odometerCleanCycles": 3761,
                                          "DFILevelPercent": waste},
                                       robotCycleStatus="CYCLE_DUMP")
    return "READY", make_raw(DFILevelPercent=75, litterLevel=482, litterLevelPercentage=0.36,
                             litterLevelState="REFILL", odometerCleanCycles=3761)


class FakeTelegram:
    def __init__(self):
        self.reliable, self.waste = [], []
        self.authorized_user_ids = []
        self.bot_token, self.chat_id = "TOKEN", "-100"

    async def send_waste_alert(self, robot_name, level):
        self.waste.append((robot_name, level))
        return True

    async def send_reliable(self, message, chat_id=None):
        self.reliable.append(message)
        return True

    def ensure_retry_task(self):
        pass


def make_check(tmp_path):
    cfg = {
        "database": str(tmp_path / "test.db"),
        "forensics": {"jsonl_dir": str(tmp_path / "data"), "retention_days": 90},
        "realtime": {"websocket": False},   # defaults: fast poll 30 s, hold 300 s
        "alerts": {"waste_threshold": 90},
    }
    tg = FakeTelegram()
    return LitterRobotCheck(cfg, notifier=tg), tg


async def replay(check, start, end, normal_interval=300):
    """Drive the check the way CheckScheduler does (5 s ticks; the interval is
    the fast one whenever the check asks for it). Returns (polls, alerts)."""
    polls, alerts, last_run = [], [], None
    for t in range(hms(start), hms(end) + 1, 5):
        fast = check.fast_poll_reason(now=T0 + t)
        interval = min(normal_interval, check.fast_interval) if fast else normal_interval
        if last_run is not None and t - last_run < interval:
            continue
        last_run = t
        status, raw = robot_at(t)
        state = {"id": RID, "name": raw["name"], "status": status,
                 "waste_drawer_level": raw["DFILevelPercent"],
                 "is_waste_drawer_full": raw["isDFIFull"]}
        got = await check.process_snapshot(RID, state, raw, source="poll", now=T0 + t)
        polls.append((t, status, raw))
        alerts += [(t, a) for a in got]
    return polls, alerts


def test_two_stage_dump_wording_and_fast_polling_through_the_gap(tmp_path, caplog):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        with caplog.at_level(logging.INFO):
            polls, alerts = await replay(check, "06:30:00", "08:10:00")

        dump = [(t, a) for t, a in alerts if a.key == "litter_dump"]
        fault = [(t, a) for t, a in alerts if a.key == "globe_motor_fault"]

        # ── stage 1: globe fault, then the litter drop — no drawer claim ──
        assert fault[0][1].kind == "new" and hms("06:47:30") <= fault[0][0] <= hms("06:48:05")
        t1, stage1 = dump[0]
        assert stage1.kind == "new"
        assert hms("06:48:53") <= t1 <= hms("06:49:30")     # at once: a fault is latched
        assert "litter level dropped 90%→40% after a failed cycle" in stage1.text
        assert "litter missing from the globe or globe not level" in stage1.text
        assert "Do NOT let it cycle again until checked" in stage1.text
        assert "(the next cycle tends to dump the litter into the drawer)" in stage1.text
        assert "waste drawer unchanged at 16%" in stage1.text
        assert "dumped into the waste drawer" not in stage1.text

        # every dump message before the drawer jump (incl. the hourly reminder) stays stage-1
        before = [a for t, a in dump if t < hms("08:00:00")]
        assert [a.kind for a in before] == ["new", "repeat"]
        assert all("dumped into the waste drawer" not in a.text for a in before)

        # ── no duplicate "litter level is REFILL" alert while the dump incident is open ──
        assert all(a.key != "litter_low" for _t, a in alerts)
        assert not any("litter level is" in m for m in tg.reliable)

        # ── the gap 06:49 -> 08:03: polled every 30 s the whole time ──
        times = [t for t, _s, _r in polls]
        gap = [t for t in times if hms("06:46:00") <= t <= hms("08:04:00")]
        assert max(b - a for a, b in zip(gap, gap[1:])) <= 35
        assert len([t for t in times if hms("07:03:00") <= t <= hms("08:00:00")]) >= 100
        # ... so the 08:00 cycle is seen while it runs, not reconstructed afterwards
        in_cycle = [raw for t, _s, raw in polls if hms("08:00:43") <= t < hms("08:02:44")]
        assert len(in_cycle) >= 3
        assert all(r["robotCycleStatus"] == "CYCLE_DUMP" for r in in_cycle)
        assert caplog.text.count("Fast polling (30s) on for COPTiP") == 1
        assert "Fast polling off" not in caplog.text

        # ── stage 2: the drawer jumps — now, and only now, "into the waste drawer" ──
        t2, stage2 = [(t, a) for t, a in dump if t >= hms("08:00:00")][0]
        assert stage2.kind == "update"
        assert hms("08:02:44") <= t2 <= hms("08:03:20")
        assert "litter dumped into the waste drawer" in stage2.text
        assert "waste drawer 16%→75%" in stage2.text and "litter level 90%→40%" in stage2.text
        assert ("globe_motor_fault", "resolved") in [(a.key, a.kind) for _t, a in fault]

        # still fast afterwards: the dump incident is open although the robot looks idle
        assert fast_poll_reason(extract_fields(robot_at(hms("08:09:00"))[1], "READY")) is None
        assert check.fast_poll_reason(now=T0 + hms("09:30:00")) == "open incident litter_dump"
        assert check.interval_override == 30
        await check.db.disconnect()
    asyncio.run(scenario())


def test_fast_polling_stays_on_for_a_latched_fault_alone_then_turns_off(tmp_path, caplog):
    """The old logic looked only at status / cycle status / pending alerts plus a
    5-min hold. A latched fault, a non-idle display or an open incident with an
    otherwise idle robot must each keep 30 s polling on by themselves."""
    async def scenario():
        check, _tg = make_check(tmp_path)
        await check._init_db()

        async def feed(t, **overrides):
            raw = make_raw(**overrides)
            state = {"id": RID, "name": raw["name"], "status": "READY",
                     "waste_drawer_level": raw["DFILevelPercent"], "is_waste_drawer_full": False}
            await check.process_snapshot(RID, state, raw, source="poll", now=T0 + t)

        with caplog.at_level(logging.INFO):
            await feed(0)
            assert check.fast_poll_reason(now=T0) is None and check.interval_override is None

            await feed(300, globeMotorFaultStatus="FAULT_TIMEOUT")
            for later in (300, 1800, 6 * 3600):          # far beyond the 300 s hold
                assert check.fast_poll_reason(now=T0 + later) == "globeMotorFault FAULT_TIMEOUT"
            assert check.interval_override == 30

            await feed(330)                               # fault cleared, display back to idle
            # the hold runs from the last snapshot that had a reason (t=300)
            assert check.fast_poll_reason(now=T0 + 300 + 299) == "hold after activity"
            assert check.fast_poll_reason(now=T0 + 300 + 301) is None

            await feed(700, displayCode="DC_MODE_CYCLE")  # idle status, display not idle
            assert check.fast_poll_reason(now=T0 + 5000) == "displayCode DC_MODE_CYCLE"
            await feed(730, globeMotorRetractFaultStatus="FAULT_OVERTORQUE_AMP")
            assert "globeRetractFault" in check.fast_poll_reason(now=T0 + 5000)
            await feed(760, robotCycleState="CYCLE_STATE_FAULT_RETRY")
            assert check.fast_poll_reason(now=T0 + 5000) == "cycleState CYCLE_STATE_FAULT_RETRY"

            await feed(790)
            await feed(790 + 301)
            assert check.fast_poll_reason(now=T0 + 790 + 301) is None
            assert check.interval_override is None
        assert "Fast polling (30s) on for COPTiP: globeMotorFault FAULT_TIMEOUT" in caplog.text
        assert "Fast polling off for COPTiP" in caplog.text
        await check.db.disconnect()
    asyncio.run(scenario())


# ── detector-level wording ───────────────────────────────────────────────────

def _feed(det, t, status="READY", **raw):
    return det.process(extract_fields(make_raw(**raw), status), float(t))


def test_litter_drop_without_fault_is_not_called_a_failed_cycle():
    det = Detector("COPTiP", DetectionConfig())
    low = dict(litterLevelPercentage=0.4, litterLevelState="REFILL")
    _feed(det, 0)
    assert _feed(det, 300, **low) == []                    # no fault: still needs confirmation
    (alert,) = _feed(det, 600, **low)
    assert alert.key == "litter_dump"
    assert "litter level dropped 90%→40% — litter missing from the globe" in alert.text
    assert "failed cycle" not in alert.text
    assert "dumped into the waste drawer" not in alert.text


def test_drawer_jump_alone_says_dumped_into_the_drawer():
    det = Detector("COPTiP", DetectionConfig())
    _feed(det, 0, DFILevelPercent=16)
    _feed(det, 300, DFILevelPercent=75)
    (alert,) = _feed(det, 600, DFILevelPercent=75)
    assert "litter dumped into the waste drawer (waste drawer 16%→75% in 5 min)" in alert.text


def test_empty_cycle_alone_does_not_claim_the_drawer():
    det = Detector("COPTiP", DetectionConfig())
    _feed(det, 0, odometerEmptyCycles=3)
    (alert,) = _feed(det, 60, odometerEmptyCycles=4)
    assert "the globe may have been emptied (empty-cycle counter +1 (3→4))" in alert.text
    assert "into the" not in alert.text


def test_open_litter_low_is_dropped_silently_and_returns_if_litter_stays_low():
    det = Detector("COPTiP", DetectionConfig())
    low = dict(litterLevelPercentage=0.7, litterLevelState="LOW")
    _feed(det, 0)
    _feed(det, 300, **low)
    assert [a.key for a in _feed(det, 600, **low)] == ["litter_low"]
    # the drawer jumps: the dump incident takes over, no "litter level is OK again"
    gone = dict(litterLevelPercentage=0.6, litterLevelState="REFILL", DFILevelPercent=75)
    _feed(det, 900, **gone)
    alerts = _feed(det, 1200, **gone)
    assert [(a.key, a.kind) for a in alerts] == [("litter_dump", "new")]
    assert set(det.incidents) == {"litter_dump"}
    assert _feed(det, 1500, **gone) == []
    # drawer emptied but nobody refilled the globe: the dump incident stays open
    # (and keeps litter_low suppressed) — see test_dump_resolution.py
    emptied = dict(litterLevelPercentage=0.6, litterLevelState="REFILL", DFILevelPercent=5)
    assert _feed(det, 1800, **emptied) == []
    assert _feed(det, 2100, **emptied) == []
    assert set(det.incidents) == {"litter_dump"}


def test_incident_persisted_by_the_previous_version_keeps_a_truthful_wording():
    """The live detector state may hold an incident opened before this change."""
    old = {"since": 0.0, "last_alert": 0.0, "count": 1, "waste_before": None, "litter_min": 40.0,
           "reasons": ["litter level 90%→40% in 2 min"]}
    det = Detector("COPTiP", DetectionConfig(), {"incidents": {"litter_dump": dict(old)}})
    assert "into the" not in det._dump_text(det.incidents["litter_dump"])
    old["reasons"] = old["reasons"] + ["waste drawer 16%→75% in 5 min"]
    assert "litter dumped into the waste drawer" in det._dump_text(old)


# ── websocket watchdog ───────────────────────────────────────────────────────

class WsRobot:
    id, name = RID, "COPTiP"

    def __init__(self, last_frame_epoch):
        self.calls = []
        self._transport = SimpleNamespace(
            _last_received=datetime.fromtimestamp(last_frame_epoch, tz=timezone.utc))

    async def unsubscribe(self):
        self.calls.append("unsubscribe")

    async def subscribe(self):
        self.calls.append("subscribe")


def test_silent_websocket_is_resubscribed_and_health_is_logged(tmp_path, caplog):
    async def scenario():
        check, _tg = make_check(tmp_path)
        now = T0 + 10_000
        robot = WsRobot(now - 30)                       # keep-alive 30 s ago: healthy
        check.robots = {RID: robot}
        check._ws_since = {RID: now - 3600}
        check._ws_stats.update(pushes=2, pushes_new_data=1, poll_first=7)
        with caplog.at_level(logging.INFO):
            assert await check._check_ws_health(now) is False
            assert robot.calls == []
            assert ("Websocket health: last frame 30s ago; 2 push(es), 1 with new data; "
                    "7 change(s) seen first by polling") in caplog.text

            later = now + lr.WS_STALE_SECONDS + 60      # no frame at all since then
            assert await check._check_ws_health(later) is True
            assert robot.calls == ["unsubscribe", "subscribe"]
            assert "re-subscribing; polling continues" in caplog.text
            # not hammered: at most one re-subscribe per WS_RESUBSCRIBE_MIN_SECONDS
            assert await check._check_ws_health(later + 200) is False
            assert robot.calls == ["unsubscribe", "subscribe"]
    asyncio.run(scenario())
