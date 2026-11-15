"""Machine-specific settings. Nothing personal lives in the repo; it all comes from here.

File: $FITDATA_CONFIG or ~/.config/fitdata/config.toml

    # Where this machine stores the archive (the machine that runs the sync).
    data_dir = "~/fitness/data"

    # Optional: read the archive from another machine over SSH instead.
    # Query commands then run there and return their output here.
    [remote]
    host = "my-server"                 # anything `ssh` accepts (host alias, user@host)
    command = "~/.local/bin/fitdata"   # fitdata on that machine (default shown)

Environment overrides: FITDATA_DATA_DIR, FITDATA_REMOTE (host; "" disables).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_DATA_DIR = "~/.local/share/fitdata"
DEFAULT_REMOTE_COMMAND = "~/.local/bin/fitdata"


@dataclass(frozen=True)
class Config:
    path: Path
    data_dir: Path
    remote_host: str | None
    remote_command: str


def config_path() -> Path:
    return Path(os.environ.get("FITDATA_CONFIG", "~/.config/fitdata/config.toml")).expanduser()


@lru_cache(maxsize=1)
def load() -> Config:
    path = config_path()
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())
    remote = raw.get("remote") or {}
    host = os.environ.get("FITDATA_REMOTE", remote.get("host")) or None
    data_dir = os.environ.get("FITDATA_DATA_DIR", raw.get("data_dir", DEFAULT_DATA_DIR))
    return Config(
        path=path,
        data_dir=Path(data_dir).expanduser(),
        remote_host=host,
        remote_command=remote.get("command", DEFAULT_REMOTE_COMMAND),
    )
