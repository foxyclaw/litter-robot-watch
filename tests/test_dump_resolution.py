"""A litter-dump incident ends when the litter is back — not when the drawer is emptied.

Seen live 2026-11-19 09:46 PST: during a deep clean the owner emptied the waste
drawer (75% -> 12%) and the open dump incident auto-resolved with "waste drawer
back to 12%" although the globe still had no litter (32%, REFILL).
"""
import asyncio
import logging

from conftest import make_raw

from checks.litter_robot import LitterRobotCheck
from detection import DetectionConfig, Detector, extract_fields

RID = "robot-1"
T0 = 1_700_000_000.0
MIN = 60


def hms(text):
    h, m, s = (int(x) for x in text.split(":"))
    return h * 3600 + m * 60 + s


def feed(det, t, status="READY", **raw):
    return det.process(extract_fields(make_raw(**raw), status), float(t))


def keys(alerts):
    return [(a.key, a.kind) for a in alerts]


OK = dict(DFILevelPercent=16, litterLevelPercentage=0.9, litterLevelState="OPTIMAL")
STAGE1 = dict(DFILevelPercent=16, litterLevelPercentage=0.4, litterLevelState="REFILL")
DUMPED = dict(DFILevelPercent=75, litterLevelPercentage=0.36, litterLevelState="REFILL")
EMPTIED = dict(DFILevelPercent=12, litterLevelPercentage=0.32, litterLevelState="REFILL")
REFILLED = dict(DFILevelPercent=12, litterLevelPercentage=0.9, litterLevelState="OPTIMAL")


def open_dump(det, t0=hms("06:40:00")):
    """The morning of 2026-11-19, shortened: litter 90->40 (stage 1), drawer 16->75 (stage 2)."""
    feed(det, t0, **OK)
    feed(det, t0 + 5 * MIN, **STAGE1)
    assert keys(feed(det, t0 + 10 * MIN, **STAGE1)) == [("litter_dump", "new")]
    assert keys(feed(det, t0 + 83 * MIN, **DUMPED)) == [("litter_dump", "update")]     # 08:03
    assert det.incidents["litter_dump"]["waste_before"] == 16


def test_20261119_emptying_the_drawer_does_not_resolve_the_dump_incident(caplog):
    det = Detector("COPTiP", DetectionConfig())
    open_dump(det)
    det.incidents["litter_dump"]["last_alert"] = hms("09:39:07")    # last reminder, as logged

    cycling = dict(robotStatus="ROBOT_CLEAN", robotCycleState="CYCLE_STATE_PROCESS",
                   displayCode="DC_MODE_CYCLE")
    replay = [   # 09:40-09:47 as logged: deep clean, bonnet back on, a cycle, drawer emptied
        ("09:40:00", "OFFLINE", dict(DUMPED, isOnline=False)),
        ("09:43:55", "BONNET_REMOVED", dict(DUMPED, isBonnetRemoved=True, robotStatus="ROBOT_BONNET",
                                            displayCode="DC_BONNET_OFF")),
        ("09:44:16", "CAT_DETECTED", dict(DUMPED, catDetect="CAT_DETECT", odometerPowerCycles=79)),
        ("09:44:46", "CLEAN_CYCLE", dict(DUMPED, **cycling, robotCycleStatus="CYCLE_DUMP")),
        ("09:45:47", "CLEAN_CYCLE", dict(DUMPED, **cycling, robotCycleStatus="CYCLE_LEVEL",
                                         DFILevelPercent=12)),                 # waste% 75 -> 12
        ("09:46:18", "CLEAN_CYCLE", dict(DUMPED, **cycling, robotCycleStatus="CYCLE_LEVEL",
                                         DFILevelPercent=12)),
        ("09:46:48", "READY", EMPTIED),                                        # litter% 36 -> 32
        ("09:47:19", "READY", EMPTIED),
        ("09:47:49", "READY", EMPTIED),
    ]
    alerts = []
    with caplog.at_level(logging.INFO, logger="detection"):
        for t, status, raw in replay:
            alerts += feed(det, hms(t), status, **raw)

    assert all(a.kind != "resolved" for a in alerts if a.key == "litter_dump")
    assert not any("cleared" in a.text or "waste drawer back" in a.text for a in alerts)
    assert all(a.key != "litter_low" for a in alerts)          # still covered by the dump incident
    assert "litter_dump" in det.incidents
    # the emptied drawer is logged — once — and nothing is sent about it
    assert caplog.text.count("waste drawer emptied (12%)") == 1
    assert "incident stays open until the litter is refilled" in caplog.text

    # an hour after the last reminder the incident still nags
    (reminder,) = feed(det, hms("10:39:10"), **EMPTIED)
    assert (reminder.key, reminder.kind) == ("litter_dump", "repeat")

    # the globe is refilled: now — and only now — the incident resolves
    (done,) = feed(det, hms("11:15:00"), **REFILLED)
    assert (done.key, done.kind) == ("litter_dump", "resolved")
    assert "litter-dump alert cleared (litter level back to 90%)" in done.text
    assert not done.urgent
    assert det.incidents == {}
    assert feed(det, hms("11:20:00"), **REFILLED) == []


