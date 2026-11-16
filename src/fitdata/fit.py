"""Decode FIT files with Garmin's official FIT SDK into plain tables."""
from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from garmin_fit_sdk import Decoder, Stream


def decode(path: Path) -> dict[str, list[dict[str, Any]]]:
    return _decode(Stream.from_file(str(path)), path.name)


def decode_bytes(data: bytes, name: str = "FIT file") -> dict[str, list[dict[str, Any]]]:
    """Decode a FIT file held in memory (how it comes out of the archive, files or bucket)."""
    return _decode(Stream.from_byte_array(bytearray(data)), name)


def _decode(stream: Stream, name: str) -> dict[str, list[dict[str, Any]]]:
    decoder = Decoder(stream)
    messages, errors = decoder.read(convert_datetimes_to_dates=True, merge_heart_rates=True)
    if errors and not messages:
        raise ValueError(f"{name}: {errors[0]}")
    return messages


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "|".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, default=str)
    return v


def csv_text(rows: list[dict[str, Any]]) -> str:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if str(k) not in cols:
                cols.append(str(k))
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({str(k): _cell(v) for k, v in r.items()})
    return buf.getvalue()


def write_csv(rows: list[dict[str, Any]], out: Path) -> int:
    if not rows:
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(csv_text(rows), newline="")
    tmp.replace(out)
    return len(rows)


def derive_activity(fit_data: bytes, archive, derived_key: str, name: str = "FIT file") -> dict[str, Any]:
    """records.csv (one row per sample), laps.csv, and session.json (the FIT's own totals,
    including what the watch computed: training effect, load, etc.), written into the archive
    under derived_key."""
    m = decode_bytes(fit_data, name)
    records = m.get("record_mesgs", [])
    for rows, file in ((records, "records.csv"), (m.get("lap_mesgs", []), "laps.csv")):
        if rows:
            archive.write(f"{derived_key}/{file}", csv_text(rows).encode())
    sessions = m.get("session_mesgs", [])
    if sessions:
        archive.write(f"{derived_key}/session.json", json.dumps(sessions, indent=1, default=str).encode())
    return {"records": len(records), "laps": len(m.get("lap_mesgs", [])), "message_types": sorted(m)}


def decode_cli(file: str, as_csv: bool) -> int:
    from . import paths
    from .errors import Refused
    path = Path(file)
    if paths.is_secret(path):
        raise Refused("That file is in the folder that holds the saved Garmin login, so fitdata will not "
                      "decode it. Give it a FIT file from the archive or from the watch.")
    m = decode(path)
    if as_csv:
        for kind, rows in m.items():
            out = Path.cwd() / f"{path.stem}.{kind.removesuffix('_mesgs')}.csv"
            print(out, write_csv(rows, out))
    else:
        json.dump(m, sys.stdout, indent=1, default=str)
        print()
    return 0
