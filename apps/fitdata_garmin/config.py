"""Machine-specific settings. Nothing personal lives in the repo; it all comes from here.

File: $FITDATA_CONFIG or ~/.config/fitdata/config.toml

    # Where this machine stores the archive (the machine that runs the sync).
    data_dir = "~/fitness/data"

    # Optional: how the day and activity files are stored (default "files").
    #   "files"   plain files and folders under data_dir/garmin/
    #   "bucket"  a my-bucket-storage bucket (compressed, stored once); needs fitdata[bucket].
    # Change it with `fitdata migrate-storage --to bucket|files`, which copies and checks first.
    storage = "files"

    # Optional: for how many of the archive's most recent days the index keeps the series through
    # the day and the night (heart rate, stress, Body Battery, ... and sleep stages; default 60,
    # 0 keeps none). Older days keep their daily numbers; the series stay in the files.
    intraday_days = 60

    [bucket]                           # only used with storage = "bucket"
    store = "~/fitness/data/store"     # default: <data_dir>/store
    name = "archive"                   # default shown

    # Optional: read the archive from another machine over SSH instead.
    # Query commands then run there and return their output here.
    [remote]
    host = "my-server"                 # anything `ssh` accepts (host alias, user@host)
    command = "~/.local/bin/fitdata"   # fitdata on that machine (default shown)

Environment overrides: FITDATA_DATA_DIR, FITDATA_STORAGE, FITDATA_REMOTE (host; "" disables),
FITDATA_INTRADAY_DAYS.

The archive's location is fitdata's own setting. my-easy-software (mes) and its dashboards find
the same folder through `[projects.fitdata] data_dir` in ~/.config/mes/config.toml (see README).
When mes itself runs the app (MES_PROJECT=fitdata with MES_DATA_DIR), MES_DATA_DIR is used instead
of data_dir, so the app never writes a different fitdata.sqlite than the one mes and the dashboards
read; FITDATA_DATA_DIR still wins over both.
"""
from __future__ import annotations

import os
import re
import secrets
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .errors import ConfigProblem

DEFAULT_DATA_DIR = "~/.local/share/fitdata"
DEFAULT_REMOTE_COMMAND = "~/.local/bin/fitdata"
DEFAULT_BUCKET = "archive"
STORAGE_KINDS = ("files", "bucket")
BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")  # my-bucket-storage's bucket names
DEFAULT_INTRADAY_DAYS = 60
MAX_INTRADAY_DAYS = 3660


@dataclass(frozen=True)
class Config:
    path: Path
    data_dir: Path
    remote_host: str | None
    remote_command: str
    storage: str = "files"
    bucket_store: Path | None = None
    bucket_name: str = DEFAULT_BUCKET
    intraday_days: int = DEFAULT_INTRADAY_DAYS

    @property
    def store_dir(self) -> Path:
        return self.bucket_store or self.data_dir / "store"


def _data_dir(raw: dict) -> Path:
    """Where the archive lives, first match wins: FITDATA_DATA_DIR; MES_DATA_DIR when mes runs this
    app for the fitdata project (its jobs, its gate and `mes db rebuild` set MES_PROJECT=fitdata
    with it), so the app writes the same fitdata.sqlite that mes and the dashboards read; data_dir
    in the config file; ~/.local/share/fitdata."""
    if os.environ.get("FITDATA_DATA_DIR"):
        return Path(os.environ["FITDATA_DATA_DIR"]).expanduser()
    mes_dir = os.environ.get("MES_DATA_DIR")
    if mes_dir and os.environ.get("MES_PROJECT") == "fitdata" and Path(mes_dir).is_absolute():
        return Path(mes_dir)
    return Path(raw.get("data_dir", DEFAULT_DATA_DIR)).expanduser()


def config_path() -> Path:
    return Path(os.environ.get("FITDATA_CONFIG", "~/.config/fitdata/config.toml")).expanduser()


