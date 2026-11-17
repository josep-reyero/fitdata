"""Read commands on a data folder this process may only read.

The read level of the mes gate binds the data folder read-only, so nothing a read command does may
need to write there: no lock file, no log, no state, no SQLite journal next to the index. Here the
folder is made read-only with permissions (the same refusal the sandbox gives, EACCES instead of
EROFS), and every read command must still answer and leave the folder exactly as it was."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import stat

import pytest

from conftest import ACTIVITY_ID, DAY
from fitdata_garmin import paths, safefs
from fitdata_garmin.errors import Temporary

if os.name != "posix" or os.geteuid() == 0:
    pytest.skip("needs a POSIX account that permissions apply to (not root)", allow_module_level=True)


def _snapshot(root):
    """Every entry under root with its kind, size and modification time."""
    out = {}
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            st = os.lstat(os.path.join(folder, name))
            out[os.path.relpath(os.path.join(folder, name), root)] = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns)
    return out


def _set_modes(root, folder_mode, file_mode):
    for folder, dirs, files in os.walk(root, topdown=False):
        for name in files:
            os.chmod(os.path.join(folder, name), file_mode)
        for name in dirs:
            os.chmod(os.path.join(folder, name), folder_mode)
    os.chmod(root, folder_mode)


@pytest.fixture
def read_only(archive):
    """The archive fixture's data folder (index built), made read-only; yields a checker that the
    folder is unchanged."""
    _set_modes(archive, 0o700, 0o600)
    before = _snapshot(archive)
    _set_modes(archive, 0o500, 0o400)

    def unchanged():
        os.chmod(archive, 0o700)  # os.walk needs to list it; nothing is written meanwhile
        try:
            _set_modes(archive, 0o700, 0o600)
            assert _snapshot(archive) == before
        finally:
            _set_modes(archive, 0o500, 0o400)
    try:
        yield archive, unchanged
    finally:
        _set_modes(archive, 0o700, 0o600)


def test_index_creates_the_sql_lock(archive):
    assert paths.SQL_LOCK.is_file()
    assert stat.S_IMODE(paths.SQL_LOCK.stat().st_mode) == 0o600


@pytest.mark.parametrize("argv", [
    ["summary"], ["days", "--days", "3650"], ["activities"], ["day", DAY], ["day", DAY, "--source", "sleep"],
    ["activity", ACTIVITY_ID], ["activity", ACTIVITY_ID, "--source", "splits"],
    ["activity", ACTIVITY_ID, "--records"], ["sources"], ["schema"], ["sql", "SELECT date, steps FROM days"],
    ["status"], ["describe"], ["guide"],
])
def test_every_read_command_works_on_a_read_only_data_folder(read_only, run, argv):
    _, unchanged = read_only
    code, out, err = run("--json", *argv)
    assert code == 0, err
    json.loads(out)
    assert "Traceback" not in err and "Note:" not in err
    unchanged()


def test_sql_still_takes_its_lock_on_a_read_only_data_folder(read_only, run):
    _, unchanged = read_only
    with open(paths.SQL_LOCK, "rb") as fh:  # another `fitdata sql` holds it
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out, err = run("--json", "sql", "SELECT 1 AS x")
        assert code == 75 and out == "" and "Another fitdata sql" in json.loads(err)["error"]
    code, out, _ = run("--json", "sql", "SELECT 1 AS x")
    assert code == 0 and json.loads(out) == [{"x": 1}]
    unchanged()


def test_sql_runs_without_a_lock_it_cannot_create(archive, run):
    paths.SQL_LOCK.unlink()  # an index made before index created the lock
    _set_modes(archive, 0o700, 0o600)
    before = _snapshot(archive)
    _set_modes(archive, 0o500, 0o400)
    try:
        code, out, err = run("--json", "sql", "SELECT count(*) AS n FROM days")
        assert code == 0 and json.loads(out) == [{"n": 1}]
        assert "without the lock" in err and "read-only" in err
    finally:
        _set_modes(archive, 0o700, 0o600)
    assert _snapshot(archive) == before


def test_sql_runs_without_a_lock_when_the_state_folder_is_missing(archive, run):
    for p in paths.STATE.iterdir():
        p.unlink()
    paths.STATE.rmdir()
    os.chmod(archive, 0o500)
    try:
        code, out, err = run("--json", "sql", "SELECT 1 AS x")
        assert code == 0 and json.loads(out) == [{"x": 1}] and "without the lock" in err
    finally:
        os.chmod(archive, 0o700)
    assert not paths.STATE.exists()


def test_read_only_file_system_opens_the_existing_lock_read_only(tmp_path, monkeypatch):
    """EROFS, as a read-only bind mount answers: the existing lock is opened read-only and held."""
    lock = tmp_path / ".state" / "sql.lock"
    lock.parent.mkdir()
    lock.write_bytes(b"")
    real_open = os.open
    opened = []

    def ro_fs(path, flags, *a, **kw):
        if flags & (os.O_RDWR | os.O_WRONLY | os.O_CREAT):
            raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(path))
        opened.append(flags)
        return real_open(path, flags, *a, **kw)
    monkeypatch.setattr(safefs.os, "open", ro_fs)
    with safefs.hold_lock(lock, read_only_ok=True):
        assert opened and not opened[-1] & (os.O_RDWR | os.O_WRONLY)
        with pytest.raises(Temporary):
            with safefs.hold_lock(lock, read_only_ok=True):
                pass


def test_the_sync_lock_still_needs_a_writable_folder(tmp_path, monkeypatch):
    lock = tmp_path / ".state" / "sync.lock"
    lock.parent.mkdir()
    real_open = os.open

    def ro_fs(path, flags, *a, **kw):
        if flags & (os.O_RDWR | os.O_WRONLY | os.O_CREAT):
            raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(path))
        return real_open(path, flags, *a, **kw)
    monkeypatch.setattr(safefs.os, "open", ro_fs)
    from fitdata_garmin.errors import FitdataError
    with pytest.raises(FitdataError, match="could not open its lock file"):
        with safefs.hold_lock(lock):
            pass
