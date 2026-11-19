"""Pure detection logic for the Litter-Robot check.

No I/O, no network, no wall clock: every call takes ``now`` (epoch seconds) and
the whole detector state is a JSON-serialisable dict, so it can be persisted
across restarts and replayed in unit tests.

Signals
-------
* ``litter_dump``  — litter probably ended up in the waste drawer:
    - waste-drawer level steps up by >= ``waste_jump_points`` (default 25)
      within ``window_seconds`` and stays there for ``confirm_seconds``;
    - litter level steps down by >= ``litter_drop_points`` (default 25);
    - the empty-cycle odometer increments, or an EMPTY_CYCLE status is seen;
    - the litter-level state becomes EMPTY.
* ``litter_low``   — litter-level state is LOW / REFILL / EMPTY.
* ``globe_motor_fault`` / ``globe_retract_fault`` / ``pinch`` / ``usb_fault``
* ``status:<NAME>`` — any status classified "error" (drawer statuses excluded:
  the drawer alert trusts the percentage only).
* ``offline`` / ``api_unreachable`` — robot or Whisker cloud unreachable for
  longer than ``offline_seconds``.

Every active problem is an *incident*: alerted once, repeated every
``*_repeat_seconds`` until it resolves (or is acknowledged / expires).
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from status_codes import (
    ACTIVE_STATUSES,
    BUSY_STATUSES,
    DRAWER_STATUSES,
    STATUS_ALIASES,
    STATUS_CODES,
    canonical_set,
    canonical_status,
    describe_status,
)
from timeutil import fmt_duration

# reading key -> (raw Whisker key, label used in the change log)
FIELD_MAP: Dict[str, Tuple[str, str]] = {
    "waste": ("DFILevelPercent", "waste%"),
    "litter_state": ("litterLevelState", "litterState"),
    "litter_mm": ("litterLevel", "litterLevelMM"),
    "empty_cycles": ("odometerEmptyCycles", "emptyCycles"),
    "clean_cycles": ("odometerCleanCycles", "cleanCycles"),
    "power_cycles": ("odometerPowerCycles", "powerCycles"),
    "filter_cycles": ("odometerFilterCycles", "filterCycles"),
    "globe_fault": ("globeMotorFaultStatus", "globeMotorFault"),
    "retract_fault": ("globeMotorRetractFaultStatus", "globeRetractFault"),
    "pinch": ("pinchStatus", "pinch"),
    "usb_fault": ("USBFaultStatus", "usbFault"),
    "cycle_state": ("robotCycleState", "cycleState"),
    "cycle_status": ("robotCycleStatus", "cycleStatus"),
    "robot_status": ("robotStatus", "robotStatus"),
    "display_code": ("displayCode", "displayCode"),
    "online": ("isOnline", "online"),
    "bonnet_removed": ("isBonnetRemoved", "bonnetRemoved"),
    "dfi_full": ("isDFIFull", "dfiFull"),
    "dfi_partial_full": ("isDFIPartialFull", "dfiPartialFull"),
    "dfi_mm": ("DFILevelMM", "dfiMM"),
    "dfi_full_counter": ("DFIFullCounter", "dfiFullCounter"),
    "cat_detect": ("catDetect", "catDetect"),
    "laser_dirty": ("isLaserDirty", "laserDirty"),
    "power": ("unitPowerStatus", "power"),
    "power_type": ("unitPowerType", "powerType"),
    "hopper_status": ("hopperStatus", "hopper"),
    "sleep_status": ("sleepStatus", "sleep"),
    "wifi_mode": ("wifiModeStatus", "wifi"),
}

# Fields whose changes are written to the plain log (order = log order).
LOGGED_FIELDS: List[str] = [
    "status", "waste", "litter_pct", "litter_state", "litter_mm",
    "empty_cycles", "clean_cycles", "power_cycles",
    "globe_fault", "retract_fault", "pinch", "usb_fault",
    "cycle_state", "cycle_status", "robot_status", "display_code",
    "online", "bonnet_removed", "dfi_full", "dfi_partial_full", "dfi_mm",
    "dfi_full_counter", "cat_detect", "laser_dirty", "power", "power_type",
    "hopper_status", "sleep_status", "wifi_mode",
]
_LABELS = {k: v[1] for k, v in FIELD_MAP.items()}
_LABELS.update({"status": "status", "litter_pct": "litter%"})

LITTER_STATE_RANK = {"OVERFILL": 0, "OPTIMAL": 0, "LOW": 1, "REFILL": 2, "EMPTY": 3}
FAULT_OK = {None, "", "NONE", "FAULT_CLEAR", "CLEAR"}
PINCH_STATUSES = {"PINCH_DETECT", "STARTUP_PINCH_DETECT"}
_LR4_STATUS_MAP = {
    "ROBOT_BONNET": "BONNET_REMOVED",
    "ROBOT_CAT_DETECT": "CAT_DETECTED",
    "ROBOT_CAT_DETECT_DELAY": "CAT_SENSOR_TIMING",
    "ROBOT_CLEAN": "CLEAN_CYCLE",
    "ROBOT_EMPTY": "EMPTY_CYCLE",
    "ROBOT_FIND_DUMP": "CLEAN_CYCLE",
    "ROBOT_IDLE": "READY",
    "ROBOT_POWER_DOWN": "POWER_DOWN",
    "ROBOT_POWER_OFF": "OFF",
    "ROBOT_POWER_UP": "POWER_UP",
}


def _enum_str(value: Any) -> Any:
    """'GlobeMotorFaultStatus.FAULT_CLEAR' / enum -> 'FAULT_CLEAR'; pass through scalars."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).split(".")[-1]


