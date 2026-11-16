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

from .validate import BadInput


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
    from .safefs import write_new
    out.parent.mkdir(parents=True, exist_ok=True)
    write_new(out, csv_text(rows).encode())
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


# The largest FIT file fitdata decodes: the same cap as for one file unpacked from a Garmin
# download. Real activity files are a few MB; decoding needs many times the file's size in memory.
MAX_FIT_BYTES = 64 * 1024 * 1024


def read_fit_file(file: str) -> tuple[bytes, str]:
    """The bytes of a FIT file given on the command line, refusing the saved login (by what the
    file is, not by its name), anything that is not a regular file, and very large files."""
    import os
    import stat

    from .errors import NotFound, Refused
    path = Path(file)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except (FileNotFoundError, NotADirectoryError):
        raise NotFound("There is no file at the path given to decode, so nothing was decoded.",
                       hint="Check the path of the FIT file and try again.") from None
    except IsADirectoryError:
        raise BadInput("decode needs a FIT file, but the path given is a folder.") from None
    except PermissionError:
        raise BadInput("fitdata may not read the file given to decode (permission denied).") from None
    try:
        st = os.fstat(fd)
        if _is_state_file(st, path):
            raise Refused("That file is in the folder that holds the saved Garmin login, so fitdata will not "
                          "decode it. Give it a FIT file from the archive or from the watch.")
        if stat.S_ISDIR(st.st_mode):
            raise BadInput("decode needs a FIT file, but the path given is a folder.")
        if not stat.S_ISREG(st.st_mode):
            raise BadInput("decode needs an ordinary FIT file, but the path given is a pipe, socket or device.")
        if st.st_size > MAX_FIT_BYTES:
            raise BadInput(f"That file is larger than {MAX_FIT_BYTES // 2**20} MB, far larger than any real FIT "
                           "file, so fitdata did not decode it.")
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read(MAX_FIT_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FIT_BYTES:
        raise BadInput(f"That file is larger than {MAX_FIT_BYTES // 2**20} MB, so fitdata did not decode it.")
    return data, path.name


def _is_state_file(st, path: Path) -> bool:
    from . import paths
    from .safefs import inside_state
    return inside_state(path, paths.STATE, st)


def decode_cli(file: str, as_csv: bool) -> int:
    from .storage import write_new
    data, name = read_fit_file(file)
    m = decode_bytes(data, name)
    if as_csv:
        stem = Path(name).stem
        for kind, rows in m.items():
            out = Path.cwd() / f"{stem}.{kind.removesuffix('_mesgs')}.csv"
            if rows:
                write_new(out, csv_text(rows).encode())
            print(out, len(rows))
    else:
        json.dump(m, sys.stdout, indent=1, default=str)
        print()
    return 0
