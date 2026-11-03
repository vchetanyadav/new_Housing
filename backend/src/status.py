"""Records where each dataset came from, when, how much, and any quality warnings.

Written to DATA_DIR/status.json by the ingest, satellite, process and verify steps, and shown
in the app so planners can see how current each layer is and what to be careful about.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile

from . import config as C


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load() -> dict:
    try:
        return json.loads(C.STATUS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sources": {}, "processing": {}, "verify": {}}


def _save(data: dict) -> None:
    C.STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=C.STATUS_FILE.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, C.STATUS_FILE)


def source(name: str, *, label: str, url: str | None = None, rows: int | None = None,
           state: str = "ok", note: str = "", updates: str = "", retrieved_at: str | None = None) -> None:
    """state: ok | warning | failed | skipped"""
    data = load()
    prev = data["sources"].get(name, {})
    data["sources"][name] = {
        "label": label, "url": url or prev.get("url"), "rows": rows if rows is not None else prev.get("rows"),
        "state": state, "note": note, "updates": updates or prev.get("updates", ""),
        "retrieved_at": retrieved_at or (now() if state in ("ok", "warning") else prev.get("retrieved_at")),
        "checked_at": now(),
    }
    _save(data)


def processing(**info) -> None:
    data = load()
    data["processing"] = {"processed_at": now(), **info}
    _save(data)


def verify(results: list[dict]) -> None:
    data = load()
    data["verify"] = {"checked_at": now(), "results": results}
    _save(data)


def file_age_days(path) -> float | None:
    try:
        return (dt.datetime.now().timestamp() - os.path.getmtime(path)) / 86400
    except FileNotFoundError:
        return None