def derive_status(raw: Dict[str, Any]) -> str:
    """Offline fallback mirroring pylitterbot's LR4 status logic (used when the
    caller has only the raw dict, e.g. when replaying a JSONL file)."""
    if raw.get("isOnline") is False:
        return "OFFLINE"
    cycle_state = raw.get("robotCycleState")
    if cycle_state == "CYCLE_STATE_CAT_DETECT":
        return "CAT_SENSOR_INTERRUPTED"
    if cycle_state == "CYCLE_STATE_PAUSE":
        return "PAUSED"
    status = _LR4_STATUS_MAP.get(raw.get("robotStatus"), "UNKNOWN")
    if status == "READY":
        if raw.get("displayCode") == "DC_CAT_DETECT":
            return "CAT_DETECTED"
        if raw.get("isDFIFull") is True:
            return "DRAWER_FULL"
    return status


def extract_fields(raw: Dict[str, Any], status: Any = None) -> Dict[str, Any]:
    """Reduce a raw ``robot.to_dict()`` snapshot to the fields we reason about."""
    raw = raw or {}
    reading: Dict[str, Any] = {
        "status": canonical_status(status) if status is not None else derive_status(raw),
    }
    for key, (raw_key, _label) in FIELD_MAP.items():
        reading[key] = _enum_str(raw.get(raw_key))
    pct = raw.get("litterLevelPercentage")
    reading["litter_pct"] = round(float(pct) * 100, 1) if isinstance(pct, (int, float)) else None
    if isinstance(reading["waste"], bool) or not isinstance(reading["waste"], (int, float)):
        reading["waste"] = None
    return reading


def diff_fields(old: Optional[Dict[str, Any]], new: Dict[str, Any],
                fields: Iterable[str] = LOGGED_FIELDS) -> List[Tuple[str, Any, Any]]:
    """Return [(label, old, new)] for every logged field that changed."""
    if not old:
        return []
    return [
        (_LABELS.get(f, f), old.get(f), new.get(f))
        for f in fields
        if old.get(f) != new.get(f)
    ]


def format_changes(changes: List[Tuple[str, Any, Any]]) -> str:
    return "; ".join(f"{label} {old}→{new}" for label, old, new in changes)


def is_settled(reading: Dict[str, Any]) -> bool:
    """True when the globe is at rest, i.e. waste/litter readings are trustworthy.

    Mid-cycle the drawer sensor reports transient values (often 0), so step
    detection only looks at settled readings.
    """
    if reading.get("online") is False:
        return False
    if reading.get("status") in BUSY_STATUSES | {"OFFLINE", "OFF", "UNKNOWN"}:
        return False
    return reading.get("cycle_status") in (None, "", "CYCLE_IDLE")


def is_active(reading: Dict[str, Any]) -> bool:
    """True when something is happening that deserves faster polling."""
    if reading.get("status") in ACTIVE_STATUSES:
        return True
    return reading.get("cycle_status") not in (None, "", "CYCLE_IDLE")


def _num(value: float) -> str:
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


