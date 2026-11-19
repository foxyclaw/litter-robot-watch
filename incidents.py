"""Forensic incident files: one JSON document per critical incident.

    <dir>/<YYYYmmdd-HHMMSS>-<kind>.json      (timestamp = UTC time the incident opened)

Written when a critical incident opens (trigger reason, recent raw snapshots,
recent activity history, the current raw robot dict), then updated in place:
fresh activity history right after opening, every reminder, and a final
snapshot when the incident resolves. Local-only (the directory is gitignored).
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from forensics import _json_default


def utc_iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")


def safe_kind(key: str) -> str:
    """'status:OVER_TORQUE_FAULT' -> 'status-OVER_TORQUE_FAULT' (safe in a file name)."""
    return re.sub(r"[^A-Za-z0-9_.]+", "-", key).strip("-") or "incident"


class IncidentStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)

    def _write(self, path: Path, doc: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, default=_json_default, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)  # never leave a half-written incident file

    def create(self, key: str, now: float, doc: Dict[str, Any], suffix: str = "") -> Path:
        """Write a new incident file and return its path (never overwrites)."""
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{safe_kind(key)}{suffix}"
        path, n = self.dir / f"{base}.json", 1
        while path.exists():
            n += 1
            path = self.dir / f"{base}-{n}.json"
        self._write(path, doc)
        return path

    def read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else None
        except (OSError, ValueError):
            return None

    def update(self, path: Path, append: Optional[Dict[str, Any]] = None, **sections: Any) -> bool:
        """Set top-level ``sections`` and append to list sections. False if the file is gone."""
        doc = self.read(path)
        if doc is None:
            return False
        doc.update(sections)
        for name, item in (append or {}).items():
            doc.setdefault(name, []).append(item)
        self._write(Path(path), doc)
        return True
