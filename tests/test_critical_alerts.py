"""Critical incidents: DM fan-out (group + telegram.alert_dm_user_ids) and the
forensic incident file. Everything is offline — sends are scripted/mocked."""
import asyncio
import json
import logging

from conftest import make_raw

from checks.litter_robot import LitterRobotCheck
from detection import is_critical
from notifier import TelegramNotifier

RID = "robot-1"
T0 = 1_800_000_000.0
MIN = 60
GROUP = "-100"
ALICE, BOB = "1001", "1002"      # fake Telegram user ids

OK = (True, False, 0.0, 200)
DOWN = (False, True, 0.0, None)
FORBIDDEN = (False, False, 0.0, 403)


class ScriptedNotifier(TelegramNotifier):
    """Real notifier logic, scripted network: ``replies[chat_id]`` is a list of
    (ok, retryable, retry_after, http_status) consumed per attempt (default OK)."""

    def __init__(self, replies=None, dm=(ALICE, BOB), **kw):
        super().__init__("TOKEN", GROUP, **kw)
        self.alert_dm_user_ids = list(dm)
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.sent, self.attempts = [], []

    async def _post(self, message, chat_id=None):
        target = str(chat_id or self.chat_id)
        self.attempts.append(target)
        script = self.replies.get(target)
        ok, retryable, retry_after, status = script.pop(0) if script else OK
        self.last_status = status
        if ok:
            self.sent.append((target, message))
        return ok, retryable, retry_after

    async def send_waste_alert(self, robot_name, level):
        return await self.send_reliable(f"WASTE {robot_name} {level}%")

    def to(self, chat_id):
        return [m for c, m in self.sent if c == chat_id]


def make_check(tmp_path, tg=None, alerts=None):
    cfg = {
        "database": str(tmp_path / "test.db"),
        "forensics": {"jsonl_dir": str(tmp_path / "data"), "retention_days": 90},
        "realtime": {"websocket": False},
        "alerts": {"waste_threshold": 90, "dump_confirm_seconds": 0,
                   "dump_repeat_seconds": 1800, **(alerts or {})},
    }
    tg = tg or ScriptedNotifier()
    return LitterRobotCheck(cfg, notifier=tg), tg


async def feed(check, t, status="READY", **overrides):
    raw = make_raw(**overrides)
    state = {
        "id": RID, "name": raw["name"], "status": status, "status_description": status,
        "waste_drawer_level": raw["DFILevelPercent"], "is_waste_drawer_full": raw["isDFIFull"],
    }
    return await check.process_snapshot(RID, state, raw, source="poll", now=T0 + t)


def run(coro):
    return asyncio.run(coro)


def incident_files(tmp_path):
    return sorted((tmp_path / "data" / "incidents").glob("*.json"))


class FakeRobot:
    """Just enough of a pylitterbot Robot for the read-only activity query."""
    serial, name, id = "LR4TEST", "COPTiP", RID

    def __init__(self, fail=False):
        self.fail, self.queries = fail, []

    async def _post(self, json=None):
        self.queries.append(json)
        if self.fail:
            raise ConnectionError("cloud down")
        return {"data": {"getLitterRobot4Activity": [
            {"timestamp": "2027-01-15T08:10:00.000000Z", "value": "robotCycleStatusDump",
             "actionValue": "", "measure": "robotCycleStatus", "commandSource": "robot"},
        ]}}


# ── which incidents are critical ─────────────────────────────────────────────

def test_critical_classification():
    for key in ("litter_dump", "globe_motor_fault", "globe_retract_fault", "pinch", "offline",
                "api_unreachable",
                "status:OVER_TORQUE_FAULT", "status:DUMP_POSITION_FAULT", "status:CAT_SENSOR_FAULT"):
        assert is_critical(key), key
    for key in ("litter_low", "usb_fault", "status:BONNET_REMOVED",
                "status:PAUSED", "status:DRAWER_FULL", "waste_pct"):
        assert not is_critical(key), key


# ── 1. DM fan-out ────────────────────────────────────────────────────────────