@dataclass
class DetectionConfig:
    waste_jump_points: float = 25.0
    litter_drop_points: float = 25.0
    window_seconds: float = 900.0        # a step may build up over this long
    confirm_seconds: float = 300.0       # step must persist this long (0 = alert at once)
    max_gap_seconds: float = 21600.0     # ignore comparisons across longer monitoring gaps
    ignore_zero_waste: bool = True       # 0% right after emptying is not a baseline
    dump_repeat_seconds: float = 3600.0
    dump_max_alert_seconds: float = 86400.0
    litter_low_states: Tuple[str, ...] = ("LOW", "REFILL", "EMPTY")
    litter_low_repeat_seconds: float = 86400.0
    fault_repeat_seconds: float = 3600.0
    offline_seconds: float = 1800.0
    status_grace_seconds: float = 600.0  # bonnet removed / paused / off must persist this long
    error_statuses: frozenset = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, alerts: Optional[Dict[str, Any]]) -> "DetectionConfig":
        a = alerts or {}
        d = cls()
        return cls(
            waste_jump_points=float(a.get("dump_waste_jump_points", d.waste_jump_points)),
            litter_drop_points=float(a.get("dump_litter_drop_points", d.litter_drop_points)),
            window_seconds=float(a.get("dump_window_seconds", d.window_seconds)),
            confirm_seconds=float(a.get("dump_confirm_seconds", d.confirm_seconds)),
            max_gap_seconds=float(a.get("dump_max_gap_seconds", d.max_gap_seconds)),
            ignore_zero_waste=bool(a.get("dump_ignore_zero_baseline", d.ignore_zero_waste)),
            dump_repeat_seconds=float(a.get("dump_repeat_seconds", d.dump_repeat_seconds)),
            dump_max_alert_seconds=float(a.get("dump_max_alert_seconds", d.dump_max_alert_seconds)),
            litter_low_states=tuple(
                str(s).upper() for s in a.get("litter_low_states", d.litter_low_states)
            ),
            litter_low_repeat_seconds=float(
                a.get("litter_low_repeat_seconds", d.litter_low_repeat_seconds)
            ),
            fault_repeat_seconds=float(a.get("fault_repeat_seconds", d.fault_repeat_seconds)),
            offline_seconds=float(a.get("offline_minutes", d.offline_seconds / 60)) * 60,
            status_grace_seconds=float(a.get("status_grace_seconds", d.status_grace_seconds)),
            error_statuses=frozenset(canonical_set(a.get("error_statuses", []))),
        )


@dataclass
class Alert:
    key: str            # incident key, e.g. "litter_dump", "globe_motor_fault"
    kind: str           # "new" | "update" | "repeat" | "resolved"
    text: str           # Telegram HTML
    urgent: bool = True

    @property
    def critical(self) -> bool:
        """True for incidents that also go out as DMs and get an incident file."""
        return is_critical(self.key)

    @property
    def plain(self) -> str:
        """Text without HTML tags, for logs / DB."""
        import re
        return html.unescape(re.sub(r"<[^>]+>", "", self.text))


# Statuses that alert immediately (hardware faults).
_HARD_ERROR_STATUSES = {
    name for name, (_d, typ) in STATUS_CODES.items()
    if typ == "error" and name not in STATUS_ALIASES
} - DRAWER_STATUSES - {"OFFLINE", "BONNET_REMOVED"} - PINCH_STATUSES

# Incidents that need a human right now: besides the group alert they are sent
# as a DM to ``telegram.alert_dm_user_ids`` and get a forensic incident file.
# (Routine things — waste drawer %, low litter, USB, bonnet/paused — are not.)
CRITICAL_KEYS = {
    "litter_dump", "globe_motor_fault", "globe_retract_fault", "pinch",
    "offline", "api_unreachable",   # robot offline / monitor blind
}


def is_critical(key: str) -> bool:
    """Litter dumped, globe motor / retract fault, pinch, hardware error status, offline."""
    if key in CRITICAL_KEYS:
        return True
    return key.startswith("status:") and key.split(":", 1)[1] in _HARD_ERROR_STATUSES


