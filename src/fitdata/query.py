"""Read-only access to the archive, shaped for people and for AI agents (`--json`)."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import endpoints, paths


def _db() -> sqlite3.Connection:
    if not paths.DB.exists():
        raise SystemExit(f"No index at {paths.DB}. Run: fitdata index (or fitdata sync)")
    db = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _db() as db:
        return [dict(r) for r in db.execute(sql, params)]


def _range(frm: str | None, to: str | None, days: int | None) -> tuple[str, str]:
    end = to or date.today().isoformat()
    start = frm or (date.fromisoformat(end) - timedelta(days=(days or 7) - 1)).isoformat()
    return start, end


def days(frm: str | None = None, to: str | None = None, last: int | None = None,
         fields: str | None = None) -> list[dict[str, Any]]:
    start, end = _range(frm, to, last)
    rows = _rows("SELECT * FROM days WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))
    if fields:
        keep = ["date", *[f.strip() for f in fields.split(",") if f.strip()]]
        rows = [{k: r.get(k) for k in keep} for r in rows]
    else:
        rows = [{k: v for k, v in r.items() if k not in ("dir", "fetched_at", "complete")} for r in rows]
    return rows


def activities(frm: str | None = None, to: str | None = None, last: int | None = None,
               type_: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    start, end = _range(frm, to, last or 3650)
    sql = "SELECT * FROM activities WHERE date BETWEEN ? AND ?"
    params: list[Any] = [start, end]
    if type_:
        sql += " AND type LIKE ?"
        params.append(f"%{type_}%")
    sql += " ORDER BY start_local DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _rows(sql, tuple(params))


def summary(last: int = 7) -> dict[str, Any]:
    start, end = _range(None, None, last)
    return {"from": start, "to": end, "days": days(start, end),
            "activities": activities(start, end)}


def day(ds: str, source: str | None = None) -> Any:
    ddir = paths.day_dir(ds)
    manifest = _read(ddir / "day.json")
    if manifest is None:
        raise SystemExit(f"Nothing stored for {ds}")
    if source:
        data = _read(ddir / "processed" / f"{source}.json")
        if data is None:
            raise SystemExit(f"No '{source}' for {ds}. Available: {', '.join(available_day_sources(ds))}")
        return data
    return {"date": ds, "headline": manifest.get("headline"), "complete": manifest.get("complete"),
            "fetched_at": manifest.get("fetched_at"), "activities": manifest.get("activities"),
            "sources_with_data": available_day_sources(ds),
            "raw_files": sorted(p.name for p in (ddir / "raw" / "wellness").glob("*.fit")),
            "dir": str(ddir)}


def available_day_sources(ds: str) -> list[str]:
    m = _read(paths.day_dir(ds) / "day.json") or {}
    return [k for k, v in (m.get("sources") or {}).items() if v.get("status") == "ok"]


def activity(activity_id: str, source: str | None = None, records: bool = False,
             every: int = 1, columns: str | None = None) -> Any:
    adir = paths.find_activity_dir(activity_id)
    if adir is None:
        raise SystemExit(f"No activity {activity_id}")
    if records:
        path = adir / "derived" / "records.csv"
        if not path.exists():
            raise SystemExit("No decoded records for this activity")
        keep = [c.strip() for c in columns.split(",")] if columns else None
        with open(path, newline="") as fh:
            rows = [r for i, r in enumerate(csv.DictReader(fh)) if i % max(every, 1) == 0]
        if keep:
            rows = [{k: r.get(k) for k in keep} for r in rows]
        return rows
    if source:
        data = _read(adir / "processed" / f"{source}.json")
        if data is None:
            raise SystemExit(f"No '{source}' for activity {activity_id}")
        return data
    m = _read(adir / "activity.json") or {}
    m = {k: v for k, v in m.items() if k not in ("sources",)}
    m["sources_with_data"] = sorted(p.stem for p in (adir / "processed").glob("*.json"))
    m["files"] = sorted(str(p.relative_to(adir)) for p in adir.rglob("*") if p.is_file())
    m["dir"] = str(adir)
    return m


def sql(query: str) -> list[dict[str, Any]]:
    return _rows(query)


def schema() -> dict[str, Any]:
    with _db() as db:
        tables = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            tables[name] = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
    return {"database": str(paths.DB), "tables": tables,
            "notes": "Read-only. days.date and activities.date are local calendar dates (YYYY-MM-DD). "
                     "Full detail lives in the JSON/FIT files: use `fitdata day DATE --source NAME` and "
                     "`fitdata activity ID --source NAME|--records`."}


def sources() -> dict[str, Any]:
    d = endpoints.DESCRIPTIONS
    return {"daily": {k: d.get(k, "") for k in [*endpoints.DAILY, "raw_wellness_fit"]},
            "activity": {k: d.get(k, "") for k in [*endpoints.ACTIVITY, "raw_fit"]},
            "account": list(endpoints.ACCOUNT)}


def _read(path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
