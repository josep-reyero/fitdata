"""Where the day and activity files live: plain files (default) or a my-bucket-storage bucket.

Everything under <data_dir>/garmin/ goes through one small interface, used by sync, query, FIT
decoding and the index:

    a = archive()                 # read-only (every query command)
    a = archive(write=True)       # sync, backfill, account, migrate-storage
    a.read(key) -> bytes | None          a.read_json(key, default)
    a.write(key, bytes | chunks)         a.write_json(key, data)
    a.exists(key)                        a.walk(prefix) -> [Entry(key, size)]
    a.children(prefix) -> (folders, files)                a.location(key) -> str

A key is the file's path relative to data_dir with "/" between parts, for example
garmin/days/2026/2026-11-14/processed/sleep.json, so both backends hold the same keys and the
index is the same for both. Keys are checked in one place (check_key): they must start with
garmin/, and no part may start with "." or be "..". So no key can name anything in .state/ (the
saved Garmin login), which stays a plain file and never goes into a bucket. The files backend
reaches every file name by name from the opened garmin/ folder and never follows a link
(safefs.py), so a link in the archive leads nowhere, and it hands out only regular files with a
single name that are not the saved login.

The bucket backend needs the optional `mybucket` package (`fitdata[bucket]`); without it,
storage = "files" works and storage = "bucket" says how to install it.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import paths, safefs
from .config import load
from .errors import ConfigProblem, FitdataError, NotFound, Refused, Temporary
from .validate import BadInput, check_activity_id

SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,200}$")
MAX_KEY_BYTES = 512
TOP = "garmin"


@dataclass(frozen=True)
class Entry:
    key: str
    size: int


def _valid_segment(seg: str) -> bool:
    return bool(SEGMENT_RE.fullmatch(seg)) and not seg.endswith(".tmp")


def check_key(key: str, what: str = "key") -> str:
    """A key inside the archive: garmin/<parts>, each part a plain name (no ".", "..", spaces)."""
    if isinstance(key, str) and key.isascii() and len(key) <= MAX_KEY_BYTES:
        parts = key.split("/")
        if parts[0] == TOP and all(_valid_segment(p) for p in parts):
            return key
    shown = repr(str(key))[:80]
    raise BadInput(f"{shown} is not a {what} inside the archive, so fitdata did not use it. Archive keys "
                   "start with garmin/ and are made of plain names separated by '/'.")


def _sort(entries: Iterable[Entry]) -> list[Entry]:
    # The order of Path objects (part by part), so the index rows come out as they always did.
    return sorted(entries, key=lambda e: e.key.split("/"))


def _content_type(key: str) -> str:
    return {"json": "application/json", "csv": "text/csv"}.get(key.rsplit(".", 1)[-1].lower(),
                                                               "application/octet-stream")


class Archive:
    kind = "?"
    readonly = True

    def read(self, key: str) -> bytes | None: ...
    def write(self, key: str, data: bytes | Iterable[bytes]) -> int: ...
    def exists(self, key: str) -> bool: ...
    def walk(self, prefix: str) -> list[Entry]: ...
    def children(self, prefix: str) -> tuple[list[str], list[str]]: ...
    def location(self, key: str) -> str: ...
    def close(self) -> None: ...

    def read_json(self, key: str, default: Any = None) -> Any:
        data = self.read(key)
        if data is None:
            return default
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return default

    def write_json(self, key: str, data: Any) -> int:
        return self.write(key, json.dumps(data, indent=1, ensure_ascii=False, default=str).encode())

    def _require_writable(self) -> None:
        if self.readonly:
            raise FitdataError("fitdata opened the archive read-only for this command, so nothing was written.")


def _indexed_activity(a: Archive, activity_id: str) -> str | None:
    """Where the index says the activity is, if that answer is a real activity folder in the
    archive (checked; the index is only a hint)."""
    if not paths.DB.exists():
        return None
    try:
        db = sqlite3.connect(f"{paths.DB.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT dir FROM activities WHERE CAST(id AS TEXT) = ?", (activity_id,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    m = paths.ACTIVITY_KEY_RE.fullmatch(str(row[0])) if row and row[0] else None
    if m and m.group(1) == activity_id and a.exists(f"{row[0]}/activity.json"):
        return row[0]
    return None


# ------------------------------------------------------------------ plain files

def write_new(path: Path, chunks: bytes | Iterable[bytes]) -> int:
    """Write to path.tmp (never following a symlink there), then move it into place."""
    return safefs.write_new(path, chunks)


class FilesArchive(Archive):
    """Plain files under <root>/garmin. Every access goes name by name from the opened garmin
    folder and never follows a link (safefs.Tree), so no read or write can leave it, and only
    regular files with one name that are not the saved login are ever handed out."""

    kind = "files"

    def __init__(self, root: Path, readonly: bool = True) -> None:
        self.root = root
        self.readonly = readonly
        self.tree = safefs.Tree(root, TOP, root / ".state")

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def read(self, key: str) -> bytes | None:
        return self.tree.read(check_key(key))

    def write(self, key: str, data: bytes | Iterable[bytes]) -> int:
        self._require_writable()
        key = check_key(key)
        try:
            return self.tree.write(key, data)
        except safefs.LinkInArchive as e:
            raise Refused(f"fitdata did not write {key}: on the way there, {e.key} {e.why.removeprefix('it ')}, "
                          "and fitdata never writes through links or into anything but its own folders "
                          "inside the archive.",
                          hint=f"Replace {e.key} with an ordinary folder (or remove it), then run the same "
                          "command again.") from None

    def exists(self, key: str) -> bool:
        return self.tree.exists(check_key(key))

    def walk(self, prefix: str) -> list[Entry]:
        return _sort(Entry(k, size) for k, size in self.tree.walk(check_key(prefix), _valid_segment))

    def children(self, prefix: str) -> tuple[list[str], list[str]]:
        dirs, files = self.tree.children(check_key(prefix), _valid_segment)
        return sorted(dirs), sorted(files)

    def location(self, key: str) -> str:
        return str(self._path(key))

    def find_activity(self, activity_id: str) -> str | None:
        activity_id = check_activity_id(activity_id)
        known = _indexed_activity(self, activity_id)
        if known:
            return known
        suffix = f"-{activity_id}"
        for year in self.children(paths.DAYS_KEY)[0]:
            for day in self.children(f"{paths.DAYS_KEY}/{year}")[0]:
                folder = f"{paths.DAYS_KEY}/{year}/{day}/activities"
                for name in self.children(folder)[0]:
                    if name.endswith(suffix):
                        return f"{folder}/{name}"
        return None

    def close(self) -> None:
        pass


# ------------------------------------------------------------------ my-bucket-storage

def _mybucket():
    try:
        import mybucket
    except ImportError:
        raise ConfigProblem(
            'storage is set to "bucket", but the my-bucket-storage package (mybucket) is not installed '
            "for fitdata, so the archive cannot be opened.",
            hint="Install fitdata with the bucket extra: uv sync --locked --no-dev --extra bucket (in the "
            'fitdata folder, see the README), or set storage = "files".') from None
    return mybucket


@contextlib.contextmanager
def _bucket_errors(what: str) -> Iterator[None]:
    mb = _mybucket()
    try:
        yield
    except mb.Busy as e:
        raise Temporary(f"The archive's bucket is busy ({what}): another fitdata is writing to it.",
                        hint="Try again in a minute.") from e
    except mb.InvalidKey as e:
        raise BadInput(f"fitdata could not use that key in the bucket: {e.message}") from None
    except mb.NotFound as e:
        raise NotFound(f"{e.message} ({what})", hint=getattr(e, "hint", None)) from None
    except mb.MyBucketError as e:
        raise FitdataError(f"The archive's bucket could not {what}: {e.message}",
                           hint=getattr(e, "hint", None)) from None


class BucketArchive(Archive):
    kind = "bucket"

    def __init__(self, store_dir: Path, name: str, readonly: bool = True) -> None:
        mb = _mybucket()
        self.store_dir = store_dir
        self.name = name
        self.readonly = readonly
        if readonly and not (store_dir / "buckets" / name).is_dir():
            raise NotFound(f'The archive is set to live in the bucket "{name}", but that bucket does not '
                           "exist yet, so there is nothing to read.",
                           hint="Run fitdata sync to start it, or fitdata migrate-storage --to bucket to move "
                           "an existing archive of plain files into it.")
        with _bucket_errors("be opened"):
            self.store = mb.open_store(store_dir, readonly=readonly, create=not readonly)
            self.bucket = self.store.bucket(name, create=not readonly)

    def read(self, key: str) -> bytes | None:
        key = check_key(key)
        mb = _mybucket()
        with _bucket_errors(f"read {key}"):
            try:
                return self.bucket.get(key)
            except mb.ObjectNotFound:
                return None

    def write(self, key: str, data: bytes | Iterable[bytes]) -> int:
        self._require_writable()
        key = check_key(key)
        with _bucket_errors(f"write {key}"):
            return self.bucket.put(key, data, content_type=_content_type(key)).size

    def exists(self, key: str) -> bool:
        key = check_key(key)
        with _bucket_errors(f"check {key}"):
            return self.bucket.exists(key)

    def _pages(self, prefix: str, delimiter: str | None):
        start = None
        with _bucket_errors(f"list {prefix}"):
            while True:
                page = self.bucket.list(prefix=check_key(prefix) + "/", delimiter=delimiter, limit=1000,
                                        start_after=start)
                yield page
                if not page.truncated:
                    return
                start = page.next_start_after

    def walk(self, prefix: str) -> list[Entry]:
        return _sort(Entry(o.key, o.size) for page in self._pages(prefix, None) for o in page.objects)

    def children(self, prefix: str) -> tuple[list[str], list[str]]:
        n = len(prefix) + 1
        dirs, files = [], []
        for page in self._pages(prefix, "/"):
            dirs += [p[n:].rstrip("/") for p in page.prefixes]
            files += [o.key[n:] for o in page.objects]
        return sorted(dirs), sorted(files)

    def location(self, key: str) -> str:
        return f"bucket:{self.name}/{check_key(key)}"

    def find_activity(self, activity_id: str) -> str | None:
        activity_id = check_activity_id(activity_id)
        known = _indexed_activity(self, activity_id)
        if known:
            return known
        want = re.compile(rf"^(garmin/days/\d{{4}}/\d{{4}}-\d{{2}}-\d{{2}}/activities/[^/]+-{activity_id})/")
        for e in self.walk(paths.DAYS_KEY):
            m = want.match(e.key)
            if m:
                return m.group(1)
        return None

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.store.close()


# ------------------------------------------------------------------ choosing one

_open: dict[tuple, Archive] = {}


def open_archive(kind: str, write: bool = False) -> Archive:
    cfg = load()
    if kind == "files":
        return FilesArchive(paths.ROOT, readonly=not write)
    return BucketArchive(cfg.store_dir, cfg.bucket_name, readonly=not write)


def archive(write: bool = False) -> Archive:
    """The archive as the config says (storage = "files" or "bucket"), read-only unless write."""
    cfg = load()
    where = (cfg.storage, str(paths.ROOT), str(cfg.store_dir), cfg.bucket_name)
    if (*where, True) in _open:  # a writer of this process also serves its reads
        return _open[(*where, True)]
    if (*where, write) not in _open:
        _open[(*where, write)] = open_archive(cfg.storage, write)
    return _open[(*where, write)]


def find_activity(a: Archive, activity_id: int | str, day: str | None = None) -> str | None:
    """The key of an activity's folder, or None. With `day`, that day's folder is looked at first."""
    activity_id = check_activity_id(activity_id)
    if day:
        try:
            dkey = paths.day_key(day)
        except BadInput:
            dkey = None
        if dkey:
            for name in a.children(f"{dkey}/activities")[0]:
                if name.endswith(f"-{activity_id}"):
                    return f"{dkey}/activities/{name}"
    return a.find_activity(activity_id)


@atexit.register
def close_all() -> None:
    while _open:
        _open.popitem()[1].close()
