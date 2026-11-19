"""Tiered litter alerts: the gentle ``litter_low`` (group only, daily) and the
``litter_very_low`` tier (group + DM, every 3 h until the litter is restored).

Owner's calibration 2026-11-19: filled exactly to the full line -> litterLevel
439 mm / 90 % OPTIMAL; empty globe -> 484 mm / 32 % REFILL. The robot's
percentage is 2 * (500 - mm) capped at 90, so 475 mm = 50 %, 470 mm = 60 %.
"""
import json

from conftest import make_raw
from test_critical_alerts import ALICE, BOB, GROUP, ScriptedNotifier, incident_files, make_check, run
from test_critical_alerts import feed as feed_check

from detection import Alert, DetectionConfig, Detector, extract_fields, is_critical

MIN = 60
HOUR = 3600


def level(mm):
    """Raw litter fields for a TOF reading, as the robot reports them."""
    pct = min(90, 2 * (500 - mm))
    state = "OPTIMAL" if pct >= 76 else "LOW" if pct >= 62 else "REFILL" if pct >= 28 else "EMPTY"
    return dict(litterLevel=mm, litterLevelPercentage=pct / 100, litterLevelState=state)


def feed(det, t, status="READY", **raw):
    return det.process(extract_fields(make_raw(**raw), status), float(t))


def keys(alerts):
    return [(a.key, a.kind) for a in alerts]


def very_low(det, mm=475, t=0):
    """Confirmed very-low alert: seen at ``t``, alerted 5 min later."""
    assert feed(det, t, **level(mm)) == []
    (alert,) = feed(det, t + 5 * MIN, **level(mm))
    assert (alert.key, alert.kind) == ("litter_very_low", "new")
    return alert


def test_config_defaults_and_overrides():
    cfg = DetectionConfig.from_config({})
    assert (cfg.litter_very_low_mm, cfg.litter_very_low_percent,
            cfg.litter_very_low_repeat_seconds) == (475, 50, 10800)
    cfg = DetectionConfig.from_config({"litter_very_low_mm": 470, "litter_very_low_percent": 40,
                                       "litter_very_low_repeat_seconds": 3600})
    assert (cfg.litter_very_low_mm, cfg.litter_very_low_percent,
            cfg.litter_very_low_repeat_seconds) == (470, 40, 3600)


def test_triggers_at_475mm_50_percent_after_confirmation():
    det = Detector("COPTiP")
    assert feed(det, 0, **level(475)) == []                 # needs 5 min settled, like litter_low
    assert feed(det, 4 * MIN, **level(475)) == []
    (alert,) = feed(det, 5 * MIN, **level(475))             # and no litter_low duplicate
    assert (alert.key, alert.kind, alert.urgent) == ("litter_very_low", "new", True)
    assert alert.plain == ("🚨 COPTiP: litter is VERY low (50%, 475 mm; full line ≈439 mm, "
                           "empty ≈484 mm). Add litter.")
    assert alert.dm and not alert.critical                  # DM, but no forensic incident file
    assert not is_critical("litter_very_low")
    assert not Alert("litter_low", "new", "x").dm


def test_not_at_470mm_60_percent_only_the_gentle_tier():
    det = Detector("COPTiP")
    feed(det, 0, **level(470))
    assert keys(feed(det, 5 * MIN, **level(470))) == [("litter_low", "new")]
    assert keys(feed(det, 4 * HOUR, **level(474))) == []    # 474 mm: still not very low
    assert "litter_very_low" not in det.incidents


def test_mm_wins_over_percent_and_percent_is_the_fallback_when_mm_is_missing():
    # mm present and fine: a low percentage alone does not trigger the tier
    det = Detector("COPTiP")
    odd = dict(litterLevel=470, litterLevelPercentage=0.4, litterLevelState="REFILL")
    feed(det, 0, **odd)
    assert keys(feed(det, 5 * MIN, **odd)) == [("litter_low", "new")]

    for pct, expected in ((50, "litter_very_low"), (52, "litter_low")):
        det = Detector("COPTiP")
        raw = dict(litterLevel=None, litterLevelPercentage=pct / 100, litterLevelState="REFILL")
        feed(det, 0, **raw)
        (alert,) = feed(det, 5 * MIN, **raw)
        assert alert.key == expected
    det = Detector("COPTiP")
    raw = dict(litterLevel=None, litterLevelPercentage=0.5, litterLevelState="REFILL")
    feed(det, 0, **raw)
    (alert,) = feed(det, 5 * MIN, **raw)
    assert alert.plain == "🚨 COPTiP: litter is VERY low (50%). Add litter."


