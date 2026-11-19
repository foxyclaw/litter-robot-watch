"""Offline tests for detection.py — no network, clock is injected."""
from conftest import make_raw

from detection import (
    DetectionConfig,
    Detector,
    derive_status,
    diff_fields,
    extract_fields,
    format_changes,
    is_settled,
)

MIN = 60


def feed(det, t, status=None, **raw):
    """Feed one reading at t seconds; returns the alerts."""
    return det.process(extract_fields(make_raw(**raw), status), float(t))


def run(det, readings):
    """readings: [(t, status, raw-overrides)] -> [(t, alert)]"""
    out = []
    for t, status, raw in readings:
        out += [(t, a) for a in feed(det, t, status, **raw)]
    return out


def keys(alerts):
    return [(a.key, a.kind) for _t, a in alerts]


# ── the 2026-11-10 incident ──────────────────────────────────────────────────

def test_nov10_waste_sequence_alerts():
    """19% -> 19% -> 73% (5-min polls, as logged) must raise a litter-dump alert."""
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [
        (0 * MIN, "CAT_DETECTED", dict(DFILevelPercent=19)),    # 18:00:06
        (5 * MIN, "CAT_DETECTED", dict(DFILevelPercent=19)),    # 18:05:08
        (10 * MIN, "READY", dict(DFILevelPercent=73)),          # 18:10:09  +54 points
        (15 * MIN, "READY", dict(DFILevelPercent=73)),          # still there -> confirmed
    ])
    assert keys(alerts) == [("litter_dump", "new")]
    t, alert = alerts[0]
    assert t == 15 * MIN
    assert "COPTiP" in alert.text
    assert "dumped" in alert.text
    assert "19%→73% in 5 min" in alert.text
    assert "Check the globe has litter" in alert.text
    assert alert.urgent


def test_nov10_immediate_when_confirmation_disabled():
    det = Detector("COPTiP", DetectionConfig(confirm_seconds=0))
    alerts = run(det, [
        (0, "CAT_DETECTED", dict(DFILevelPercent=19)),
        (5 * MIN, "CAT_DETECTED", dict(DFILevelPercent=19)),
        (10 * MIN, "READY", dict(DFILevelPercent=73)),
    ])
    assert [(t, a.key) for t, a in alerts] == [(10 * MIN, "litter_dump")]


def test_nov10_full_sequence_from_db_history():
    """What the DB actually recorded: litter 90%->44% (REFILL) at 17:45, waste
    19%->73% at 18:10. The litter drop alone must alert ~25 min earlier — and
    must not claim the litter is in the drawer until the drawer actually jumps."""
    det = Detector("COPTiP", DetectionConfig())
    ok = dict(DFILevelPercent=19, litterLevelPercentage=0.9, litterLevelState="OPTIMAL")
    low = dict(DFILevelPercent=19, litterLevelPercentage=0.44, litterLevelState="REFILL")
    dumped = dict(DFILevelPercent=73, litterLevelPercentage=0.44, litterLevelState="REFILL")
    alerts = run(det, [
        (0, "READY", ok), (5 * MIN, "READY", ok),
        (10 * MIN, "READY", low),                # 17:45 litter drops
        (15 * MIN, "READY", low),                # confirmed
        (20 * MIN, "READY", low), (25 * MIN, "CAT_DETECTED", low), (30 * MIN, "CAT_DETECTED", low),
        (35 * MIN, "READY", dumped),             # 18:10 waste jumps
        (40 * MIN, "READY", dumped),
    ])
    dump = [(t, a) for t, a in alerts if a.key == "litter_dump"]
    # the drawer jump needs no second confirmation once a dump incident is open
    assert [(t, a.kind) for t, a in dump] == [(15 * MIN, "new"), (35 * MIN, "update")]
    stage1, stage2 = dump[0][1].text, dump[1][1].text
    assert "litter level dropped 90%→44%" in stage1
    assert "litter missing from the globe or globe not level" in stage1
    assert "Do NOT let it cycle again until checked" in stage1
    assert "waste drawer unchanged at 19%" in stage1
    assert "dumped into the waste drawer" not in stage1 and "failed cycle" not in stage1
    assert "litter dumped into the waste drawer" in stage2
    assert "waste drawer 19%→73%" in stage2 and "litter level 90%→44%" in stage2
    # the REFILL state is not reported a second time while the dump incident is open
    assert all(a.key != "litter_low" for _t, a in alerts)


