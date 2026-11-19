"""End-to-end (offline) tests of LitterRobotCheck.process_snapshot:
storage, change log, waste alert behaviour, dump alert delivery."""
import asyncio
import gzip
import json
import logging
import sqlite3
from datetime import datetime, timezone

import pytest
from conftest import make_raw

from checks.litter_robot import LitterRobotCheck
from forensics import SnapshotLog

RID = "robot-1"
T0 = 1_800_000_000.0   # arbitrary fixed epoch
MIN = 60


class FakeTelegram:
    def __init__(self):
        self.waste, self.reliable = [], []
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


def make_check(tmp_path, alerts=None, dry_run=False):
    cfg = {
        "database": str(tmp_path / "test.db"),
        "forensics": {"jsonl_dir": str(tmp_path / "data"), "retention_days": 90},
        "realtime": {"websocket": False},
        "alerts": {"waste_threshold": 90, "waste_repeat_seconds": 10800, **(alerts or {})},
    }
    tg = FakeTelegram()
    return LitterRobotCheck(cfg, notifier=tg, dry_run=dry_run), tg


def state_for(raw, status):
    return {
        "id": RID, "name": raw["name"], "status": status, "status_description": status,
        "waste_drawer_level": raw["DFILevelPercent"], "is_waste_drawer_full": raw["isDFIFull"],
        "litter_level": raw["litterLevelPercentage"] * 100,
    }


async def feed(check, t, status="READY", **overrides):
    raw = make_raw(**overrides)
    return await check.process_snapshot(RID, state_for(raw, status), raw, source="poll", now=T0 + t)


def run(coro):
    return asyncio.run(coro)


def test_nov10_replay_sends_urgent_telegram_alert(tmp_path, caplog):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        with caplog.at_level(logging.INFO):
            await feed(check, 0, "CAT_DETECTED", DFILevelPercent=19)
            await feed(check, 5 * MIN, "CAT_DETECTED", DFILevelPercent=19)
            await feed(check, 10 * MIN, "READY", DFILevelPercent=73)
            assert tg.reliable == []                       # waiting for confirmation
            assert check.interval_override == 30           # ... while polling fast
            alerts = await feed(check, 15 * MIN, "READY", DFILevelPercent=73)
        assert [a.key for a in alerts] == ["litter_dump"]
        assert len(tg.reliable) == 1
        assert "🚨" in tg.reliable[0] and "19%→73%" in tg.reliable[0] and "COPTiP" in tg.reliable[0]
        assert tg.waste == []                              # 73% is below the drawer threshold
        # the plain log tells the story
        assert "CHANGE COPTiP [poll]: status CAT_DETECTED→READY; waste% 19→73" in caplog.text
        rows = await check.db.get_alerts()
        assert rows[0]["alert_type"] == "litter_dump"
        await check.db.disconnect()
    run(scenario())


