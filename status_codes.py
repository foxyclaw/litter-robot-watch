"""Single source of truth for Litter-Robot status names.

pylitterbot reports LR4 statuses as enum *names* (READY, CLEAN_CYCLE,
BONNET_REMOVED, ...). Older configs and the LR3 API use short codes (RDY, CCP,
BR, ...). Everything is normalised to the enum name via ``canonical_status``.
"""
from typing import Dict, Iterable, Set, Tuple

# name -> (description, type). type: normal | activity | warning | error | inactive
STATUS_CODES: Dict[str, Tuple[str, str]] = {
    "READY": ("Ready", "normal"),
    "CLEAN_CYCLE": ("Clean cycle in progress", "activity"),
    "CLEAN_CYCLE_COMPLETE": ("Clean cycle complete", "activity"),
    "CAT_DETECTED": ("Cat detected", "activity"),
    "CAT_SENSOR_TIMING": ("Cat sensor timing", "activity"),
    "CAT_SENSOR_INTERRUPTED": ("Cat sensor interrupted", "warning"),
    "CAT_SENSOR_FAULT": ("Cat sensor fault", "error"),
    "STARTUP_CAT_SENSOR_FAULT": ("Cat sensor fault at startup", "error"),
    "DRAWER_FULL_1": ("Drawer almost full - 2 cycles left", "warning"),
    "DRAWER_FULL_2": ("Drawer almost full - 1 cycle left", "warning"),
    "DRAWER_FULL": ("Drawer full", "error"),
    "STARTUP_DRAWER_FULL": ("Drawer full at startup", "error"),
    "DUMP_HOME_POSITION_FAULT": ("Dump + home position fault", "error"),
    "DUMP_POSITION_FAULT": ("Dump position fault", "error"),
    "HOME_POSITION_FAULT": ("Home position fault", "error"),
    "OVER_TORQUE_FAULT": ("Over torque fault", "error"),
    "PINCH_DETECT": ("Pinch detected", "error"),
    "STARTUP_PINCH_DETECT": ("Pinch detected at startup", "error"),
    "BONNET_REMOVED": ("Bonnet removed", "error"),
    "EMPTY_CYCLE": ("Empty cycle", "warning"),
    "PAUSED": ("Clean cycle paused", "warning"),
    "POWER_DOWN": ("Powering down", "inactive"),
    "POWER_UP": ("Powering up", "activity"),
    "OFF": ("Off", "inactive"),
    "OFFLINE": ("Offline", "error"),
    "UNKNOWN": ("Unknown", "unknown"),
}

# Short (LR3 / legacy config) code -> canonical name
STATUS_ALIASES: Dict[str, str] = {
    "RDY": "READY",
    "CCP": "CLEAN_CYCLE",
    "CCC": "CLEAN_CYCLE_COMPLETE",
    "CD": "CAT_DETECTED",
    "CST": "CAT_SENSOR_TIMING",
    "CSI": "CAT_SENSOR_INTERRUPTED",
    "CSF": "CAT_SENSOR_FAULT",
    "SCF": "STARTUP_CAT_SENSOR_FAULT",
    "DF1": "DRAWER_FULL_1",
    "DF2": "DRAWER_FULL_2",
    "DFS": "DRAWER_FULL",
    "SDF": "STARTUP_DRAWER_FULL",
    "DHF": "DUMP_HOME_POSITION_FAULT",
    "DPF": "DUMP_POSITION_FAULT",
    "HPF": "HOME_POSITION_FAULT",
    "OTF": "OVER_TORQUE_FAULT",
    "PD": "PINCH_DETECT",
    "SPF": "STARTUP_PINCH_DETECT",
    "BR": "BONNET_REMOVED",
    "EC": "EMPTY_CYCLE",
    "P": "PAUSED",
    "PWRD": "POWER_DOWN",
    "PWRU": "POWER_UP",
}

# Short codes keep working as lookup keys (old DB rows, old configs).
for _code, _name in STATUS_ALIASES.items():
    STATUS_CODES.setdefault(_code, STATUS_CODES[_name])

# Waste-drawer statuses: never alerted as "errors" — the drawer alert trusts
# the percentage only (the robot's full flag is flaky).
DRAWER_STATUSES: Set[str] = {
    "DRAWER_FULL", "DRAWER_FULL_1", "DRAWER_FULL_2", "STARTUP_DRAWER_FULL",
}

# Statuses during which sensor readings (waste %, litter %) are in flux.
BUSY_STATUSES: Set[str] = {
    "CLEAN_CYCLE", "EMPTY_CYCLE", "PAUSED", "CAT_SENSOR_INTERRUPTED",
    "POWER_UP", "POWER_DOWN",
}

# Statuses that mean "something is happening" -> poll faster.
ACTIVE_STATUSES: Set[str] = BUSY_STATUSES | {
    "CAT_DETECTED", "CAT_SENSOR_TIMING", "CLEAN_CYCLE_COMPLETE",
}


def canonical_status(status) -> str:
    """'LitterBoxStatus.BONNET_REMOVED' / 'BR' / 'BONNET_REMOVED' -> 'BONNET_REMOVED'."""
    s = str(status if status is not None else "UNKNOWN").split(".")[-1].strip()
    return STATUS_ALIASES.get(s, s)


def canonical_set(statuses: Iterable[str]) -> Set[str]:
    return {canonical_status(s) for s in (statuses or [])}


def describe_status(status) -> Tuple[str, str]:
    """Return (description, type) for any status spelling."""
    name = canonical_status(status)
    return STATUS_CODES.get(name, (name.replace("_", " ").title(), "unknown"))