def test_globe_looks_empty_wording_at_or_below_litter_empty_percent():
    text = very_low(Detector("COPTiP"), 484).plain
    assert text == ("🚨 COPTiP: litter is VERY low — globe looks EMPTY (32%, 484 mm; "
                    "full line ≈439 mm, empty ≈484 mm). Add litter.")
    assert "EMPTY" not in very_low(Detector("COPTiP"), 477).plain          # 46 %
    off = DetectionConfig.from_config({"litter_empty_percent": 0})
    assert "EMPTY" not in very_low(Detector("COPTiP", off), 484).plain


def test_mid_cycle_readings_are_ignored():
    det = Detector("COPTiP")
    cycling = dict(robotCycleStatus="CYCLE_DUMP", robotStatus="ROBOT_CLEAN",
                   robotCycleState="CYCLE_STATE_PROCESS", displayCode="DC_MODE_CYCLE")
    feed(det, 0)
    seen = []
    for t in (1, 3, 6, 9):                                   # litter thrown around mid-cycle
        seen += feed(det, t * MIN, "CLEAN_CYCLE", **level(484), **cycling)
    seen += feed(det, 10 * MIN)                              # back at rest, level fine
    # (the state-based litter_low tier does not filter mid-cycle readings; this one does)
    assert all(a.key == "litter_low" for a in seen)
    assert det.state.get("very_low") is None and not det.incidents

    # ... and a cycle does not clear or restart an active very-low incident
    det = Detector("COPTiP")
    very_low(det)
    assert feed(det, 20 * MIN, "CLEAN_CYCLE", **level(439), **cycling) == []
    assert "litter_very_low" in det.incidents
    assert feed(det, 25 * MIN, **level(476)) == []


def test_repeats_every_3h_until_restored_then_resolves():
    det = Detector("COPTiP")
    very_low(det)                                            # alerted at 5 min
    assert feed(det, 3 * HOUR, **level(476)) == []
    (rep,) = feed(det, 3 * HOUR + 5 * MIN, **level(478))
    assert (rep.key, rep.kind) == ("litter_very_low", "repeat")
    assert "(44%, 478 mm;" in rep.plain and "globe looks EMPTY" in rep.plain   # current reading
    # a little litter added (470 mm / 60 % REFILL): not restored -> keeps reminding, no litter_low
    assert feed(det, 4 * HOUR, **level(470)) == []
    assert keys(feed(det, 6 * HOUR + 5 * MIN, **level(470))) == [("litter_very_low", "repeat")]
    # refilled to the full line
    (done,) = feed(det, 7 * HOUR, **level(439))
    assert (done.key, done.kind) == ("litter_very_low", "resolved")
    assert done.plain == "✅ COPTiP: litter is back to a normal level (90%, 439 mm)."
    assert done.dm
    assert not det.incidents and det.state["very_low"] is None
    assert feed(det, 11 * HOUR, **level(439)) == []


def test_resolves_at_dump_resolve_litter_percent_even_if_the_state_is_still_low():
    det = Detector("COPTiP", DetectionConfig.from_config({"dump_resolve_litter_percent": 70}))
    very_low(det)
    assert feed(det, 10 * MIN, **level(468)) == []           # 64 % LOW: below 70
    assert keys(feed(det, 15 * MIN, **level(465))) == [("litter_very_low", "resolved")]  # 70 % LOW
    # the gentle tier is now free to report the remaining LOW state after its own grace
    assert keys(feed(det, 20 * MIN, **level(465))) == [("litter_low", "new")]


def test_pending_very_low_is_dropped_if_the_level_recovers_before_confirmation():
    det = Detector("COPTiP")
    feed(det, 0, **level(476))
    assert feed(det, 2 * MIN, **level(470)) == []            # one-off reading
    assert keys(feed(det, 7 * MIN, **level(470))) == [("litter_low", "new")]
    assert "litter_very_low" not in det.incidents


def test_very_low_replaces_an_open_litter_low_without_an_ok_again_message():
    det = Detector("COPTiP")
    feed(det, 0, **level(466))                               # 68 % LOW
    assert keys(feed(det, 5 * MIN, **level(466))) == [("litter_low", "new")]
    assert feed(det, 1 * HOUR, **level(475)) == []
    assert keys(feed(det, 1 * HOUR + 5 * MIN, **level(475))) == [("litter_very_low", "new")]
    assert set(det.incidents) == {"litter_very_low"}
    # no daily litter_low reminder on top of it
    out = feed(det, 25 * HOUR, **level(475))
    assert keys(out) == [("litter_very_low", "repeat")]


