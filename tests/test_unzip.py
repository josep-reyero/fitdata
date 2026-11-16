"""Issue 4: unpacking Garmin's downloads must never write outside the destination folder."""
from __future__ import annotations

import io
import zipfile

import pytest

from fitdata_garmin import sync


def _zip(*entries: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def _all_files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_normal_zip(tmp_path):
    dest = tmp_path / "raw"
    assert sync._unzip(_zip(("123_ACTIVITY.fit", b"fit")), dest) == ["123_ACTIVITY.fit"]
    assert (dest / "123_ACTIVITY.fit").read_bytes() == b"fit"


def test_bare_fit(tmp_path):
    assert sync._unzip(b".FIT data", tmp_path / "raw") == ["file.fit"]
    assert _all_files(tmp_path) == ["raw/file.fit"]


@pytest.mark.parametrize("name", ["../../evil.fit", "/tmp/evil.fit", "..\\..\\evil.fit", "a/../evil.fit"])
def test_folders_in_names_are_dropped(tmp_path, name):
    dest = tmp_path / "a" / "raw"
    assert sync._unzip(_zip((name, b"x")), dest) == ["evil.fit"]
    assert _all_files(tmp_path) == ["a/raw/evil.fit"]


@pytest.mark.parametrize("name", ["..", "a/..", ".", ".bashrc", "x.fit.tmp", "a b.fit", "x\n.fit", "-rf"])
def test_odd_names_skipped(tmp_path, name):
    dest = tmp_path / "raw"
    assert sync._unzip(_zip((name, b"x"), ("ok.fit", b"y")), dest) == ["ok.fit"]
    assert _all_files(tmp_path) == ["raw/ok.fit"]


def test_duplicate_names_keep_first(tmp_path):
    dest = tmp_path / "raw"
    assert sync._unzip(_zip(("a/x.fit", b"1"), ("b/x.fit", b"2")), dest) == ["x.fit"]
    assert (dest / "x.fit").read_bytes() == b"1"


def test_existing_symlink_is_replaced_not_followed(tmp_path):
    dest = tmp_path / "raw"
    dest.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("keep")
    (dest / "x.fit").symlink_to(target)
    sync._unzip(_zip(("x.fit", b"new")), dest)
    assert target.read_text() == "keep"
    assert not (dest / "x.fit").is_symlink() and (dest / "x.fit").read_bytes() == b"new"


def test_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "UNZIP_MAX_BYTES", 1000)
    with pytest.raises(ValueError, match="larger than any real FIT"):
        sync._unzip(_zip(("big.fit", b"0" * 5000)), tmp_path / "raw")
    assert _all_files(tmp_path) == []


def test_names_differing_only_in_case_keep_the_first(tmp_path):
    """On a case-insensitive disk (macOS default) ACT.FIT would overwrite act.fit."""
    dest = tmp_path / "raw"
    names = sync._unzip(_zip(("act.fit", b"FIRST"), ("ACT.FIT", b"SECOND")), dest)
    assert names == ["act.fit"]
    assert [p.read_bytes() for p in dest.iterdir()] == [b"FIRST"]