def test_normal_small_increments_do_not_alert():
    """+1..3 points per clean cycle, all day long: silence."""
    det = Detector("COPTiP", DetectionConfig())
    readings, waste = [], 10
    for i in range(200):
        if i % 12 == 0:
            waste += (1, 2, 3)[(i // 12) % 3]
        readings.append((i * 5 * MIN, "READY", dict(DFILevelPercent=waste)))
    assert waste > 40
    assert run(det, readings) == []
    assert det.incidents == {}


def test_step_just_below_threshold_does_not_alert():
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [(i * 5 * MIN, "READY", dict(DFILevelPercent=w))
                       for i, w in enumerate([20, 20, 44, 44, 44, 44])])
    assert alerts == []


def test_jump_spread_over_window_alerts():
    """Two +15 steps within 15 minutes add up to a dump."""
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [(i * 5 * MIN, "READY", dict(DFILevelPercent=w))
                       for i, w in enumerate([20, 35, 50, 50, 50])])
    assert keys(alerts) == [("litter_dump", "new")]
    assert "20%→50%" in alerts[0][1].text


def test_transient_spike_is_not_a_dump():
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [
        (0, "READY", dict(DFILevelPercent=19)),
        (60, "READY", dict(DFILevelPercent=76)),     # blip
        (120, "READY", dict(DFILevelPercent=19)),
        (10 * MIN, "READY", dict(DFILevelPercent=19)),
    ])
    assert alerts == []


def test_mid_cycle_readings_are_ignored():
    """During a clean cycle the drawer sensor reports junk (often 0)."""
    det = Detector("COPTiP", DetectionConfig(confirm_seconds=0))
    cyc = dict(robotStatus="ROBOT_CLEAN", robotCycleStatus="CYCLE_DUMP")
    alerts = run(det, [
        (0, "READY", dict(DFILevelPercent=40)),
        (30, "CLEAN_CYCLE", dict(DFILevelPercent=0, **cyc)),
        (60, "CLEAN_CYCLE", dict(DFILevelPercent=76, **cyc)),
        (90, "READY", dict(DFILevelPercent=42)),
    ])
    assert alerts == []
    assert not is_settled(extract_fields(make_raw(**cyc), "CLEAN_CYCLE"))


def test_zero_baseline_after_emptying_is_ignored():
    """0% right after the drawer was emptied, then the first real reading."""
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [(i * 5 * MIN, "READY", dict(DFILevelPercent=w))
                       for i, w in enumerate([80, 0, 0, 30, 30, 30])])
    assert alerts == []


def test_long_monitoring_gap_is_not_compared():
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [
        (0, "READY", dict(DFILevelPercent=30)),
        (2 * 86400, "READY", dict(DFILevelPercent=58)),
        (2 * 86400 + 5 * MIN, "READY", dict(DFILevelPercent=58)),
    ])
    assert alerts == []


# ── other dump signals ───────────────────────────────────────────────────────

def test_empty_cycle_counter_increment_alerts_immediately():
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [
        (0, "READY", dict(odometerEmptyCycles=3)),
        (5 * MIN, "READY", dict(odometerEmptyCycles=4)),
    ])
    assert [(t, a.key, a.kind) for t, a in alerts] == [(5 * MIN, "litter_dump", "new")]
    assert "empty-cycle counter +1 (3→4)" in alerts[0][1].text


def test_empty_cycle_counter_unchanged_or_first_reading_is_silent():
    det = Detector("COPTiP", DetectionConfig())
    assert run(det, [(0, "READY", dict(odometerEmptyCycles=3)),
                     (5 * MIN, "READY", dict(odometerEmptyCycles=3))]) == []


def test_empty_cycle_status_alerts():
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [
        (0, "READY", {}),
        (30, "EMPTY_CYCLE", dict(robotStatus="ROBOT_EMPTY", robotCycleStatus="CYCLE_DUMP")),
    ])
    assert keys(alerts) == [("litter_dump", "new")]
    assert "EMPTY cycle" in alerts[0][1].text


def test_litter_state_empty_alerts():
    det = Detector("COPTiP", DetectionConfig())
    alerts = run(det, [
        (0, "READY", {}),
        (5 * MIN, "READY", dict(litterLevelPercentage=0.18, litterLevelState="EMPTY")),
    ])
    assert ("litter_dump", "new") in keys(alerts)
    assert "OPTIMAL→EMPTY" in alerts[0][1].text