def test_suppressed_while_a_dump_incident_is_open_then_takes_over():
    cfg = DetectionConfig.from_config({"dump_max_alert_seconds": 2 * HOUR})
    det = Detector("COPTiP", cfg)
    feed(det, 0, **level(439))
    feed(det, 5 * MIN, **level(480))                         # 90 -> 40 %: litter gone
    assert keys(feed(det, 10 * MIN, **level(480))) == [("litter_dump", "new")]
    out = feed(det, 70 * MIN, **level(480))
    assert keys(out) == [("litter_dump", "repeat")]
    assert set(det.incidents) == {"litter_dump"}
    # /ack silences the dump reminders: the gentle tier takes over, very-low stays quiet
    det.acknowledge()
    feed(det, 75 * MIN, **level(480))
    assert keys(feed(det, 80 * MIN, **level(480))) == [("litter_low", "new")]
    assert "litter_very_low" not in det.incidents
    # the dump incident closes at its cap with the globe still empty -> very-low takes over
    feed(det, 2 * HOUR + 11 * MIN, **level(480))
    assert "litter_dump" not in det.incidents
    out = feed(det, 2 * HOUR + 16 * MIN, **level(480))
    assert keys(out) == [("litter_very_low", "new")]
    assert set(det.incidents) == {"litter_very_low"}         # and litter_low is gone again


def test_dump_resolved_by_refill_leaves_no_very_low_behind():
    det = Detector("COPTiP")
    feed(det, 0, **level(439))
    feed(det, 5 * MIN, **level(482))
    assert keys(feed(det, 10 * MIN, **level(482))) == [("litter_dump", "new")]
    assert keys(feed(det, 30 * MIN, **level(439))) == [("litter_dump", "resolved")]
    assert feed(det, 40 * MIN, **level(439)) == []
    assert not det.incidents and not det.state.get("very_low")


def test_ack_silences_the_reminders():
    det = Detector("COPTiP")
    very_low(det)
    assert det.acknowledge() == ["litter_very_low"]
    assert feed(det, 4 * HOUR, **level(475)) == []
    assert keys(feed(det, 5 * HOUR, **level(439))) == [("litter_very_low", "resolved")]


def test_state_survives_a_restart():
    det = Detector("COPTiP")
    very_low(det)
    det2 = Detector("COPTiP", DetectionConfig(), json.loads(json.dumps(det.state)))
    assert feed(det2, 1 * HOUR, **level(475)) == []          # no second "new" alert
    assert keys(feed(det2, 3 * HOUR + 5 * MIN, **level(470))) == [("litter_very_low", "repeat")]
    det3 = Detector("COPTiP", DetectionConfig(), json.loads(json.dumps(det2.state)))
    (done,) = feed(det3, 4 * HOUR, **level(439))
    assert (done.key, done.kind) == ("litter_very_low", "resolved")
    assert "(90%, 439 mm)" in done.plain

    # restart during the confirmation window: the 5 min keep counting
    det = Detector("COPTiP")
    feed(det, 0, **level(475))
    det2 = Detector("COPTiP", DetectionConfig(), json.loads(json.dumps(det.state)))
    assert keys(feed(det2, 5 * MIN, **level(475))) == [("litter_very_low", "new")]


def test_goes_to_group_and_dms_first_alert_reminder_and_resolved(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path)
        await check._init_db()
        await feed_check(check, 0, **level(475))
        await feed_check(check, 5 * MIN, **level(475))
        await feed_check(check, 3 * HOUR + 5 * MIN, **level(475))
        await feed_check(check, 4 * HOUR, **level(439))
        await check.wait_background()
        for chat in (GROUP, ALICE, BOB):
            msgs = tg.to(chat)
            assert len(msgs) == 3, (chat, msgs)
            assert "litter is <b>VERY low</b> (50%, 475 mm" in msgs[0]
            assert msgs[1].startswith("🔁 Reminder") and "VERY low" in msgs[1]
            assert msgs[2].startswith("✅") and "back to a normal level (90%, 439 mm)" in msgs[2]
        assert incident_files(tmp_path) == []                # not a fault: no forensic file
        await check.db.disconnect()
    run(scenario())


def test_gentle_litter_low_stays_group_only(tmp_path):
    async def scenario():
        check, tg = make_check(tmp_path, tg=ScriptedNotifier())
        await check._init_db()
        await feed_check(check, 0, **level(470))
        await feed_check(check, 5 * MIN, **level(470))
        assert len(tg.to(GROUP)) == 1 and "litter level is" in tg.to(GROUP)[0]
        assert tg.to(ALICE) == [] and tg.to(BOB) == []
        await check.db.disconnect()
    run(scenario())