def test_dump_alert_goes_to_group_and_dms_on_first_alert_reminder_and_resolved(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        await feed(check, 0, DFILevelPercent=19)
        await feed(check, 5 * MIN, DFILevelPercent=73)              # dump -> first alert
        for chat in (GROUP, ALICE, BOB):
            assert len(tg.to(chat)) == 1 and "🚨" in tg.to(chat)[0] and "19%→73%" in tg.to(chat)[0]
        low = dict(litterLevelPercentage=0.7, litterLevelState="LOW")
        await feed(check, 20 * MIN, DFILevelPercent=73, **low)      # < 30 min: quiet
        assert len(tg.sent) == 3
        await feed(check, 36 * MIN, DFILevelPercent=73, **low)      # reminder
        for chat in (GROUP, ALICE, BOB):
            assert len(tg.to(chat)) == 2 and "Reminder" in tg.to(chat)[1]
        await feed(check, 38 * MIN, DFILevelPercent=5, **low)       # drawer emptied: says nothing
        assert len(tg.sent) == 6
        await feed(check, 40 * MIN, DFILevelPercent=5)              # litter refilled -> resolved
        for chat in (GROUP, ALICE, BOB):
            assert len(tg.to(chat)) == 3 and tg.to(chat)[2].startswith("✅")
            assert "litter level back to 90%" in tg.to(chat)[2]
        await check.wait_background()
        await check.db.disconnect()
    run(scenario())


def test_faults_pinch_hard_status_and_offline_are_dmed(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path, alerts={"offline_minutes": 0})
        await check._init_db()
        await feed(check, 0)
        await feed(check, 1 * MIN, globeMotorFaultStatus="FAULT_TIMEOUT")
        await feed(check, 2 * MIN, globeMotorRetractFaultStatus="FAULT_TIMEOUT")
        await feed(check, 3 * MIN, "PINCH_DETECT")
        await feed(check, 4 * MIN, "OVER_TORQUE_FAULT")
        await feed(check, 5 * MIN, "OFFLINE", isOnline=False)
        dms = "\n".join(tg.to(ALICE))
        for needle in ("globe motor fault", "globe motor retract fault", "pinch detected",
                       "OVER_TORQUE_FAULT", "OFFLINE"):
            assert needle in dms, needle
        assert tg.to(ALICE) == tg.to(BOB) == tg.to(GROUP)           # incl. the ✅ resolved ones
        assert any(m.startswith("✅") for m in tg.to(ALICE))
        await check.wait_background()
        await check.db.disconnect()
    run(scenario())


def test_routine_alerts_are_not_dmed(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path, alerts={"status_grace_seconds": 0})
        await check._init_db()
        await feed(check, 0, DFILevelPercent=85)
        await feed(check, 5 * MIN, DFILevelPercent=92)                         # waste drawer >= 90%
        await feed(check, 10 * MIN, DFILevelPercent=92, litterLevelState="LOW")  # low litter
        await feed(check, 15 * MIN, DFILevelPercent=92, litterLevelState="LOW")
        await feed(check, 20 * MIN, DFILevelPercent=92, USBFaultStatus="FAULT")  # usb (non-urgent)
        await feed(check, 25 * MIN, "BONNET_REMOVED", DFILevelPercent=92)
        group = "\n".join(tg.to(GROUP))
        assert "WASTE COPTiP 92%" in group and "litter level is" in group
        assert "USB power fault" in group and "Bonnet removed" in group
        assert tg.to(ALICE) == [] and tg.to(BOB) == []
        assert incident_files(tmp_path) == []                       # and no incident file either
        await check.db.disconnect()
    run(scenario())


def test_no_dm_ids_configured_means_group_only(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path, tg=ScriptedNotifier(dm=()))
        await check._init_db()
        await feed(check, 0)
        await feed(check, 5 * MIN, globeMotorFaultStatus="FAULT_TIMEOUT")
        assert tg.attempts == [GROUP]
        await check.wait_background()
        await check.db.disconnect()
    run(scenario())


def test_dm_403_warns_once_per_user_per_day_and_is_never_queued(caplog):
    async def scenario():
        n = ScriptedNotifier({BOB: [FORBIDDEN] * 5})
        with caplog.at_level(logging.WARNING):
            for i in range(3):
                assert await n.send_critical(f"🚨 alert {i}") is True
        assert len(n.to(GROUP)) == 3 and len(n.to(ALICE)) == 3 and n.to(BOB) == []
        assert n.attempts.count(BOB) == 3       # tried once per alert, not hammered
        assert n.pending == 0                   # ... and never put in the retry queue
        warnings = [r for r in caplog.records if "403" in r.getMessage()]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        assert BOB in warnings[0].getMessage()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        # next day: warned again, once
        n._forbidden_logged[BOB] = "2000-01-01"
        with caplog.at_level(logging.WARNING):
            await n.send_critical("🚨 next day")
            await n.send_critical("🚨 next day 2")
        assert len([r for r in caplog.records if "403" in r.getMessage()]) == 2
    run(scenario())


def test_403_from_the_group_chat_is_still_an_error(caplog):
    async def scenario():
        n = ScriptedNotifier({GROUP: [FORBIDDEN]}, dm=())
        with caplog.at_level(logging.WARNING):
            assert await n.send_critical("🚨") is False
        assert [r for r in caplog.records if r.levelno >= logging.ERROR]
    run(scenario())


def test_dms_use_the_retry_outbox_during_an_outage(tmp_path, monkeypatch):
    import notifier as notifier_mod
    monkeypatch.setattr(notifier_mod, "backoff_delay", lambda attempt, **kw: 0.01)

    async def scenario():
        outbox = tmp_path / "outbox.json"
        n = ScriptedNotifier({GROUP: [DOWN, DOWN], ALICE: [DOWN], BOB: [DOWN, FORBIDDEN]},
                             outbox_path=str(outbox))
        assert await n.send_critical("🚨 dump") is False
        assert n.pending == 3 and n.sent == []
        assert sorted(i["chat_id"] for i in json.loads(outbox.read_text())) == sorted([GROUP, ALICE, BOB])
        for _ in range(300):
            await asyncio.sleep(0.01)
            if n.pending == 0:
                break
        assert n.pending == 0                   # BOB's queued DM hit 403 -> dropped, not retried forever
        assert n.to(GROUP) == ["🚨 dump"] and n.to(ALICE) == ["🚨 dump"] and n.to(BOB) == []
    run(scenario())


# ── 2. incident snapshot file ────────────────────────────────────────────────

def test_incident_file_is_written_on_open_and_completed_on_resolve(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        robot = FakeRobot()
        check.robots = {RID: robot}
        await check.db.store_activities(RID, [
            {"timestamp": f"2027-01-15 07:{i:02d}:00", "action": "catDetect", "action_value": str(i),
             "raw": {"n": i}} for i in range(60)
        ])
        await feed(check, -45 * MIN, DFILevelPercent=18)            # older than the 30 min window
        await feed(check, -20 * MIN, DFILevelPercent=19)
        await feed(check, -10 * MIN, "CLEAN_CYCLE", DFILevelPercent=0, robotCycleStatus="CYCLE_DUMP")
        assert incident_files(tmp_path) == []
        await feed(check, 0, DFILevelPercent=73, someFutureWhiskerKey={"x": 1})   # incident opens
        await check.wait_background()

        files = incident_files(tmp_path)
        assert [f.name for f in files] == ["20270115-080000-litter_dump.json"]
        doc = json.loads(files[0].read_text())
        assert doc["incident"] == "litter_dump" and doc["robot_id"] == RID
        assert "19%→73%" in doc["trigger"]["reason"]
        assert doc["trigger"]["details"]["waste_before"] == 19
        assert doc["current_raw"] == make_raw(DFILevelPercent=73, someFutureWhiskerKey={"x": 1})
        # last 30 minutes of raw snapshots, oldest first, full dicts
        assert [s["data"]["DFILevelPercent"] for s in doc["raw_snapshots"]] == [19, 0, 73]
        assert doc["raw_snapshots"][1]["status"] == "CLEAN_CYCLE"
        assert doc["raw_snapshots"][-1]["data"]["someFutureWhiskerKey"] == {"x": 1}
        # last 50 stored activity entries, newest first, with their raw rows
        assert len(doc["activity_history"]) == 50
        assert doc["activity_history"][0]["action_value"] == "59"
        assert doc["activity_history"][0]["raw"] == {"n": 59}
        # fresh activity fetched right after opening — one read-only GraphQL query
        assert len(robot.queries) == 1 and robot.queries[0]["query"].lstrip().startswith("query")
        assert "mutation" not in robot.queries[0]["query"]
        assert doc["activity_fresh"]["entries"][0]["action"] == "robotCycleStatusDump"
        assert doc["activity_fresh"]["new_entries"] == 1
        assert doc["resolved"] is None and [a["kind"] for a in doc["alerts"]] == ["new"]

        await feed(check, 31 * MIN, DFILevelPercent=73,             # reminder; litter reads low
                   litterLevelPercentage=0.7, litterLevelState="LOW")
        await feed(check, 40 * MIN, DFILevelPercent=4)              # litter back -> resolved
        await check.wait_background()
        files = incident_files(tmp_path)
        assert len(files) == 1                                      # same file, updated in place
        doc = json.loads(files[0].read_text())
        assert [a["kind"] for a in doc["alerts"]] == ["new", "repeat", "resolved"]
        assert "cleared" in doc["resolved"]["reason"]
        assert doc["resolved"]["current_raw"]["DFILevelPercent"] == 4
        assert doc["resolved"]["raw_snapshots"][-1]["data"]["DFILevelPercent"] == 4
        assert doc["activity_fresh"]["new_entries"] == 1            # earlier sections kept
        assert len(robot.queries) == 1
        assert await check.db.get_kv(f"incident_file:{RID}:litter_dump") is None
        await check.db.disconnect()
    run(scenario())


def test_incident_file_survives_failed_activity_fetch_and_few_snapshots(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        check.robots = {RID: FakeRobot(fail=True)}

        async def no_history(limit=100):
            raise ConnectionError("cloud down")
        check.robots[RID].get_activity_history = no_history
        await feed(check, 0, globeMotorFaultStatus="FAULT_TIMEOUT")   # very first snapshot ever
        await check.wait_background()
        (path,) = incident_files(tmp_path)
        assert path.name == "20270115-080000-globe_motor_fault.json"
        doc = json.loads(path.read_text())
        assert len(doc["raw_snapshots"]) == 1 and doc["activity_history"] == []
        assert "cloud down" in doc["activity_fresh"]["error"]
        assert len(tg.to(GROUP)) == 1 and len(tg.to(ALICE)) == 1    # alerting unaffected
        await check.db.disconnect()
    run(scenario())


def test_resolve_without_an_open_file_writes_a_resolved_file(tmp_path):
    async def scenario():
        check, _tg = make_check(tmp_path)
        await check._init_db()
        await feed(check, 0, "OVER_TORQUE_FAULT")
        await check.wait_background()
        (path,) = incident_files(tmp_path)
        assert path.name == "20270115-080000-status-OVER_TORQUE_FAULT.json"
        path.unlink()                                               # e.g. cleaned up by hand
        await feed(check, 5 * MIN, "READY")
        names = [f.name for f in incident_files(tmp_path)]
        assert names == ["20270115-080500-status-OVER_TORQUE_FAULT-resolved.json"]
        doc = json.loads(incident_files(tmp_path)[0].read_text())
        assert doc["resolved"]["current_raw"]["robotStatus"] == "ROBOT_IDLE"
        await check.db.disconnect()
    run(scenario())


def test_dry_run_writes_no_incident_file_and_sends_no_dm(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        check.dry_run = True
        await check._init_db()
        alerts = await feed(check, 0, globeMotorFaultStatus="FAULT_TIMEOUT")
        assert [a.key for a in alerts] == ["globe_motor_fault"]
        assert tg.attempts == [] and incident_files(tmp_path) == []
        await check.db.disconnect()
    run(scenario())


def test_cloud_unreachable_is_dmed_and_gets_an_incident_file(tmp_path, monkeypatch):
    import checks.litter_robot as mod

    async def scenario():
        check, tg = make_check(tmp_path, alerts={"offline_minutes": 30})
        await check._init_db()
        await feed(check, 0)
        clock = [T0]
        monkeypatch.setattr(mod.time, "time", lambda: clock[0])
        await check._note_api(reachable=False)                      # outage starts
        assert tg.sent == []
        clock[0] = T0 + 31 * MIN
        await check._note_api(reachable=False)                      # > 30 min: alert
        for chat in (GROUP, ALICE, BOB):
            assert len(tg.to(chat)) == 1 and "Whisker" in tg.to(chat)[0]
        (path,) = incident_files(tmp_path)
        assert path.name.endswith("-api_unreachable.json")
        clock[0] = T0 + 40 * MIN
        await check._note_api(reachable=True)
        assert tg.to(ALICE)[-1].startswith("✅")
        assert json.loads(path.read_text())["resolved"]["reason"].startswith("✅")
        await check.db.disconnect()
    run(scenario())
