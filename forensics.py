"""Append-only JSONL log of raw robot snapshots, one file per UTC day.

    <dir>/robot_snapshots-YYYY-MM-DD.jsonl       (today, plain text)
    <dir>/robot_snapshots-YYYY-MM-DD.jsonl.gz    (previous days, compressed)

Files older than ``retention_days`` are deleted, so the directory cannot grow
without bound. Everything here is local-only (the directory is gitignored).
"""
import gzip
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PREFIX = "robot_snapshots-"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


class SnapshotLog:
    def __init__(self, directory: str, retention_days: int = 90):
        self.dir = Path(directory)
        self.retention_days = int(retention_days)
        self.logger = logging.getLogger("forensics")
        self._last_maintenance_day: Optional[str] = None

    def path_for(self, day: str) -> Path:
        return self.dir / f"{PREFIX}{day}.jsonl"

    def append(self, record: Dict[str, Any], now: Optional[datetime] = None) -> Path:
        """Append one record (a dict) as a JSON line; rotates/prunes once a day."""
        now = now or datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(day)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=_json_default, separators=(",", ":")) + "\n")
        if day != self._last_maintenance_day:
            self._last_maintenance_day = day
            try:
                self.maintain(now)
            except Exception as e:  # never let housekeeping break logging
                self.logger.warning(f"Snapshot log maintenance failed: {e}")
        return path

    def maintain(self, now: Optional[datetime] = None) -> None:
        """Compress finished days, delete files past retention."""
        now = now or datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        cutoff = (now - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        for path in sorted(self.dir.glob(f"{PREFIX}*.jsonl*")):
            day = path.name[len(PREFIX):len(PREFIX) + 10]
            if self.retention_days > 0 and day < cutoff:
                path.unlink()
                self.logger.info(f"Deleted old snapshot log {path.name}")
            elif path.suffix == ".jsonl" and day < today:
                gz = path.with_name(path.name + ".gz")
                with open(path, "rb") as src, gzip.open(gz, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.unlink(path)
                self.logger.info(f"Compressed snapshot log {path.name}")
