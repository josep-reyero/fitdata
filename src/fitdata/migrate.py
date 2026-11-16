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
import stat
import sys
import time
from typing import Any

from . import paths, safefs, storage
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


MAX_LISTED = 20


def _raw_files() -> tuple[int, list[str]]:
    """(number of ordinary files, problems) under <data_dir>/garmin, looked at as they are on disk,
    without following anything. A problem is anything the archive's own reader leaves out, so a
    copy of it would silently differ from what is on disk: a symbolic link, a file with more than
    one name (hard link), a pipe or device, or a name the archive does not use (for example a
    leftover .tmp file or a name with spaces)."""
    top = paths.ROOT / storage.TOP
    problems: list[str] = []
    count = 0
    try:
        st = os.lstat(top)
    except FileNotFoundError:
        return 0, []
    if not stat.S_ISDIR(st.st_mode):
        return 0, [f"{storage.TOP}: {safefs._what(st).removeprefix('it ')}"]

    def visit(folder: str) -> None:
        nonlocal count
        try:
            names = sorted(os.listdir(paths.ROOT / folder))
        except OSError as e:
            problems.append(f"{folder}: could not be listed ({e.strerror})")
            return
        for name in names:
            key = f"{folder}/{name}"
            try:
                st = os.lstat(paths.ROOT / key)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if not storage._valid_segment(name):
                    problems.append(f"{key}/: a folder name the archive does not use")
                else:
                    visit(key)
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                if storage._valid_segment(name):
                    count += 1
                else:
                    problems.append(f"{key}: a file name the archive does not use")
            else:
                problems.append(f"{key}: {safefs._what(st).removeprefix('it ')}")
    visit(storage.TOP)
    return count, problems


def _refuse_odd_files(problems: list[str]) -> Refused:
    shown = "; ".join(problems[:MAX_LISTED])
    more = f"; and {len(problems) - MAX_LISTED} more" if len(problems) > MAX_LISTED else ""
    return Refused(f"fitdata did not move the archive: {len(problems)} thing(s) in it would not be copied "
                   f"as they are, so the copy would not match what is on disk: {shown}{more}. Nothing was "
                   "copied or switched.",
                   hint="Replace each link with the ordinary file or folder it should be (or remove it), "
                   "remove leftover .tmp files and anything that does not belong in the archive, then run "
                   "the same command again.")


def migrate(to: str, progress: bool = True) -> dict[str, Any]:
    global _quiet
    _quiet = not progress
    cfg = load()
    source_kind = cfg.storage
    if to == source_kind:
        return {"changed": False, "storage": to,
                "message": f"The archive is already stored as {to} ({_where(to)}), so nothing was copied."}

    raw_count = None
    if source_kind == "files":
        raw_count, problems = _raw_files()
        if problems:
            raise _refuse_odd_files(problems)

    src = storage.open_archive(source_kind, write=False)
    dst = storage.open_archive(to, write=True)
    try:
        entries = src.walk(storage.TOP)
        if raw_count is None:
            raw_count = len(entries)
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

    try:
        written = set_storage(to)
    except Refused as e:
        raise Refused(f"Every object was copied to {_where(to)} and checked, but {e.message} The archive is "
                      f"still read from {_where(source_kind)}, and nothing was deleted.", hint=e.hint) from None
    storage.close_all()
    complete = raw_count == len(expected) == len(entries)
    result: dict[str, Any] = {
        "changed": True, "from": source_kind, "to": to, "objects": len(expected), "bytes": total,
        "verified": len(expected), "source_files": raw_count, "config": str(written),
        "source_kept": _where(source_kind),
        "remove_source_with": _remove_command(source_kind) if complete else None,
        "extra_in_destination": len(extra),
        "message": (f"Copied and checked {len(expected)} objects ({total / 2**20:.1f} MiB). fitdata now reads and "
                    f"writes the archive in {_where(to)} (storage = \"{to}\" in {written}). The old copy in "
                    f"{_where(source_kind)} was kept"),
    }
    if complete:
        result["message"] += f"; when you are sure, remove it with: {_remove_command(source_kind)}"
    else:
        result["message"] += (f". It holds {raw_count} files but {len(expected)} were copied, so fitdata does "
                              "not suggest removing it: compare the two first.")
    if extra:
        result["message"] += (f" Note: the destination already held {len(extra)} objects that the source does "
                              "not have (from an earlier copy); they were left as they are.")
    if os.environ.get("FITDATA_STORAGE") not in (None, "", to):
        result["message"] += (" FITDATA_STORAGE is set in this environment and still overrides the config; "
                              "change or remove it.")
    return result
