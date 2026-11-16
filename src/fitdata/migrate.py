"""`fitdata migrate-storage --to bucket|files`: move the archive between plain files and a bucket.

1. Copies every object under garmin/ (days, activities, account) from the current backend to the
   other one, at the same keys. .state/ (the saved login), the logs and the index are not part of
   the archive and stay where they are, as plain files.
2. Reads every copied object back from the destination and compares its SHA-256 with the source.
3. Only when every object matched, writes `storage = "<to>"` into fitdata's config file.
4. Never deletes the source. It prints the command that removes it, for when you are sure.

Safe to run again: copying the same content again changes nothing. Run it while no sync runs
(it takes the same lock as sync, so a timer that starts meanwhile waits for the next turn).
"""
from __future__ import annotations

import hashlib
import os
import shlex
import sys
import time
from typing import Any

from . import paths, storage
from .config import load, set_storage
from .errors import FitdataError, Refused

PROGRESS_EVERY_S = 5.0


_quiet = False


def _say(text: str) -> None:
    if not _quiet:  # with --json, stderr carries only a JSON error, if any
        print(text, file=sys.stderr, flush=True)


def _remove_command(kind: str) -> str:
    cfg = load()
    if kind == "files":
        return f"rm -rf -- {shlex.quote(str(paths.ROOT / 'garmin'))}"
    return f"mybucket --store {shlex.quote(str(cfg.store_dir))} rb {cfg.bucket_name} --force"


def _where(kind: str) -> str:
    cfg = load()
    return str(paths.ROOT / "garmin") if kind == "files" else f'bucket "{cfg.bucket_name}" in {cfg.store_dir}'


def migrate(to: str, progress: bool = True) -> dict[str, Any]:
    global _quiet
    _quiet = not progress
    cfg = load()
    source_kind = cfg.storage
    if to == source_kind:
        return {"changed": False, "storage": to,
                "message": f"The archive is already stored as {to} ({_where(to)}), so nothing was copied."}

    src = storage.open_archive(source_kind, write=False)
    dst = storage.open_archive(to, write=True)
    try:
        entries = src.walk(storage.TOP)
        _say(f"Copying {len(entries)} objects from {_where(source_kind)} to {_where(to)} ...")
        expected: dict[str, tuple[str, int]] = {}
        total = 0
        last = time.monotonic()
        for i, e in enumerate(entries, 1):
            data = src.read(e.key)
            if data is None:
                raise FitdataError(f"fitdata could not read {e.key} from {_where(source_kind)} while copying, so it "
                                   f"stopped. Nothing was switched: the archive is still read from {source_kind}.",
                                   hint="Check that the file is readable, then run the same command again.")
            expected[e.key] = (hashlib.sha256(data).hexdigest(), len(data))
            total += len(data)
            dst.write(e.key, data)
            if time.monotonic() - last > PROGRESS_EVERY_S:
                _say(f"  copied {i} of {len(entries)}")
                last = time.monotonic()

        _say("Checking every copied object against the original ...")
        bad = []
        for key, (digest, size) in expected.items():
            got = dst.read(key)
            if got is None or len(got) != size or hashlib.sha256(got).hexdigest() != digest:
                bad.append(key)
        if bad:
            raise Refused(f"{len(bad)} of {len(expected)} objects did not read back the same from the copy in "
                          f"{_where(to)} (first: {bad[0]}), so fitdata did not switch. The archive is still read "
                          f"from {_where(source_kind)}, and nothing was deleted.",
                          hint="Run the same command again; if it keeps failing, check the disk.")
        extra = [e.key for e in dst.walk(storage.TOP) if e.key not in expected]
    finally:
        src.close()
        dst.close()

    written = set_storage(to)
    storage.close_all()
    result: dict[str, Any] = {
        "changed": True, "from": source_kind, "to": to, "objects": len(expected), "bytes": total,
        "verified": len(expected), "config": str(written),
        "source_kept": _where(source_kind),
        "remove_source_with": _remove_command(source_kind),
        "extra_in_destination": len(extra),
        "message": (f"Copied and checked {len(expected)} objects ({total / 2**20:.1f} MiB). fitdata now reads and "
                    f"writes the archive in {_where(to)} (storage = \"{to}\" in {written}). The old copy in "
                    f"{_where(source_kind)} was kept; when you are sure, remove it with: "
                    f"{_remove_command(source_kind)}"),
    }
    if extra:
        result["message"] += (f" Note: the destination already held {len(extra)} objects that the source does "
                              "not have (from an earlier copy); they were left as they are.")
    if os.environ.get("FITDATA_STORAGE") not in (None, "", to):
        result["message"] += (" FITDATA_STORAGE is set in this environment and still overrides the config; "
                              "change or remove it.")
    return result
