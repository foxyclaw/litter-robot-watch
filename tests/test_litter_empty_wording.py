"""An empty LR4 globe does not read 0 %.

Owner's calibration 2026-11-19 after a deep clean: a completely EMPTY globe
reads litterLevelPercentage 32 % (litterLevel ~484 mm, state REFILL); a normal
full one 90 % (~426-430 mm). So at or below ``alerts.litter_empty_percent``
(default 45) the alerts say the globe looks EMPTY.
"""
from conftest import make_raw

from detection import DetectionConfig, Detector, extract_fields

MIN = 60
NOTE = "globe looks EMPTY (empty globe reads ~32%)"


def feed(det, t, status="READY", **raw):
    return det.process(extract_fields(make_raw(**raw), status), float(t))


def litter(pct, state="REFILL", **extra):
    return dict(litterLevelPercentage=pct / 100, litterLevelState=state, **extra)


def stage1(det, pct, state="REFILL"):
    feed(det, 0)
    feed(det, 5 * MIN, **litter(pct, state))
    (alert,) = feed(det, 10 * MIN, **litter(pct, state))
    assert (alert.key, alert.kind) == ("litter_dump", "new")
    return alert


def litter_low(det, pct, state="REFILL"):
    """No 25-point step (so no dump incident): litter is low from the first reading."""
    feed(det, 0, **litter(pct, state))
    (alert,) = feed(det, 5 * MIN, **litter(pct, state))
    assert (alert.key, alert.kind) == ("litter_low", "new")
    return alert


def test_config_default_and_override():
    assert DetectionConfig().litter_empty_percent == 45
    assert DetectionConfig.from_config({}).litter_empty_percent == 45
    assert DetectionConfig.from_config({"litter_empty_percent": 35}).litter_empty_percent == 35


def test_dump_stage1_says_globe_looks_empty_at_or_below_the_threshold():
    text = stage1(Detector("COPTiP"), 40).plain
    assert "litter level dropped 90%→40%" in text
    assert f"Litter level 40% — {NOTE}." in text
    assert "into the waste drawer" not in text.replace("dump the litter into the drawer", "")
    assert "Do NOT let it cycle again" in text

    assert NOTE in stage1(Detector("COPTiP"), 45).plain        # boundary: <=

    # an earlier dump read 18 % / EMPTY: the state change opens the incident at
    # once, the confirmed 90→18 step then updates it with the stage-1 wording
    det = Detector("COPTiP")
    feed(det, 0)
    feed(det, 5 * MIN, **litter(18, "EMPTY"))
    (upd,) = feed(det, 10 * MIN, **litter(18, "EMPTY"))
    assert (upd.key, upd.kind) == ("litter_dump", "update")
    assert f"Litter level 18% — {NOTE}." in upd.plain


def test_dump_stage1_above_the_threshold_keeps_the_plain_wording():
    text = stage1(Detector("COPTiP"), 50).plain
    assert "litter level dropped 90%→50%" in text
    assert "EMPTY" not in text
    assert "EMPTY" not in stage1(Detector("COPTiP"), 45.5).plain


def test_dump_stage1_threshold_is_configurable_and_can_be_switched_off():
    cfg = DetectionConfig.from_config({"litter_empty_percent": 55})
    assert f"Litter level 50% — {NOTE}." in stage1(Detector("COPTiP", cfg), 50).plain
    off = DetectionConfig.from_config({"litter_empty_percent": 0})
    assert "EMPTY" not in stage1(Detector("COPTiP", off), 32).plain


def test_dump_reminder_uses_the_latest_litter_reading():
    det = Detector("COPTiP")
    assert "EMPTY" not in stage1(det, 60).plain                # 90 -> 60: not "empty" yet
    feed(det, 30 * MIN, **litter(40))                          # sinks further, no new 25-point step
    (rem,) = feed(det, 75 * MIN, **litter(40))
    assert (rem.key, rem.kind) == ("litter_dump", "repeat")
    assert "litter level dropped 90%→60%" in rem.plain
    assert f"Litter level 40% — {NOTE}." in rem.plain


def test_litter_low_says_globe_looks_empty_at_or_below_the_threshold():
    alert = litter_low(Detector("COPTiP"), 32)                 # the deep-clean reading
    assert f"litter level is <b>REFILL</b> (32%) — {NOTE}." in alert.text
    assert alert.plain.endswith("Add litter / check the globe.")
    assert NOTE in litter_low(Detector("COPTiP"), 45).plain
    assert NOTE in litter_low(Detector("COPTiP"), 18, "EMPTY").plain


def test_litter_low_above_the_threshold_or_without_a_percentage_keeps_the_plain_wording():
    alert = litter_low(Detector("COPTiP"), 70, "LOW")
    assert "litter level is <b>LOW</b> (70%). Add litter" in alert.text
    assert "EMPTY" not in alert.plain
    assert "looks EMPTY" not in litter_low(Detector("COPTiP"), 46).plain

    det = Detector("COPTiP")
    feed(det, 0, litterLevelPercentage=None, litterLevelState="REFILL")
    (alert,) = feed(det, 5 * MIN, litterLevelPercentage=None, litterLevelState="REFILL")
    assert "litter level is <b>REFILL</b>. Add litter" in alert.text

    off = DetectionConfig.from_config({"litter_empty_percent": 0})
    assert "looks EMPTY" not in litter_low(Detector("COPTiP", off), 32).plain
