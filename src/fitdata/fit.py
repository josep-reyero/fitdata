"""Decode FIT files with Garmin's official FIT SDK into plain tables."""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from garmin_fit_sdk import Decoder, Stream


def decode(path: Path) -> dict[str, list[dict[str, Any]]]:
    stream = Stream.from_file(str(path))
    decoder = Decoder(stream)
    messages, errors = decoder.read(convert_datetimes_to_dates=True, merge_heart_rates=True)
    if errors and not messages:
        raise ValueError(f"{path.name}: {errors[0]}")
    return messages


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "|".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, default=str)
    return v


def write_csv(rows: list[dict[str, Any]], out: Path) -> int:
    if not rows:
        return 0
    cols: list[str] = []
    for r in rows:
        for k in r:
            if str(k) not in cols:
                cols.append(str(k))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({str(k): _cell(v) for k, v in r.items()})
    tmp.replace(out)
    return len(rows)


def derive_activity(fit_path: Path, derived: Path) -> dict[str, Any]:
    """records.csv (one row per sample), laps.csv, and session.json (the FIT's own totals,
    including what the watch computed: training effect, load, etc.)."""
    m = decode(fit_path)
    n = write_csv(m.get("record_mesgs", []), derived / "records.csv")
    write_csv(m.get("lap_mesgs", []), derived / "laps.csv")
    sessions = m.get("session_mesgs", [])
    if sessions:
        (derived / "session.json").write_text(json.dumps(sessions, indent=1, default=str))
    return {"records": n, "laps": len(m.get("lap_mesgs", [])), "message_types": sorted(m)}


def decode_cli(file: str, as_csv: bool) -> int:
    path = Path(file)
    m = decode(path)
    if as_csv:
        for kind, rows in m.items():
            out = Path.cwd() / f"{path.stem}.{kind.removesuffix('_mesgs')}.csv"
            print(out, write_csv(rows, out))
    else:
        json.dump(m, sys.stdout, indent=1, default=str)
        print()
    return 0