class Detector:
    """Stateful, clock-free detector for one robot."""

    def __init__(self, name: str, config: Optional[DetectionConfig] = None,
                 state: Optional[Dict[str, Any]] = None):
        self.name = name
        self.cfg = config or DetectionConfig()
        self.state: Dict[str, Any] = state if isinstance(state, dict) else {}
        self.state.setdefault("last", None)
        self.state.setdefault("steps", {})
        self.state.setdefault("cond_since", {})
        self.state.setdefault("incidents", {})

    # ── public API ───────────────────────────────────────────────────────────

    @property
    def incidents(self) -> Dict[str, Dict[str, Any]]:
        return self.state["incidents"]

    def has_pending(self) -> bool:
        """True while a step / condition is waiting for confirmation."""
        if any(s.get("pending") for s in self.state["steps"].values()):
            return True
        return any(k not in self.incidents for k in self.state["cond_since"])

    def acknowledge(self) -> List[str]:
        """Silence repeats for all active incidents (new evidence re-alerts)."""
        keys = []
        for key, inc in self.incidents.items():
            if not inc.get("acked"):
                inc["acked"] = True
                keys.append(key)
        return keys

    def process(self, reading: Dict[str, Any], now: float) -> List[Alert]:
        """Feed one reading (from ``extract_fields``); returns alerts to send."""
        prev = self.state.get("last")
        alerts: List[Alert] = []
        alerts += self._dump(prev, reading, now)
        alerts += self._conditions(self._current_conditions(reading), now, exclude={"api_unreachable"})
        alerts += self._conditions({}, now, only={"api_unreachable"})  # we got data -> reachable
        self.state["last"] = dict(reading, ts=now)
        return alerts

    def process_unreachable(self, now: float) -> List[Alert]:
        """Call when the Whisker API could not be reached this cycle."""
        cond = {
            "api_unreachable": {
                "grace": self.cfg.offline_seconds,
                "repeat": self.cfg.fault_repeat_seconds,
                "text": lambda since: (
                    f"📡 <b>{self._n}</b>: the monitor has not been able to reach the Whisker "
                    f"cloud for {fmt_duration(now - since)} — the robot is NOT being watched."
                ),
                "resolved": f"✅ <b>{self._n}</b>: Whisker cloud reachable again, monitoring resumed.",
            }
        }
        return self._conditions(cond, now, only={"api_unreachable"})

    def process_reachable(self, now: float) -> List[Alert]:
        """Call when the Whisker API answered again."""
        return self._conditions({}, now, only={"api_unreachable"})

    # ── litter dump ──────────────────────────────────────────────────────────

    @property
    def _n(self) -> str:
        return html.escape(self.name)

    def _step(self, key: str, ts: float, value: float, direction: int,
              threshold: float) -> Optional[Dict[str, Any]]:
        """Track a level and report a confirmed step of >= threshold in `direction`."""
        st = self.state["steps"].setdefault(key, {"history": [], "pending": None})
        hist: List[List[float]] = st["history"]
        cutoff = ts - self.cfg.window_seconds
        recent = [h for h in hist if h[0] >= cutoff]
        if not recent:
            # "consecutive readings": keep the single previous reading unless
            # the monitoring gap is too long to mean anything.
            older = [h for h in hist if h[0] < cutoff and ts - h[0] <= self.cfg.max_gap_seconds]
            recent = older[-1:]
        hist[:] = recent

        result = None
        pending = st.get("pending")
        if pending:
            if (value - pending["from"]) * direction >= threshold:
                pending["to"] = value
                if ts - pending["since"] >= self.cfg.confirm_seconds:
                    result = pending
            else:
                st["pending"] = None  # transient blip
        elif hist:
            # lowest (highest for drops) recent reading; latest one on ties
            ref = min(reversed(hist), key=lambda h: h[1] * direction)
            if (value - ref[1]) * direction >= threshold:
                pending = {"since": ts, "from": ref[1], "from_ts": ref[0], "to": value}
                if self.cfg.confirm_seconds <= 0:
                    result = pending
                else:
                    st["pending"] = pending
        if result:
            st["pending"] = None
            hist.clear()  # new baseline
        hist.append([ts, value])
        return result

    def _dump_text(self, inc: Dict[str, Any]) -> str:
        return (
            f"🚨 <b>{self._n}</b>: litter appears to have been dumped into the waste drawer "
            f"({html.escape('; '.join(inc['reasons']))}). Check the globe has litter!"
        )

    def _dump(self, prev: Optional[Dict[str, Any]], r: Dict[str, Any], now: float) -> List[Alert]:
        cfg = self.cfg
        reasons: List[str] = []
        waste_before = None
        status = r.get("status")
        waste, litter = r.get("waste"), r.get("litter_pct")

        if prev:
            ec_prev, ec = prev.get("empty_cycles"), r.get("empty_cycles")
            if isinstance(ec_prev, (int, float)) and isinstance(ec, (int, float)) and ec > ec_prev:
                reasons.append(f"empty-cycle counter +{_num(ec - ec_prev)} ({_num(ec_prev)}→{_num(ec)})")
            if status == "EMPTY_CYCLE" and prev.get("status") != "EMPTY_CYCLE":
                reasons.append("robot reported an EMPTY cycle")
            if r.get("litter_state") == "EMPTY" and prev.get("litter_state") not in (None, "EMPTY"):
                reasons.append(f"litter level state {prev.get('litter_state')}→EMPTY")

        if is_settled(r):
            if waste is not None and not (cfg.ignore_zero_waste and waste <= 0):
                step = self._step("waste", now, float(waste), +1, cfg.waste_jump_points)
                if step:
                    waste_before = step["from"]
                    reasons.append(
                        f"waste drawer {_num(step['from'])}%→{_num(step['to'])}% in "
                        f"{fmt_duration(step['since'] - step['from_ts'])}"
                    )
            if litter is not None:
                step = self._step("litter", now, float(litter), -1, cfg.litter_drop_points)
                if step:
                    reasons.append(
                        f"litter level {_num(step['from'])}%→{_num(step['to'])}% in "
                        f"{fmt_duration(step['since'] - step['from_ts'])}"
                    )

        inc = self.incidents.get("litter_dump")
        if reasons:
            kind = "update" if inc else "new"
            if not inc:
                inc = self.incidents["litter_dump"] = {
                    "since": now, "reasons": [], "waste_before": None, "litter_min": litter,
                }
            inc["reasons"] += [x for x in reasons if x not in inc["reasons"]]
            if waste_before is not None and inc.get("waste_before") is None:
                inc["waste_before"] = waste_before
            inc["last_alert"] = now
            inc["acked"] = False
            inc["count"] = inc.get("count", 0) + 1
            return [Alert("litter_dump", kind, self._dump_text(inc))]
        if not inc:
            return []

        if litter is not None:
            inc["litter_min"] = litter if inc.get("litter_min") is None else min(inc["litter_min"], litter)
        if is_settled(r):
            cleared = None
            wb = inc.get("waste_before")
            if wb is not None and waste is not None and waste <= wb + cfg.waste_jump_points / 2:
                cleared = f"waste drawer back to {_num(waste)}%"
            elif (litter is not None and inc.get("litter_min") is not None
                  and litter - inc["litter_min"] >= cfg.litter_drop_points):
                cleared = f"litter level back up to {_num(litter)}%"
            if cleared:
                del self.incidents["litter_dump"]
                return [Alert("litter_dump", "resolved",
                              f"✅ <b>{self._n}</b>: litter-dump alert cleared ({cleared}).", urgent=False)]
        if cfg.dump_max_alert_seconds and now - inc["since"] >= cfg.dump_max_alert_seconds:
            del self.incidents["litter_dump"]  # stop nagging after a day
            return []
        if not inc.get("acked") and now - inc.get("last_alert", 0) >= cfg.dump_repeat_seconds:
            inc["last_alert"] = now
            inc["count"] = inc.get("count", 0) + 1
            return [Alert(
                "litter_dump", "repeat",
                f"🔁 Reminder ({fmt_duration(now - inc['since'])} unresolved, /ack to silence)\n"
                + self._dump_text(inc),
            )]
        return []

    # ── state conditions ─────────────────────────────────────────────────────

    def _current_conditions(self, r: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        cfg, n = self.cfg, self._n
        conds: Dict[str, Dict[str, Any]] = {}
        status = r.get("status")

        def fault(key: str, label: str, value: Any, urgent: bool = True) -> None:
            v = html.escape(str(value))
            conds[key] = {
                "grace": 0, "repeat": cfg.fault_repeat_seconds, "urgent": urgent,
                "text": lambda since: (
                    f"{'🚨' if urgent else '⚠️'} <b>{n}</b>: {label} — <code>{v}</code>. Check the robot!"
                ),
                "resolved": f"✅ <b>{n}</b>: {label} cleared.",
            }

        if r.get("globe_fault") not in FAULT_OK:
            fault("globe_motor_fault", "globe motor fault", r["globe_fault"])
        if r.get("retract_fault") not in FAULT_OK:
            fault("globe_retract_fault", "globe motor retract fault", r["retract_fault"])
        pinch = str(r.get("pinch") or "")
        if status in PINCH_STATUSES or "FAULT" in pinch.upper() or "PINCH" in pinch.upper() \
                or "PINCH" in str(r.get("globe_fault") or ""):
            fault("pinch", "pinch detected", pinch if status not in PINCH_STATUSES else status)
        if r.get("usb_fault") not in FAULT_OK:
            fault("usb_fault", "USB power fault", r["usb_fault"], urgent=False)

        soft = ({"BONNET_REMOVED"} | set(cfg.error_statuses)) - DRAWER_STATUSES - {"OFFLINE"} \
            - PINCH_STATUSES - _HARD_ERROR_STATUSES
        if status in _HARD_ERROR_STATUSES or status in soft:
            desc = html.escape(describe_status(status)[0])
            hard = status in _HARD_ERROR_STATUSES
            conds[f"status:{status}"] = {
                "grace": 0 if hard else cfg.status_grace_seconds,
                "repeat": cfg.fault_repeat_seconds,
                "urgent": hard,
                "text": lambda since: (
                    f"{'🚨' if hard else '⚠️'} <b>{n}</b>: {desc} (<code>{status}</code>)"
                    + (f" for {fmt_duration(self._now - since)}" if self._now - since >= 60 else "")
                    + "."
                ),
                "resolved": f"✅ <b>{n}</b>: {desc} — resolved.",
            }

        if r.get("online") is False or status == "OFFLINE":
            conds["offline"] = {
                "grace": cfg.offline_seconds, "repeat": cfg.fault_repeat_seconds,
                "text": lambda since: (
                    f"📡 <b>{n}</b> has been OFFLINE for {fmt_duration(self._now - since)}. "
                    f"Check power / Wi-Fi."
                ),
                "resolved": f"✅ <b>{n}</b> is back online.",
            }

        state = r.get("litter_state")
        if state in cfg.litter_low_states:
            pct = r.get("litter_pct")
            pct_s = f" ({_num(pct)}%)" if pct is not None else ""
            conds["litter_low"] = {
                "grace": cfg.confirm_seconds, "repeat": cfg.litter_low_repeat_seconds,
                "urgent": state == "EMPTY", "rank": LITTER_STATE_RANK.get(state, 1),
                "text": lambda since: (
                    f"{'🚨' if state == 'EMPTY' else '⚠️'} <b>{n}</b>: litter level is "
                    f"<b>{state}</b>{pct_s}. Add litter / check the globe."
                ),
                "resolved": f"✅ <b>{n}</b>: litter level is OK again.",
            }
        return conds

    _now: float = 0.0

    def _conditions(self, conds: Dict[str, Dict[str, Any]], now: float,
                    only: Optional[set] = None, exclude: Optional[set] = None) -> List[Alert]:
        """Generic machinery: grace period -> alert -> repeats -> resolved."""
        self._now = now
        since_map: Dict[str, float] = self.state["cond_since"]
        alerts: List[Alert] = []

        def in_scope(key: str) -> bool:
            if key == "litter_dump":
                return False
            if only is not None:
                return key in only
            return not (exclude and key in exclude)

        for key, c in conds.items():
            if not in_scope(key):
                continue
            since = since_map.setdefault(key, now)
            inc = self.incidents.get(key)
            if inc is None:
                if now - since < c.get("grace", 0):
                    continue
                self.incidents[key] = {
                    "since": since, "last_alert": now, "count": 1,
                    "rank": c.get("rank"), "resolved_text": c.get("resolved"),
                }
                alerts.append(Alert(key, "new", c["text"](since), c.get("urgent", True)))
                continue
            inc["resolved_text"] = c.get("resolved")
            worse = c.get("rank") is not None and c["rank"] > (inc.get("rank") or 0)
            if worse:
                inc["rank"] = c["rank"]
            due = not inc.get("acked") and now - inc.get("last_alert", 0) >= c["repeat"]
            if worse or due:
                inc["last_alert"] = now
                inc["count"] = inc.get("count", 0) + 1
                if worse:
                    inc["acked"] = False
                    alerts.append(Alert(key, "update", c["text"](since), c.get("urgent", True)))
                else:
                    alerts.append(Alert(
                        key, "repeat",
                        f"🔁 Reminder (/ack to silence)\n{c['text'](since)}", c.get("urgent", True),
                    ))

        for key in [k for k in list(since_map) if in_scope(k) and k not in conds]:
            del since_map[key]
            inc = self.incidents.pop(key, None)
            if inc and inc.get("resolved_text"):
                alerts.append(Alert(key, "resolved", inc["resolved_text"], urgent=False))
        return alerts
