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
    monkeypatch.delenv("FITDATA_STORAGE", raising=False)
    monkeypatch.delenv("MES_ALLOW", raising=False)
    monkeypatch.delenv("MES_VIA_GATE", raising=False)
    from fitdata_garmin import config, paths
    config.load.cache_clear()
    importlib.reload(paths)
    from fitdata_garmin import index, storage
    storage.close_all()
    index.rebuild()
    yield root
    storage.close_all()
    config.load.cache_clear()


def use(tmp_path: Path, monkeypatch, storage_kind: str | None = None, config_text: str = "") -> Path:
    """Point fitdata at an empty archive in tmp_path (config file tmp_path/config.toml)."""
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text(config_text)
    monkeypatch.setenv("FITDATA_DATA_DIR", str(root))
    monkeypatch.setenv("FITDATA_REMOTE", "")
    monkeypatch.setenv("FITDATA_CONFIG", str(cfg))
    if storage_kind:
        monkeypatch.setenv("FITDATA_STORAGE", storage_kind)
    else:
        monkeypatch.delenv("FITDATA_STORAGE", raising=False)
    monkeypatch.delenv("MES_ALLOW", raising=False)
    monkeypatch.delenv("MES_VIA_GATE", raising=False)
    from fitdata_garmin import config, paths, storage
    storage.close_all()
    config.load.cache_clear()
    importlib.reload(paths)
    return root


@pytest.fixture(params=["files", "bucket"])
def backend(request, tmp_path, monkeypatch):
    """An empty archive in each storage backend; the bucket one is skipped without mybucket."""
    if request.param == "bucket":
        pytest.importorskip("mybucket", reason="my-bucket-storage is not installed (fitdata[bucket])")
    use(tmp_path, monkeypatch, request.param)
    yield request.param
    from fitdata_garmin import config, storage
    storage.close_all()
    config.load.cache_clear()


@pytest.fixture
def cli_run(capsys):
    """Run the CLI in-process without the `archive` fixture: returns (exit code, stdout, stderr)."""
    from fitdata_garmin import cli

    def _run(*argv: str):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err
    return _run


@pytest.fixture
def run(archive, capsys):
    """Run the CLI in-process: returns (exit code, stdout, stderr)."""
    from fitdata_garmin import cli

    def _run(*argv: str):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err
    return _run


@pytest.fixture(autouse=True)
def _fresh_logger():
    """Each CLI run in a test adds log handlers; drop them so tests do not see each other's logs."""
    import logging
    from fitdata_garmin import safefs
    safefs._reported.clear()  # each test sees its own warnings about skipped links
    yield
    log = logging.getLogger("fitdata")
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()


def easyui_for_project(repo: Path):
    """my-easy-ui, when it is installed and reads this project's project.toml ([project] apps,
    no [ui] data). Skips the test otherwise, saying why."""
    pytest.importorskip("easyui", reason="my-easy-ui is not installed, so the dashboards were not checked with "
                        "it. Run: uv run --with-editable <path to my-easy-ui> python -m pytest")
    from easyui import appconfig, errors
    try:
        appconfig.load(repo)
    except errors.EasyUIError as e:  # an easy-ui that does not read project apps
        pytest.skip(f"this my-easy-ui does not read the apps of project.toml ({e})")
    import easyui
    return easyui