def test_resolves_on_configurable_percent_or_non_low_state_and_only_at_rest():
    # state still REFILL but % above the configured threshold
    det = Detector("COPTiP", DetectionConfig(dump_resolve_litter_percent=60))
    open_dump(det)
    t = hms("09:00:00")
    assert feed(det, t, DFILevelPercent=12, litterLevelPercentage=0.59, litterLevelState="REFILL") == []
    (done,) = feed(det, t + 30, DFILevelPercent=12, litterLevelPercentage=0.6, litterLevelState="REFILL")
    assert done.kind == "resolved" and "litter level back to 60%" in done.text

    # default threshold 80: 70% REFILL is not enough, but a non-low state is
    det = Detector("COPTiP", DetectionConfig())
    open_dump(det)
    assert det.cfg.dump_resolve_litter_percent == 80
    assert feed(det, t, DFILevelPercent=12, litterLevelPercentage=0.7, litterLevelState="REFILL") == []
    (done,) = feed(det, t + 30, DFILevelPercent=12, litterLevelPercentage=0.7, litterLevelState="OPTIMAL")
    assert done.kind == "resolved" and "litter level back to 70%" in done.text

    # a mid-cycle reading is not trusted
    det = Detector("COPTiP", DetectionConfig())
    open_dump(det)
    assert feed(det, t, "CLEAN_CYCLE", **REFILLED, robotCycleStatus="CYCLE_DUMP") == []
    assert "litter_dump" in det.incidents
    assert keys(feed(det, t + 60, **REFILLED)) == [("litter_dump", "resolved")]

    assert DetectionConfig.from_config({"dump_resolve_litter_percent": 65}).dump_resolve_litter_percent == 65


def drawer_only(det, **extra):
    """Drawer 16% -> 75% while the litter reads 90% OPTIMAL throughout."""
    feed(det, 0, DFILevelPercent=16)
    feed(det, 5 * MIN, DFILevelPercent=75, **extra)
    assert keys(feed(det, 10 * MIN, DFILevelPercent=75, **extra)) == [("litter_dump", "new")]


def test_drawer_jump_only_incident_resolves_when_the_drawer_reading_drops_back():
    """Litter never read low, no globe fault: a drawer jump on its own is most
    likely sensor noise, so the reading dropping back clears it (as before)."""
    det = Detector("COPTiP", DetectionConfig())
    drawer_only(det)
    assert feed(det, 15 * MIN, DFILevelPercent=75) == []        # litter OPTIMAL: nothing to restore
    assert feed(det, 16 * MIN, DFILevelPercent=50) == []        # not back yet (> 16 + 25/2)
    # a mid-cycle reading is not trusted
    assert feed(det, 17 * MIN, "CLEAN_CYCLE", DFILevelPercent=0, robotCycleStatus="CYCLE_DUMP") == []
    assert "litter_dump" in det.incidents
    (done,) = feed(det, 20 * MIN, DFILevelPercent=18)
    assert (done.key, done.kind, done.urgent) == ("litter_dump", "resolved", False)
    assert "waste drawer back to 18%" in done.text and "never read low" in done.text
    assert det.incidents == {}


