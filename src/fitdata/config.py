"""Machine-specific settings. Nothing personal lives in the repo; it all comes from here.

File: $FITDATA_CONFIG or ~/.config/fitdata/config.toml

    # Where this machine stores the archive (the machine that runs the sync).
    data_dir = "~/fitness/data"

    # Optional: how the day and activity files are stored (default "files").
    #   "files"   plain files and folders under data_dir/garmin/
    #   "bucket"  a my-bucket-storage bucket (compressed, stored once); needs fitdata[bucket].
    # Change it with `fitdata migrate-storage --to bucket|files`, which copies and checks first.
    storage = "files"

    [bucket]                           # only used with storage = "bucket"
    store = "~/fitness/data/store"     # default: <data_dir>/store
    name = "archive"                   # default shown

    # Optional: read the archive from another machine over SSH instead.
    # Query commands then run there and return their output here.
    [remote]
    host = "my-server"                 # anything `ssh` accepts (host alias, user@host)
    command = "~/.local/bin/fitdata"   # fitdata on that machine (default shown)

Environment overrides: FITDATA_DATA_DIR, FITDATA_STORAGE, FITDATA_REMOTE (host; "" disables).

The archive's location is fitdata's own setting. my-easy-software (mes) and its dashboards find
the same folder through `[projects.fitdata] data_dir` in ~/.config/mes/config.toml (see README).
"""
from __future__ import annotations

import os
import re
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


@dataclass(frozen=True)
class Config:
    path: Path
    data_dir: Path
    remote_host: str | None
    remote_command: str
    storage: str = "files"
    bucket_store: Path | None = None
    bucket_name: str = DEFAULT_BUCKET

    @property
    def store_dir(self) -> Path:
        return self.bucket_store or self.data_dir / "store"


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
    data_dir = Path(os.environ.get("FITDATA_DATA_DIR", raw.get("data_dir", DEFAULT_DATA_DIR))).expanduser()
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
    cfg = Config(path=path, data_dir=data_dir, remote_host=host,
                 remote_command=remote.get("command", DEFAULT_REMOTE_COMMAND),
                 storage=storage, bucket_store=store, bucket_name=name)
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
    return Path(os.environ.get("FITDATA_DATA_DIR", raw.get("data_dir", DEFAULT_DATA_DIR))).expanduser()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _check_store_location(cfg: Config) -> None:
    """The bucket store may not sit inside .state/ (the saved login) or inside the files tree."""
    for folder, why in ((cfg.data_dir / ".state", "the folder that holds the saved Garmin login"),
                        (cfg.data_dir / "garmin", "the folder of the archive's plain files")):
        if _inside(cfg.store_dir, folder):
            raise ConfigProblem(f"[bucket] store points inside {folder}, {why}, so fitdata will not use it.",
                                hint="Leave [bucket] store out (it defaults to <data_dir>/store) or point it "
                                "at a folder of its own.")


# ------------------------------------------------------------------ changing the storage setting

_STORAGE_LINE = re.compile(r'^\s*storage\s*=.*$')
_TABLE_LINE = re.compile(r'^\s*\[')


def set_storage(value: str) -> Path:
    """Write `storage = "<value>"` into the config file, keeping every other line as it is.

    Only a top-level `storage = ...` line (before the first [table]) is replaced; if there is
    none, one is added before the first table. The file is replaced atomically and keeps its
    permissions. Returns the path written."""
    if value not in STORAGE_KINDS:
        raise ValueError(value)
    path = config_path()
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(lines))
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    tmp.replace(path)
    load.cache_clear()
    return path
