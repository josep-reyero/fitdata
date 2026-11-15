"""fitdata.sqlite: an index rebuilt from the files (never edited by hand; delete it any time).

Tables
    days(date, has_data, complete, fetched_at, steps, resting_hr, sleep_score, hrv_last_night_avg, ...,
         dir)                           one row per archived day, columns = day.json "headline"
    activities(id, date, type, name, start_local, duration_s, distance_m, ascent_m, avg_hr,
               aerobic_te, training_load, vo2max, ..., dir, fit)    one row per activity
    files(path, kind, date, activity_id, bytes)                     every stored file
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from . import paths

log = logging.getLogger("fitdata")


def _rows(pattern: str) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(paths.DAYS.glob(pattern)):
        try:
            out.append((p, json.loads(p.read_text())))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _table(db: sqlite3.Connection, name: str, rows: list[dict], pk: str, base: tuple[str, ...] = ()) -> None:
    cols: list[str] = [pk, *base]
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    defs = ", ".join(f'"{c}"' + (" PRIMARY KEY" if c == pk else "") for c in cols)
    db.execute(f'DROP TABLE IF EXISTS "{name}"')
    db.execute(f'CREATE TABLE "{name}" ({defs})')
    q = f'INSERT OR REPLACE INTO "{name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({", ".join("?" * len(cols))})'
    db.executemany(q, [[(json.dumps(r.get(c)) if isinstance(r.get(c), (dict, list)) else r.get(c)) for c in cols] for r in rows])


def rebuild() -> None:
    tmp = paths.DB.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)

    days = []
    for p, m in _rows("*/*/day.json"):
        days.append({"date": m.get("date"), "has_data": m.get("has_data"), "complete": m.get("complete"),
                     "fetched_at": m.get("fetched_at"), **(m.get("headline") or {}),
                     "activity_count": len(m.get("activities") or []),
                     "dir": str(p.parent.relative_to(paths.ROOT))})
    _table(db, "days", days, "date", ("has_data",))

    acts = []
    for p, m in _rows("*/*/activities/*/activity.json"):
        fit = next(iter(sorted((p.parent / "raw").glob("*.fit"))), None)
        acts.append({k: v for k, v in m.items() if k not in ("sources", "derived", "layout_version")}
                    | {"dir": str(p.parent.relative_to(paths.ROOT)),
                       "fit": str(fit.relative_to(paths.ROOT)) if fit else None,
                       "records": (m.get("derived") or {}).get("records")})
    _table(db, "activities", acts, "id", ("date", "type", "name", "start_local", "dir", "fit"))

    files = []
    for f in paths.DAYS.rglob("*"):
        if not f.is_file() or f.name.endswith(".tmp"):
            continue
        rel = f.relative_to(paths.DAYS).parts  # YYYY, date, ...
        kind = "/".join(x for x in rel[2:-1] if not x[:4].isdigit()) or "day"
        aid = rel[3].rsplit("-", 1)[-1] if len(rel) > 3 and rel[2] == "activities" else None
        files.append({"path": str(f.relative_to(paths.ROOT)), "kind": kind, "date": rel[1],
                      "activity_id": aid, "bytes": f.stat().st_size})
    _table(db, "files", files, "path", ("kind", "date", "activity_id", "bytes"))

    db.execute("CREATE INDEX IF NOT EXISTS act_date ON activities(date)")
    db.execute("CREATE INDEX IF NOT EXISTS files_date ON files(date)")
    db.commit()
    db.close()
    tmp.replace(paths.DB)
    log.info("index: %d days, %d activities, %d files -> %s", len(days), len(acts), len(files), paths.DB.name)