def test_drawer_jump_incident_with_litter_seen_low_only_resolves_on_litter_restored():
    # litter turns out low AFTER the incident was opened by the drawer jump alone
    det = Detector("COPTiP", DetectionConfig())
    drawer_only(det)
    low = dict(litterLevelPercentage=0.75, litterLevelState="LOW")      # no 25-point drop
    assert feed(det, 15 * MIN, DFILevelPercent=75, **low) == []
    assert det.incidents["litter_dump"]["litter_was_low"] is True
    assert feed(det, 20 * MIN, DFILevelPercent=12, **low) == []         # drawer emptied: stays open
    assert feed(det, 25 * MIN, DFILevelPercent=12, **low) == []
    assert "litter_dump" in det.incidents
    (done,) = feed(det, 30 * MIN, DFILevelPercent=12)                   # 90% OPTIMAL again
    assert done.kind == "resolved" and "litter level back to 90%" in done.text

    # litter is first seen low on the very reading where the drawer drops back
    det = Detector("COPTiP", DetectionConfig())
    drawer_only(det)
    assert feed(det, 20 * MIN, DFILevelPercent=12, **low) == []
    assert "litter_dump" in det.incidents

    # litter already low when the drawer jumped
    det = Detector("COPTiP", DetectionConfig())
    feed(det, 0, DFILevelPercent=16, **low)
    feed(det, 5 * MIN, DFILevelPercent=75, **low)
    assert ("litter_dump", "new") in keys(feed(det, 10 * MIN, DFILevelPercent=75, **low))
    assert all(a.key != "litter_dump" for a in feed(det, 20 * MIN, DFILevelPercent=12, **low))
    assert "litter_dump" in det.incidents


def test_drawer_jump_incident_with_a_globe_fault_or_other_evidence_is_not_noise():
    fault = dict(globeMotorFaultStatus="FAULT_OVER_TORQUE")
    # fault seen while the incident is open
    det = Detector("COPTiP", DetectionConfig())
    drawer_only(det)
    feed(det, 12 * MIN, DFILevelPercent=75, **fault)
    assert all(a.key != "litter_dump" for a in feed(det, 20 * MIN, DFILevelPercent=12))
    assert det.incidents["litter_dump"]["fault_seen"] is True

    # fault just before the jump (the "failed cycle" case: alert without confirmation)
    det = Detector("COPTiP", DetectionConfig())
    feed(det, 0, DFILevelPercent=16, **fault)
    assert ("litter_dump", "new") in keys(feed(det, 5 * MIN, DFILevelPercent=75))
    assert all(a.key != "litter_dump" for a in feed(det, 20 * MIN, DFILevelPercent=12))
    assert "litter_dump" in det.incidents

    # empty-cycle counter went up as well
    det = Detector("COPTiP", DetectionConfig())
    drawer_only(det)
    assert keys(feed(det, 12 * MIN, DFILevelPercent=75, odometerEmptyCycles=4)) == [("litter_dump", "update")]
    assert feed(det, 20 * MIN, DFILevelPercent=12, odometerEmptyCycles=4) == []
    assert "litter_dump" in det.incidents

    # incident persisted by an older version (no ``fault_seen``): stay conservative
    det = Detector("COPTiP", DetectionConfig())
    drawer_only(det)
    del det.incidents["litter_dump"]["fault_seen"]
    assert feed(det, 20 * MIN, DFILevelPercent=12) == []
    assert "litter_dump" in det.incidents


