"""A tiny fake archive in a temp folder, with a fake login token next to it. Nothing here talks to
Garmin or ssh."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

TOKEN_TEXT = "FAKE-TOKEN-DO-NOT-LEAK"
DAY = "2026-11-14"
ACTIVITY_ID = "111"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "data"
    ddir = root / "garmin" / "days" / DAY[:4] / DAY
    adir = ddir / "activities" / f"0700-running-{ACTIVITY_ID}"
    _write(ddir / "day.json", {"date": DAY, "sources": {"sleep": {"status": "ok"}}, "headline": {"steps": 1234},
                               "activities": [adir.name]})
    _write(ddir / "processed" / "sleep.json", {"sleep": "ok"})
    _write(adir / "activity.json", {"id": int(ACTIVITY_ID), "date": DAY, "type": "running"})
    _write(adir / "processed" / "splits.json", {"splits": [1, 2]})
    _write(adir / "derived" / "records.csv", "timestamp,heart_rate\n1,100\n2,101\n")
    _write(root / ".state" / "garmin-tokens.json", {"token": TOKEN_TEXT})
    # Something outside the archive a traversal could aim at.
    _write(tmp_path / "outside" / "day.json", {"date": "x"})
    _write(tmp_path / "outside" / "processed" / "leak.json", {"leak": TOKEN_TEXT})

    monkeypatch.setenv("FITDATA_DATA_DIR", str(root))
    monkeypatch.setenv("FITDATA_REMOTE", "")
    monkeypatch.setenv("FITDATA_CONFIG", str(tmp_path / "no-config.toml"))
    from fitdata import config, paths
    config.load.cache_clear()
    importlib.reload(paths)
    from fitdata import index
    index.rebuild()
    yield root
    config.load.cache_clear()


@pytest.fixture
def run(archive, capsys):
    """Run the CLI in-process: returns (exit code, stdout, stderr)."""
    from fitdata import cli

    def _run(*argv: str):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err
    return _run
