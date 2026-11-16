"""fitdata.sqlite: an index rebuilt from the files (never edited by hand; delete it any time).

Tables
    days(date, has_data, complete, fetched_at, steps, resting_hr, sleep_score, hrv_last_night_avg, ...,
         dir)                           one row per archived day, columns = day.json "headline"
    activities(id, date, type, name, start_local, duration_s, distance_m, ascent_m, avg_hr,
               aerobic_te, training_load, vo2max, ..., dir, fit)    one row per activity
    files(path, kind, date, activity_id, bytes)                     every stored file

The archive is read through storage.archive() (plain files or a bucket); paths in the index are
the archive keys, which are the same as the files' paths relative to data_dir.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from . import paths, storage

log = logging.getLogger("fitdata")


def _manifests(a: storage.Archive, entries: list[storage.Entry]) -> tuple[list, list]:
    """(day key, day.json) and (activity key, activity.json) pairs, in path order."""
    days, acts = [], []
    for e in entries:
        parts = e.key.split("/")  # garmin, days, YYYY, date, ...
        if len(parts) == 5 and parts[4] == "day.json":
            target = days
        elif len(parts) == 7 and parts[4] == "activities" and parts[6] == "activity.json":
            target = acts
        else:
            continue
        m = a.read_json(e.key)
        if isinstance(m, dict):
            target.append((e.key.rsplit("/", 1)[0], m))
    return days, acts


# Column names come from the manifests' keys, which hold data from Garmin: only plain names
# become columns, so nothing in a key can change the SQL that builds the table.
COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _table(db: sqlite3.Connection, name: str, rows: list[dict], pk: str, base: tuple[str, ...] = ()) -> None:
    cols: list[str] = [pk, *base]
    seen = {c.lower() for c in cols}  # SQLite column names ignore case
    skipped: set[str] = set()
    for r in rows:
        for k in r:
            if k in cols or k in skipped:
                continue
            if not (isinstance(k, str) and COLUMN_RE.fullmatch(k)) or k.lower() in seen:
                skipped.add(k)
                continue
            cols.append(k)
            seen.add(k.lower())
    if skipped:
        shown = ", ".join(repr(str(k))[:40] for k in sorted(skipped, key=str)[:5])
        log.warning("index: left %d field name(s) out of the %s table because they are not plain column "
                    "names (letters, digits and _, at most 64, not repeating another column): %s",
                    len(skipped), name, shown)
    defs = ", ".join(f'"{c}"' + (" PRIMARY KEY" if c == pk else "") for c in cols)
    db.execute(f'DROP TABLE IF EXISTS "{name}"')
    db.execute(f'CREATE TABLE "{name}" ({defs})')
    q = f'INSERT OR REPLACE INTO "{name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({", ".join("?" * len(cols))})'
    db.executemany(q, [[(json.dumps(r.get(c)) if isinstance(r.get(c), (dict, list)) else r.get(c)) for c in cols] for r in rows])


def rebuild() -> None:
    paths.ROOT.mkdir(parents=True, exist_ok=True)  # a new machine: an empty index is still an index
    tmp = paths.DB.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)

    a = storage.archive()
    entries = a.walk(paths.DAYS_KEY)
    day_rows, act_rows = _manifests(a, entries)
    keys = {e.key for e in entries}

    days = []
    for key, m in day_rows:
        days.append({"date": m.get("date"), "has_data": m.get("has_data"), "complete": m.get("complete"),
                     "fetched_at": m.get("fetched_at"), **(m.get("headline") or {}),
                     "activity_count": len(m.get("activities") or []),
                     "dir": key})
    _table(db, "days", days, "date", ("has_data",))

    raw_fits: dict[str, list[str]] = {}
    for k in keys:
        folder, name = k.rsplit("/", 1)
        if name.endswith(".fit") and folder.endswith("/raw"):
            raw_fits.setdefault(folder[:-4], []).append(k)
    acts = []
    for key, m in act_rows:
        fit = min(raw_fits.get(key, []), default=None)
        acts.append({k: v for k, v in m.items() if k not in ("sources", "derived", "layout_version")}
                    | {"dir": key, "fit": fit, "records": (m.get("derived") or {}).get("records")})
    _table(db, "activities", acts, "id", ("date", "type", "name", "start_local", "dir", "fit"))

    files = []
    for e in entries:
        rel = e.key.split("/")[2:]  # YYYY, date, ...
        if len(rel) < 3:
            continue  # only files inside a day's folder
        kind = "/".join(x for x in rel[2:-1] if not x[:4].isdigit()) or "day"
        aid = rel[3].rsplit("-", 1)[-1] if len(rel) > 3 and rel[2] == "activities" else None
        files.append({"path": e.key, "kind": kind, "date": rel[1], "activity_id": aid, "bytes": e.size})
    _table(db, "files", files, "path", ("kind", "date", "activity_id", "bytes"))

    db.execute("CREATE INDEX IF NOT EXISTS act_date ON activities(date)")
    db.execute("CREATE INDEX IF NOT EXISTS files_date ON files(date)")
    db.commit()
    db.close()
    tmp.replace(paths.DB)
    log.info("index: %d days, %d activities, %d files -> %s", len(days), len(acts), len(files), paths.DB.name)
