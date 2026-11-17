"""End to end with my-easy-software (mes): the fitness project as mes runs it.

A copy of this repository is installed with ``uv sync --offline``, a made-up archive (the same
generator as the dashboards' sample) is written into mes's data folder for the project, and mes
does the rest: ``mes apps sync`` (the lock), ``mes db rebuild`` (fitdata-garmin's ``index``, run as
mes runs a job), ``mes db check`` and ``mes apps check``. It proves that under mes the app writes
exactly the ``fitdata.sqlite`` mes and the dashboards read (apps contract 4.4 rule 1), and never
its own default folder.

Needs mes: ``MES_REWORK_DIR`` (a my-easy-software checkout), ``../my-easy-software`` next to this
repository (its ``.venv/bin/mes``), or ``mes`` on PATH; and ``uv`` with a warm cache. Otherwise it
is skipped, saying why. Nothing here talks to Garmin: only the index is rebuilt.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _mes() -> list[str] | None:
    candidates = [os.environ.get("MES_REWORK_DIR"), str(REPO.parent / "my-easy-software")]
    for c in candidates:
        if c and (Path(c) / ".venv" / "bin" / "mes").is_file():
            return [str(Path(c) / ".venv" / "bin" / "mes")]
    return [m] if (m := shutil.which("mes")) else None


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    mes = _mes()
    if mes is None:
        pytest.skip("my-easy-software (mes) is not available (set MES_REWORK_DIR to its checkout)")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    base = tmp_path_factory.mktemp("fitdata-e2e")
    root = base / "fitdata"
    # symlinks=True: a link in the repository stays a link in the copy, and the check below fails on
    # it (mes deploy refuses a project with links), so nothing is left out to hide one.
    shutil.copytree(REPO, root, symlinks=True, ignore=shutil.ignore_patterns(
        ".venv", ".git", "__pycache__", "apps.lock.json", ".pytest_cache", ".ruff_cache"))
    links = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_symlink()]
    assert links == [], f"links in the project (mes deploy refuses them): {links}"
    home = base / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FITDATA_", "MES_")) and k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "HOME")}
    env.update({"HOME": str(home), "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or _uv_cache(uv),
                "FITDATA_CONFIG": str(base / "no-config.toml"),
                "MES_DATA_HOME": str(base / "data"), "MES_STATE_HOME": str(base / "state"),
                "MES_CONFIG": str(base / "mes-config.toml")})
    done = subprocess.run([uv, "sync", "--offline", "--locked", "--quiet"], cwd=root, env=env, capture_output=True,
                          text=True, timeout=300)
    if done.returncode != 0:
        pytest.skip(f"uv sync --offline did not work here: {done.stderr[-300:]}")
    data = base / "data" / "fitdata"
    data.mkdir(parents=True)
    sys.path.insert(0, str(REPO / "ui" / "fixtures"))
    try:
        import make_fixture
    finally:
        sys.path.pop(0)
    make_fixture.write_archive(data)

    def run(*argv):
        exe = mes if argv[0] == "mes" else [str(root / ".venv" / "bin" / argv[0])]
        return subprocess.run([*exe, *argv[1:]], cwd=root, env=env, capture_output=True, text=True, timeout=180)

    return root, run, data, home


def _uv_cache(uv: str) -> str:
    return subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, timeout=30).stdout.strip()


def test_mes_runs_the_garmin_app_on_the_shared_database(project):
    root, run, data, home = project
    p = run("mes", "--json", "apps", "sync", "--project-dir", str(root))
    if p.returncode != 0 and "apps" not in p.stdout:
        pytest.skip(f"this mes does not know the app model yet: {(p.stderr or p.stdout)[:300]}")
    assert p.returncode == 0, p.stdout + p.stderr
    lock = json.loads((root / "apps.lock.json").read_text())
    assert [(a["name"], a["in_project"], a["program"]) for a in lock["apps"]] == [
        ("fitdata-garmin", True, "fitdata-garmin")]

    p = run("mes", "--json", "db", "rebuild", "--project-dir", str(root))
    assert p.returncode == 0, p.stdout + p.stderr
    db = data / "fitdata.sqlite"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*), min(date), max(date) FROM days").fetchone() == (119, "2026-01-05",
                                                                                            "2026-05-04")
        assert conn.execute("SELECT DISTINCT source FROM activities").fetchall() == [("fitdata-garmin",)]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert not (home / ".local" / "share" / "fitdata").exists()  # never the app's own default folder

    p = run("mes", "--json", "db", "check", "--project-dir", str(root))
    assert p.returncode == 0 and json.loads(p.stdout)["ok"] is True, p.stdout
    p = run("mes", "--json", "apps", "check", "--project-dir", str(root))
    assert p.returncode == 0 and json.loads(p.stdout)["ok"] is True, p.stdout
    p = run("mes", "db", "schema", "--sql", "--project-dir", str(root))
    assert p.stdout == (root / "apps" / "fitdata_garmin" / "schema.sql").read_text()

    # The project command finds the app through the lock mes wrote.
    p = run("fitdata", "--json", "apps")
    assert p.returncode == 0 and json.loads(p.stdout)["apps"][0]["location"] == "in-project", p.stderr