def test_dump_alert_repeats_until_resolved():
    det = Detector("COPTiP", DetectionConfig(dump_repeat_seconds=3600))
    alerts = run(det, [(0, "READY", dict(DFILevelPercent=19)),
                       (5 * MIN, "READY", dict(DFILevelPercent=73)),
                       (10 * MIN, "READY", dict(DFILevelPercent=73))])
    assert keys(alerts) == [("litter_dump", "new")]
    later = run(det, [(10 * MIN + i * 5 * MIN, "READY", dict(DFILevelPercent=73)) for i in range(1, 37)])
    assert keys(later) == [("litter_dump", "repeat")] * 3          # 3 hours -> 3 reminders
    assert "Reminder" in later[0][1].text
    # drawer-jump-only incident (litter never read low, no fault): the drawer
    # reading dropping back clears it — sensor noise
    back = run(det, [(3 * 3600 + 15 * MIN, "READY", dict(DFILevelPercent=16))])
    assert keys(back) == [("litter_dump", "resolved")]
    assert "waste drawer back to 16%" in back[0][1].text
    assert det.incidents == {}


def test_dump_resolved_by_litter_refill():
    det = Detector("COPTiP", DetectionConfig())
    low = dict(litterLevelPercentage=0.44, litterLevelState="REFILL")
    run(det, [(0, "READY", {}), (5 * MIN, "READY", low), (10 * MIN, "READY", low)])
    assert "litter_dump" in det.incidents
    alerts = run(det, [(60 * MIN, "READY", dict(litterLevelPercentage=0.9, litterLevelState="OPTIMAL"))])
    assert keys(alerts) == [("litter_dump", "resolved")]   # litter_low was suppressed: no ✅ for it
    assert "litter level back to 90%" in alerts[0][1].text
    assert det.incidents == {}


def test_dump_reminders_stop_after_ack_and_after_max_age():
    det = Detector("COPTiP", DetectionConfig(dump_repeat_seconds=3600, dump_max_alert_seconds=86400))
    run(det, [(0, "READY", dict(odometerEmptyCycles=3)), (60, "READY", dict(odometerEmptyCycles=4))])
    assert det.acknowledge() == ["litter_dump"]
    assert run(det, [(i * 3600, "READY", dict(odometerEmptyCycles=4)) for i in range(1, 6)]) == []
    # new evidence re-alerts even after /ack
    again = run(det, [(6 * 3600, "READY", dict(odometerEmptyCycles=5))])
    assert keys(again) == [("litter_dump", "update")]
    run(det, [(31 * 3600, "READY", dict(odometerEmptyCycles=5))])
    assert "litter_dump" not in det.incidents      # expired after a day


# ── faults ───────────────────────────────────────────────────────────────────

def test_globe_motor_fault_alerts_repeats_and_resolves():
    det = Detector("COPTiP", DetectionConfig(fault_repeat_seconds=3600))
    fault = dict(globeMotorFaultStatus="FAULT_OVERTORQUE_AMP")
    alerts = run(det, [(0, "READY", {}), (5 * MIN, "READY", fault)])
    assert keys(alerts) == [("globe_motor_fault", "new")]
    assert "FAULT_OVERTORQUE_AMP" in alerts[0][1].text and alerts[0][1].urgent
    assert run(det, [(10 * MIN, "READY", fault)]) == []                      # cooldown
    assert keys(run(det, [(66 * MIN, "READY", fault)])) == [("globe_motor_fault", "repeat")]
    assert keys(run(det, [(70 * MIN, "READY", {})])) == [("globe_motor_fault", "resolved")]


def test_retract_fault_usb_fault_and_fault_clear():
    det = Detector("COPTiP", DetectionConfig())
    assert run(det, [(0, "READY", dict(globeMotorFaultStatus="FAULT_CLEAR",
                                        globeMotorRetractFaultStatus="NONE", USBFaultStatus="CLEAR"))]) == []
    alerts = run(det, [(60, "READY", dict(globeMotorRetractFaultStatus="FAULT_TIMEOUT",
                                           USBFaultStatus="SET"))])
    assert sorted(keys(alerts)) == [("globe_retract_fault", "new"), ("usb_fault", "new")]


def test_pinch_detected():
    det = Detector("COPTiP", DetectionConfig())
    assert run(det, [(0, "READY", dict(pinchStatus="SWITCH_2_SET"))]) == []   # normal idle value
    alerts = run(det, [(60, "PINCH_DETECT", {})])
    assert ("pinch", "new") in keys(alerts)
    det2 = Detector("COPTiP", DetectionConfig())
    assert ("pinch", "new") in keys(run(det2, [(0, "READY", dict(globeMotorFaultStatus="FAULT_PINCH"))]))


def test_error_status_alerts_immediately_but_drawer_full_does_not():
    det = Detector("COPTiP", DetectionConfig())
    assert keys(run(det, [(0, "OVER_TORQUE_FAULT", {})])) == [("status:OVER_TORQUE_FAULT", "new")]
    det = Detector("COPTiP", DetectionConfig())
    assert run(det, [(0, "DRAWER_FULL", dict(isDFIFull=True)),
                     (3600, "DRAWER_FULL", dict(isDFIFull=True))]) == []