@lru_cache(maxsize=1)
def load() -> Config:
    path = config_path()
    raw: dict = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigProblem(f"fitdata's config file {path} is not valid TOML ({e}), so fitdata "
                                "stopped without doing anything.",
                                hint="Fix the line it names (see the example in the README).") from None
    remote = raw.get("remote") or {}
    bucket = raw.get("bucket") or {}
    host = os.environ.get("FITDATA_REMOTE", remote.get("host")) or None
    data_dir = _data_dir(raw)
    storage = os.environ.get("FITDATA_STORAGE") or raw.get("storage") or "files"
    if storage not in STORAGE_KINDS:
        raise ConfigProblem(f"storage is set to {str(storage)[:40]!r} in {path} (or FITDATA_STORAGE), but "
                            "fitdata only knows \"files\" and \"bucket\", so it stopped without doing anything.",
                            hint='Set storage = "files" (the default) or storage = "bucket". To move an '
                            "archive from one to the other, use `fitdata migrate-storage --to bucket|files`.")
    name = bucket.get("name", DEFAULT_BUCKET)
    if not isinstance(name, str) or not BUCKET_NAME_RE.fullmatch(name):
        raise ConfigProblem(f"[bucket] name in {path} is {str(name)[:40]!r}, which is not a bucket name.",
                            hint='Use 2-63 lowercase letters, digits and "-", for example name = "archive".')
    store = Path(bucket["store"]).expanduser() if bucket.get("store") else None
    intraday = os.environ.get("FITDATA_INTRADAY_DAYS") or raw.get("intraday_days", DEFAULT_INTRADAY_DAYS)
    try:
        intraday = int(intraday) if not isinstance(intraday, bool) else None
    except (TypeError, ValueError):
        intraday = None
    if intraday is None or not 0 <= intraday <= MAX_INTRADAY_DAYS:
        raise ConfigProblem(f"intraday_days in {path} (or FITDATA_INTRADAY_DAYS) is not a whole number from 0 to "
                            f"{MAX_INTRADAY_DAYS}, so fitdata stopped without doing anything.",
                            hint=f"Set intraday_days = {DEFAULT_INTRADAY_DAYS} (the default), or leave it out.")
    cfg = Config(path=path, data_dir=data_dir, remote_host=host,
                 remote_command=remote.get("command", DEFAULT_REMOTE_COMMAND),
                 storage=storage, bucket_store=store, bucket_name=name, intraday_days=intraday)
    _check_store_location(cfg)
    return cfg


def data_dir_only() -> Path:
    """Just data_dir, without checking the rest of the config (for paths.py, imported early; the
    full check runs in load() and reports any problem in plain words)."""
    raw: dict = {}
    try:
        raw = tomllib.loads(config_path().read_text())
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return _data_dir(raw)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _check_store_location(cfg: Config) -> None:
    """The bucket store may not sit inside .state/ (the saved login) or inside the files tree."""
    for name, why in ((".state", "the folder that holds the saved Garmin login"),
                      ("garmin", "the folder of the archive's plain files")):
        if _inside(cfg.store_dir, cfg.data_dir / name):
            raise ConfigProblem(f"[bucket] store points inside {name}/ in the data folder, {why}, so fitdata "
                                "will not use it.",
                                hint="Leave [bucket] store out (it defaults to <data_dir>/store) or point it "
                                "at a folder of its own.")


# ------------------------------------------------------------------ changing the storage setting

_STORAGE_LINE = re.compile(r'^\s*storage\s*=.*$')
_TABLE_LINE = re.compile(r'^\s*\[')


def set_storage(value: str) -> Path:
    """Write `storage = "<value>"` into the config file, keeping every other line as it is.

    Only a top-level `storage = ...` line (before the first [table]) is replaced; if there is
    none, one is added before the first table. Before anything is written, the new text is read
    back as TOML: it must say storage = "<value>" and everything else exactly as before (a
    `storage =` line inside a multi-line string, for example, would not), or nothing is written.
    A config file that is a symbolic link is written where the link points, so the link stays.
    The new text goes to a new temporary file (random name, never through a link), keeps the
    file's permissions, and replaces the file in one step; then the file is read again to make
    sure. Returns the path written."""
    from .errors import Refused
    if value not in STORAGE_KINDS:
        raise ValueError(value)
    path = config_path()
    target = Path(os.path.realpath(path))
    old_text = target.read_text() if target.exists() else ""
    lines = old_text.splitlines(keepends=True)
    new = f'storage = "{value}"\n'
    first_table = next((i for i, line in enumerate(lines) if _TABLE_LINE.match(line)), len(lines))
    for i in range(first_table):
        if _STORAGE_LINE.match(lines[i]):
            lines[i] = new
            break
    else:
        if first_table and not lines[first_table - 1].endswith("\n"):
            lines[first_table - 1] += "\n"
        lines.insert(first_table, new)
    text = "".join(lines)

    def refuse() -> Refused:
        return Refused(f'fitdata could not set storage = "{value}" in its config file {path} by itself: '
                       "the file is laid out in a way it cannot safely change line by line (for example, a "
                       "storage line inside a multi-line text value), so it left the file as it was.",
                       hint=f'Open the file and set storage = "{value}" near the top, before any [table] line.')

    try:
        before, after = tomllib.loads(old_text), tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise refuse() from None
    before["storage"] = value
    if after != before:
        raise refuse()

    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o7777 if target.exists() else 0o600
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
            os.fchmod(out.fileno(), mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    load.cache_clear()
    try:
        written = tomllib.loads(target.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        written = {}
    if written.get("storage") != value:
        raise Refused(f'fitdata wrote storage = "{value}" into its config file {path}, but reading the file '
                      "back does not give that value, so the switch did not take effect.",
                      hint=f'Open the file and check that it says storage = "{value}" near the top.')
    return path