def test_cap_closes_the_incident_and_litter_low_takes_over():
    cfg = DetectionConfig(dump_repeat_seconds=3600, dump_max_alert_seconds=86400)
    det = Detector("COPTiP", cfg)
    open_dump(det, t0=0)
    start = det.incidents["litter_dump"]["since"]
    seen = []
    for t in range(hms("08:05:00"), int(start) + 86400, 10 * MIN):
        seen += feed(det, t, **EMPTIED)
    assert seen and {(a.key, a.kind) for a in seen} == {("litter_dump", "repeat")}
    # 24 h: the dump incident is dropped without a "resolved" message ...
    end = start + 86400
    assert feed(det, end, **EMPTIED) == []
    assert "litter_dump" not in det.incidents
    # ... and the still-low litter is reported by litter_low after its usual confirmation
    assert feed(det, end + 4 * MIN, **EMPTIED) == []
    (low,) = feed(det, end + 5 * MIN, **EMPTIED)
    assert (low.key, low.kind) == ("litter_low", "new")
    assert "litter level is <b>REFILL</b> (32%)" in low.text


def test_ack_hands_over_to_litter_low_while_litter_is_still_low():
    det = Detector("COPTiP", DetectionConfig())
    open_dump(det, t0=0)
    t = 90 * MIN
    assert feed(det, t, **EMPTIED) == []                    # open, not acked: suppressed
    assert "litter_low" not in det.state["cond_since"]
    assert det.acknowledge() == ["litter_dump"]
    assert feed(det, t + 60, **EMPTIED) == []
    (low,) = feed(det, t + 60 + 5 * MIN, **EMPTIED)
    assert (low.key, low.kind) == ("litter_low", "new")
    # refilled: both close
    done = feed(det, t + 3600, **REFILLED)
    assert sorted(keys(done)) == [("litter_dump", "resolved"), ("litter_low", "resolved")]
    assert det.incidents == {}


# ── after deploy: the live incident was already (wrongly) closed ─────────────

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


def test_restart_with_no_open_dump_incident_and_litter_refill_raises_litter_low(tmp_path):
    """Detector state as persisted in kv_state after the wrong auto-resolve:
    no incidents, last reading READY / waste 12% / litter 32% REFILL."""
    cfg = {
        "database": str(tmp_path / "test.db"),
        "forensics": {"jsonl_dir": str(tmp_path / "data"), "retention_days": 90},
        "realtime": {"websocket": False},
        "alerts": {"waste_threshold": 90},
    }
    raw = make_raw(**EMPTIED, odometerCleanCycles=3762)
    state = {"id": RID, "name": raw["name"], "status": "READY",
             "waste_drawer_level": 12, "is_waste_drawer_full": False}
    t = hms("09:50:00")

    async def scenario():
        old = LitterRobotCheck(cfg, notifier=FakeTelegram())
        await old._init_db()
        await old.db.set_kv(f"detector:{RID}", {
            "last": dict(extract_fields(raw, "READY"), ts=T0 + t),
            "steps": {"waste": {"history": [[T0 + t - 60, 75.0], [T0 + t, 12.0]], "pending": None},
                      "litter": {"history": [[T0 + t - 60, 36.0], [T0 + t, 32.0]], "pending": None}},
            "cond_since": {}, "incidents": {},
        })
        await old.db.disconnect()

        tg = FakeTelegram()
        check = LitterRobotCheck(cfg, notifier=tg)          # the restarted service
        await check._init_db()
        first = await check.process_snapshot(RID, state, raw, source="poll", now=T0 + t + 30)
        assert first == [] and tg.reliable == []            # confirmation period
        got = []
        for dt in range(60, 6 * MIN, 30):
            got += await check.process_snapshot(RID, state, raw, source="poll", now=T0 + t + dt)
        assert [(a.key, a.kind) for a in got] == [("litter_low", "new")]
        assert "litter level is <b>REFILL</b> (32%)" in got[0].text
        assert len(tg.reliable) == 1 and "litter level is" in tg.reliable[0]
        assert "litter_dump" not in check.detectors[RID].incidents
        await check.wait_background()
        await check.db.disconnect()
    asyncio.run(scenario())
