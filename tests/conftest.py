import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def make_raw(**overrides):
    """A raw LR4 ``robot.to_dict()`` in its normal idle state (keys as Whisker sends them)."""
    raw = {
        "unitId": "robot-1",
        "name": "COPTiP",
        "serial": "LR4TEST",
        "robotStatus": "ROBOT_IDLE",
        "robotCycleStatus": "CYCLE_IDLE",
        "robotCycleState": "CYCLE_STATE_WAIT_ON",
        "displayCode": "DC_MODE_IDLE",
        "isOnline": True,
        "DFILevelPercent": 19,
        "DFILevelMM": 109,
        "isDFIFull": False,
        "isDFIPartialFull": False,
        "litterLevel": 426,
        "litterLevelPercentage": 0.9,
        "litterLevelState": "OPTIMAL",
        "odometerEmptyCycles": 3,
        "odometerCleanCycles": 3700,
        "odometerPowerCycles": 78,
        "globeMotorFaultStatus": "FAULT_CLEAR",
        "globeMotorRetractFaultStatus": "FAULT_CLEAR",
        "pinchStatus": "SWITCH_2_SET",
        "USBFaultStatus": "CLEAR",
        "catDetect": "CAT_DETECT_CLEAR",
        "isBonnetRemoved": False,
        "isLaserDirty": False,
        "unitPowerStatus": "ON",
    }
    raw.update(overrides)
    return raw
