"""Where the archive lives: fitdata's own `data_dir`, always. mes and the dashboards find the same
folder through `[projects.fitdata] data_dir` in ~/.config/mes/config.toml. No symlinks."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from conftest import easyui_for_project, use
from fitdata_garmin import index, paths

REPO = Path(__file__).resolve().parents[1]
# The project's shared database: <data folder>/<project name>.sqlite (apps contract, section 4.1).
UI_DATA = tomllib.loads((REPO / "project.toml").read_text())["project"]["name"] + ".sqlite"


@pytest.fixture
def two_configs(tmp_path, monkeypatch):
    """fitdata's config says data_dir = <archive>; mes's config points [projects.fitdata] at it."""
    archive = tmp_path / "my-archive"
    use(tmp_path, monkeypatch, config_text=f'data_dir = "{archive}"\n')
    monkeypatch.delenv("FITDATA_DATA_DIR")
    mes_home = tmp_path / "mes-data"
    mes_config = tmp_path / "mes-config.toml"
    mes_config.write_text(f'[projects.fitdata]\ndata_dir = "{archive}"\n')
    monkeypatch.setenv("MES_CONFIG", str(mes_config))
    monkeypatch.setenv("MES_DATA_HOME", str(mes_home))
    from fitdata_garmin import config
    import importlib
    config.load.cache_clear()
    importlib.reload(paths)
    return archive, mes_home, mes_config


def test_fitdata_uses_its_own_data_dir(two_configs, cli_run):
    archive, mes_home, _ = two_configs
    assert paths.ROOT == archive
    index.rebuild()
    # The index the dashboards read is exactly <data_dir>/fitdata.sqlite.
    assert (archive / UI_DATA).is_file() and paths.DB == archive / UI_DATA
    assert not mes_home.exists()  # fitdata never writes into mes's own folders
    links = [p for p in archive.parent.rglob("*") if p.is_symlink()]
    assert links == []
    code, out, _ = cli_run("--json", "status")
    st = json.loads(out)
    assert st["root"] == str(archive)
    assert tomllib.loads(st["mes_config"]) == {"projects": {"fitdata": {"data_dir": str(archive)}}}


def test_mes_config_does_not_move_fitdata(two_configs, tmp_path):
    """A different [projects.fitdata] data_dir in mes's config changes where mes looks, never
    where fitdata writes: the two must be set to the same folder by the person."""
    archive, _, mes_config = two_configs
    mes_config.write_text(f'[projects.fitdata]\ndata_dir = "{tmp_path / "elsewhere"}"\n')
    index.rebuild()
    assert (archive / UI_DATA).is_file() and not (tmp_path / "elsewhere").exists()


def test_easy_ui_finds_the_index_through_the_override(two_configs):
    appconfig = easyui_for_project(REPO).appconfig
    archive, _, _ = two_configs
    index.rebuild()
    cfg = appconfig.load(REPO)
    assert cfg.source_db() == archive / UI_DATA and cfg.source_db().is_file()