def test_bonnet_removed_needs_grace_period_and_legacy_codes_map():
    cfg = DetectionConfig.from_config({"error_statuses": ["CSF", "BR", "CSI", "P", "OFF"],
                                       "status_grace_seconds": 600})
    assert "PAUSED" in cfg.error_statuses and "BONNET_REMOVED" in cfg.error_statuses
    det = Detector("COPTiP", cfg)
    br = dict(robotStatus="ROBOT_BONNET", isBonnetRemoved=True)
    assert run(det, [(0, "BONNET_REMOVED", br), (5 * MIN, "BONNET_REMOVED", br)]) == []
    assert keys(run(det, [(10 * MIN, "BONNET_REMOVED", br)])) == [("status:BONNET_REMOVED", "new")]
    # a quick clean (bonnet off for 2 minutes) never alerts
    det = Detector("COPTiP", cfg)
    assert run(det, [(0, "BONNET_REMOVED", br), (120, "READY", {}), (20 * MIN, "READY", {})]) == []


def test_offline_only_after_configured_minutes():
    det = Detector("COPTiP", DetectionConfig.from_config({"offline_minutes": 30}))
    off = dict(isOnline=False)
    assert run(det, [(i * 5 * MIN, "OFFLINE", off) for i in range(6)]) == []
    alerts = run(det, [(30 * MIN, "OFFLINE", off)])
    assert keys(alerts) == [("offline", "new")] and "30 min" in alerts[0][1].text
    assert keys(run(det, [(35 * MIN, "READY", {})])) == [("offline", "resolved")]


def test_api_unreachable_alerts_after_grace_and_recovers():
    det = Detector("Litter-Robot", DetectionConfig.from_config({"offline_minutes": 30}))
    assert det.process_unreachable(0) == []
    assert det.process_unreachable(20 * MIN) == []
    alerts = det.process_unreachable(31 * MIN)
    assert [(a.key, a.kind) for a in alerts] == [("api_unreachable", "new")]
    assert [(a.key, a.kind) for a in det.process_reachable(40 * MIN)] == [("api_unreachable", "resolved")]


def test_litter_low_alerts_once_then_daily_and_escalates():
    det = Detector("COPTiP", DetectionConfig())
    low = dict(litterLevelPercentage=0.7, litterLevelState="LOW")
    alerts = run(det, [(0, "READY", {}), (5 * MIN, "READY", low), (10 * MIN, "READY", low)])
    assert keys(alerts) == [("litter_low", "new")]
    assert run(det, [(10 * MIN + i * 3600, "READY", low) for i in range(1, 23)]) == []
    assert keys(run(det, [(25 * 3600, "READY", low)])) == [("litter_low", "repeat")]
    worse = dict(litterLevelPercentage=0.56, litterLevelState="REFILL")
    assert ("litter_low", "update") in keys(run(det, [(26 * 3600, "READY", worse)]))


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_state_survives_json_roundtrip():
    """Detector state is persisted in SQLite between restarts."""
    import json
    det = Detector("COPTiP", DetectionConfig())
    run(det, [(0, "READY", dict(DFILevelPercent=19)), (5 * MIN, "READY", dict(DFILevelPercent=73))])
    det2 = Detector("COPTiP", DetectionConfig(), json.loads(json.dumps(det.state)))
    alerts = run(det2, [(10 * MIN, "READY", dict(DFILevelPercent=73))])
    assert keys(alerts) == [("litter_dump", "new")]


def test_extract_fields_and_change_log():
    old = extract_fields(make_raw(), "CAT_DETECTED")
    new = extract_fields(make_raw(DFILevelPercent=73, odometerEmptyCycles=4,
                                  globeMotorFaultStatus="FAULT_TIMEOUT"), "LitterBoxStatus.READY")
    assert new["status"] == "READY" and new["waste"] == 73 and new["litter_pct"] == 90.0
    text = format_changes(diff_fields(old, new))
    assert "status CAT_DETECTED→READY" in text
    assert "waste% 19→73" in text
    assert "emptyCycles 3→4" in text
    assert "globeMotorFault FAULT_CLEAR→FAULT_TIMEOUT" in text
    assert diff_fields(new, new) == [] and diff_fields(None, new) == []


def test_derive_status_fallback():
    assert derive_status(make_raw()) == "READY"
    assert derive_status(make_raw(isOnline=False)) == "OFFLINE"
    assert derive_status(make_raw(robotStatus="ROBOT_EMPTY")) == "EMPTY_CYCLE"
    assert derive_status(make_raw(isDFIFull=True)) == "DRAWER_FULL"
    assert derive_status(make_raw(robotCycleState="CYCLE_STATE_PAUSE")) == "PAUSED"