def test_normal_increments_send_nothing(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        for i, waste in enumerate([19, 20, 22, 23, 26, 27, 29]):
            await feed(check, i * 5 * MIN, DFILevelPercent=waste)
        assert tg.reliable == [] and tg.waste == []
        await check.db.disconnect()
    run(scenario())


def test_waste_alert_unchanged_percent_only_repeat_every_3h(tmp_path):
    """User decision: trust the % only (>= 90), ignore is_waste_drawer_full, repeat every 3h."""
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        # flaky full flag / DRAWER_FULL status below the threshold: no waste alert, no error alert
        await feed(check, 0, "DRAWER_FULL", DFILevelPercent=78, isDFIFull=True)
        await feed(check, 5 * MIN, "DRAWER_FULL", DFILevelPercent=89, isDFIFull=True)
        assert tg.waste == [] and tg.reliable == []
        # reaching 90% alerts, even with the flag off
        await feed(check, 10 * MIN, "READY", DFILevelPercent=90, isDFIFull=False)
        assert tg.waste == [("COPTiP", 90)]
        # stays quiet for 3 hours ...
        for i in range(1, 36):
            await feed(check, 10 * MIN + i * 5 * MIN, "READY", DFILevelPercent=92)
        assert len(tg.waste) == 1
        # ... then repeats
        await feed(check, 10 * MIN + 3 * 3600, "READY", DFILevelPercent=92)
        assert tg.waste == [("COPTiP", 90), ("COPTiP", 92)]
        assert tg.reliable == []
        await check.db.disconnect()
    run(scenario())


def test_motor_fault_and_empty_cycle_counter_are_sent(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        await feed(check, 0)
        await feed(check, 5 * MIN, globeMotorFaultStatus="FAULT_TIMEOUT")
        assert len(tg.reliable) == 1 and "globe motor fault" in tg.reliable[0]
        await feed(check, 10 * MIN, odometerEmptyCycles=4)
        assert any("empty-cycle counter +1" in m for m in tg.reliable)
        assert any("globe motor fault cleared" in m for m in tg.reliable)
        await check.db.disconnect()
    run(scenario())


def test_every_snapshot_is_persisted_in_full(tmp_path):
    async def scenario():
        check, _tg = make_check(tmp_path)
        await check._init_db()
        await feed(check, 0, someFutureWhiskerKey={"nested": [1, 2, 3]})
        await feed(check, 5 * MIN, DFILevelPercent=20)
        await check.db.disconnect()
    run(scenario())

    con = sqlite3.connect(tmp_path / "test.db")
    rows = con.execute("SELECT ts, robot_id, source, status, json FROM raw_snapshots ORDER BY id").fetchall()
    assert len(rows) == 2
    first = json.loads(rows[0][4])
    assert first == make_raw(someFutureWhiskerKey={"nested": [1, 2, 3]})   # ALL keys, untouched
    assert rows[0][0] == datetime.fromtimestamp(T0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert rows[0][1:4] == (RID, "poll", "READY")
    assert con.execute("SELECT COUNT(*) FROM robot_states").fetchone()[0] == 2   # legacy table still fed

    files = sorted((tmp_path / "data").glob("robot_snapshots-*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["data"]["someFutureWhiskerKey"] == {"nested": [1, 2, 3]}
    assert lines[1]["data"]["DFILevelPercent"] == 20 and lines[1]["robot_id"] == RID


def test_detector_state_survives_restart(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        await feed(check, 0, DFILevelPercent=19)
        await feed(check, 5 * MIN, DFILevelPercent=73)
        await check.db.disconnect()
        # daemon restarts
        check2, tg2 = make_check(tmp_path)
        await check2._init_db()
        await feed(check2, 10 * MIN, DFILevelPercent=73)
        assert len(tg2.reliable) == 1 and "19%→73%" in tg2.reliable[0]
        await check2.db.disconnect()
    run(scenario())


def test_dry_run_stores_snapshot_but_sends_and_mutates_nothing(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        await feed(check, 0, odometerEmptyCycles=3)
        await check.db.disconnect()

        dry, tg_dry = make_check(tmp_path, dry_run=True)
        await dry._init_db()
        alerts = await feed(dry, 5 * MIN, odometerEmptyCycles=4, DFILevelPercent=95)
        assert [a.key for a in alerts] == ["litter_dump"]       # detection ran ...
        assert tg_dry.reliable == [] and tg_dry.waste == []     # ... nothing sent
        assert await dry.db.get_alerts() == []
        state = await dry.db.get_kv(f"detector:{RID}")
        assert state["incidents"] == {} and state["last"]["empty_cycles"] == 3   # untouched
        await dry.db.disconnect()
    run(scenario())
    con = sqlite3.connect(tmp_path / "test.db")
    assert con.execute("SELECT COUNT(*) FROM raw_snapshots").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM robot_states").fetchone()[0] == 1


def test_activity_history_is_deduplicated(tmp_path):
    async def scenario():
        check, _tg = make_check(tmp_path)
        await check._init_db()
        batch1 = [
            {"timestamp": "2026-11-19 06:40:29", "action": "robotCycleStatusDump", "action_value": "", "raw": {"originalHex": "0x4E0002"}},
            {"timestamp": "2026-11-19 06:40:29", "action": "odometerCleanCycles", "action_value": "3758", "raw": {}},
        ]
        batch2 = batch1 + [
            {"timestamp": "2026-11-19 06:41:41", "action": "DFILevelPercent", "action_value": "15", "raw": {}},
        ]
        assert len(await check.db.store_activities(RID, batch1)) == 2
        new = await check.db.store_activities(RID, batch2)
        assert [e["action"] for e in new] == ["DFILevelPercent"]
        assert await check.db.count_activities(RID) == 3
        latest = await check.db.get_activities(RID, limit=1)
        assert latest[0]["action"] == "DFILevelPercent" and latest[0]["action_value"] == "15"
        await check.db.disconnect()
    run(scenario())


def test_jsonl_rotation_compresses_old_days_and_enforces_retention(tmp_path):
    log = SnapshotLog(str(tmp_path), retention_days=90)
    old = tmp_path / "robot_snapshots-2026-01-01.jsonl.gz"
    old.write_bytes(gzip.compress(b"{}\n"))
    log.append({"n": 1}, now=datetime(2026, 11, 17, 23, 59, tzinfo=timezone.utc))
    log.append({"n": 2}, now=datetime(2026, 11, 18, 0, 1, tzinfo=timezone.utc))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["robot_snapshots-2026-11-17.jsonl.gz", "robot_snapshots-2026-11-18.jsonl"]
    with gzip.open(tmp_path / "robot_snapshots-2026-11-17.jsonl.gz", "rt") as f:
        assert json.loads(f.readline()) == {"n": 1}


def test_raw_snapshot_pruning(tmp_path):
    async def scenario():
        check, _tg = make_check(tmp_path)
        await check._init_db()
        await check.db.store_raw_snapshot(RID, {"a": 1}, "2020-01-01 00:00:00")
        await check.db.store_raw_snapshot(RID, {"a": 2}, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        assert await check.db.prune_raw_snapshots(90) == 1
        assert (await check.db.get_latest_raw_snapshot(RID))["data"] == {"a": 2}
        await check.db.disconnect()
    run(scenario())
