"""Reaching files under <data_dir>/garmin without ever following a link.

The archive can hold things fitdata did not put there (a restore, a copy from another machine,
someone's mistake), so every access starts from the opened `garmin` folder and goes one name at a
time, each opened relative to the folder before it with O_NOFOLLOW. A symbolic link anywhere on
the way is never followed, so nothing outside <data_dir>/garmin can be read or written through
the archive, whatever the links say and however the disk compares upper and lower case.

A file is only handed out when it is a regular file with exactly one name (st_nlink == 1) and is
not the same file as anything in .state/ (the saved Garmin login). Anything else (a symbolic link,
a hard link, a pipe or device) is reported once in plain words and skipped.

Allow-list, not deny-list: only what is reached this way exists for fitdata.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger("fitdata")

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
# O_NONBLOCK: opening a named pipe must not hang; it is refused right after by fstat.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
# What open() says, depending on the system, when O_NOFOLLOW meets a symbolic link.
_LINK_ERRNOS = {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP), getattr(errno, "EFTYPE", errno.ELOOP)}
_MISSING_ERRNOS = {errno.ENOENT, errno.ENOTDIR}


class LinkInArchive(Exception):
    """A write would have had to go through a link or something that is not a folder."""

    def __init__(self, key: str, why: str) -> None:
        super().__init__(f"{key}: {why}")
        self.key = key
        self.why = why


_reported: set[str] = set()


def report(key: str, why: str) -> None:
    """Say once per run, in plain words, that something in the archive was skipped."""
    if key in _reported:
        return
    _reported.add(key)
    log.warning("Skipped %s in the archive: %s. fitdata never follows links or reads files with more "
                "than one name inside the archive, so it left this out. If it should be there, replace "
                "it with an ordinary file or folder.", key, why)


def _what(st: os.stat_result) -> str:
    if stat.S_ISLNK(st.st_mode):
        return "it is a symbolic link"
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        return f"it is a hard link (the same file has {st.st_nlink} names)"
    if stat.S_ISDIR(st.st_mode):
        return "it is a folder"
    return "it is not an ordinary file (a pipe, socket or device)"


def secret_inodes(state_dir: Path) -> frozenset[tuple[int, int]]:
    """(device, inode) of .state/ itself and of everything in it, links followed: whatever name
    or letter case a path uses, the saved login is recognised by what it is, not by its name."""
    found: set[tuple[int, int]] = set()

    def add(path: Path | str, follow: bool) -> os.stat_result | None:
        try:
            st = os.stat(path, follow_symlinks=follow)
        except OSError:
            return None
        found.add((st.st_dev, st.st_ino))
        return st

    def scan(folder: Path, depth: int) -> None:
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            p = folder / name
            add(p, False)
            st = add(p, True)
            if st is not None and stat.S_ISDIR(st.st_mode) and depth < 3:
                scan(p, depth + 1)

    add(state_dir, False)
    if add(state_dir, True) is not None:
        scan(state_dir, 0)
    return frozenset(found)


class Tree:
    """The archive folder <data_dir>/<top>, reached only by names, never through links."""

    def __init__(self, data_dir: Path, top: str, state_dir: Path) -> None:
        self.data_dir = data_dir
        self.top = top
        self.state_dir = state_dir
        self._secrets: frozenset[tuple[int, int]] | None = None

    # ------------------------------------------------------------ helpers

    @property
    def secrets(self) -> frozenset[tuple[int, int]]:
        if self._secrets is None:
            self._secrets = secret_inodes(self.state_dir)
        return self._secrets

    def _usable(self, st: os.stat_result) -> bool:
        return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
                and (st.st_dev, st.st_ino) not in self.secrets)

    def _why_not(self, st: os.stat_result) -> str:
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
            return "it is the same file as one in the folder of the saved Garmin login"
        return _what(st)

    def _open_top(self, create: bool = False) -> int | None:
        """The data folder is the owner's own setting and is opened as configured; the archive
        folder inside it is never a link."""
        if create:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            base = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
        except OSError as e:
            if e.errno in _MISSING_ERRNOS and not create:
                return None
            raise
        try:
            return self._step(base, self.top, self.top, create)
        finally:
            os.close(base)

    def _step(self, parent: int, name: str, key: str, create: bool) -> int | None:
        """Open folder `name` inside `parent`. None when missing (or not usable, for reads)."""
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent)
        except OSError as e:
            err = e.errno
        if err == errno.ENOENT and create:
            try:
                os.mkdir(name, 0o777, dir_fd=parent)
            except FileExistsError:
                pass
            try:
                return os.open(name, _DIR_FLAGS, dir_fd=parent)
            except OSError as e:
                err = e.errno
        if err == errno.ENOENT:
            return None
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            st = None
        why = _what(st) if st is not None else "fitdata could not open it as a folder"
        if create:
            raise LinkInArchive(key, why)
        if st is not None and stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
            return None  # an ordinary file where a folder was expected: nothing there, as before
        if err in _LINK_ERRNOS or err == errno.ENOTDIR:
            report(key, why)
            return None
        raise OSError(err, os.strerror(err), key)

    def _open_dir(self, parts: list[str], create: bool = False) -> int | None:
        fd = self._open_top(create)
        if fd is None:
            return None
        done = [self.top]
        try:
            for name in parts:
                done.append(name)
                nxt = self._step(fd, name, "/".join(done), create)
                os.close(fd)
                fd = nxt
                if fd is None:
                    return None
            out, fd = fd, None
            return out
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _split(key: str) -> list[str]:
        parts = key.split("/")
        assert parts[0] and all(parts), key  # keys are checked by storage.check_key first
        return parts[1:]

    # ------------------------------------------------------------ reading

    def open_file(self, key: str) -> int | None:
        """A file descriptor for the regular, single-named, non-secret file at key, or None."""
        parts = self._split(key)
        if not parts:
            return None
        folder = self._open_dir(parts[:-1])
        if folder is None:
            return None
        try:
            try:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=folder)
            except OSError as e:
                if e.errno in _LINK_ERRNOS:
                    report(key, "it is a symbolic link")
                    return None
                if e.errno == errno.ENXIO:
                    report(key, "it is not an ordinary file (a pipe, socket or device)")
                    return None
                if e.errno in _MISSING_ERRNOS or e.errno == errno.EISDIR:
                    return None
                raise
        finally:
            os.close(folder)
        st = os.fstat(fd)
        if not self._usable(st):
            os.close(fd)
            if not stat.S_ISDIR(st.st_mode):
                report(key, self._why_not(st))
            return None
        return fd

    def read(self, key: str, max_bytes: int | None = None) -> bytes | None:
        fd = self.open_file(key)
        if fd is None:
            return None
        with os.fdopen(fd, "rb") as fh:
            if max_bytes is not None and os.fstat(fh.fileno()).st_size > max_bytes:
                raise ValueError(f"{key} is larger than {max_bytes // 2**20} MB")
            return fh.read()

    def exists(self, key: str) -> bool:
        fd = self.open_file(key)
        if fd is None:
            return False
        os.close(fd)
        return True

    def _entries(self, fd: int, key: str) -> Iterator[tuple[str, str, os.stat_result]]:
        """(name, "dir" | "file", lstat) for each usable entry of an open folder; others reported."""
        for name in sorted(os.listdir(fd)):
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                yield name, "dir", st
            elif self._usable(st):
                yield name, "file", st
            else:
                report(f"{key}/{name}", self._why_not(st))

    def children(self, key: str, valid=lambda name: True) -> tuple[list[str], list[str]]:
        fd = self._open_dir(self._split(key))
        if fd is None:
            return [], []
        dirs, files = [], []
        try:
            for name, kind, _ in self._entries(fd, key):
                if valid(name):
                    (dirs if kind == "dir" else files).append(name)
        finally:
            os.close(fd)
        return dirs, files

    def walk(self, key: str, valid=lambda name: True) -> list[tuple[str, int]]:
        """(key, size) of every usable file below key; folders and files with names that
        `valid` refuses are left out, as before."""
        fd = self._open_dir(self._split(key))
        if fd is None:
            return []
        out: list[tuple[str, int]] = []

        def visit(fd: int, key: str) -> None:
            for name, kind, st in self._entries(fd, key):
                if not valid(name):
                    continue
                if kind == "file":
                    out.append((f"{key}/{name}", st.st_size))
                    continue
                try:
                    sub = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError:
                    continue  # replaced by a link or removed meanwhile: never followed
                try:
                    visit(sub, f"{key}/{name}")
                finally:
                    os.close(sub)
        try:
            visit(fd, key)
        finally:
            os.close(fd)
        return out

    # ------------------------------------------------------------ writing

    def write(self, key: str, chunks: bytes | Iterable[bytes], mode: int = 0o644) -> int:
        """Write key's content to a new temporary file next to it (created with O_EXCL and
        O_NOFOLLOW), then rename it into place. Folders on the way are created; a link or a file
        where a folder should be stops the write (LinkInArchive)."""
        parts = self._split(key)
        folder = self._open_dir(parts[:-1], create=True)
        assert folder is not None
        try:
            return write_at(folder, parts[-1], chunks, mode)
        finally:
            os.close(folder)


def write_at(folder: int, name: str, chunks: bytes | Iterable[bytes], mode: int = 0o644) -> int:
    """Write `name` inside the open folder: name.tmp first (never through a link), then rename.
    The rename replaces whatever entry had the name (even a link) without touching what it
    pointed to."""
    if isinstance(chunks, (bytes, bytearray)):
        chunks = [bytes(chunks)]
    tmp = name + ".tmp"
    try:
        os.unlink(tmp, dir_fd=folder)  # a leftover of an interrupted run; unlink never follows
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, mode, dir_fd=folder)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                out.write(chunk)
                size += len(chunk)
        os.replace(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=folder)
        except OSError:
            pass
        raise
    return size


def write_new(path: Path, chunks: bytes | Iterable[bytes], mode: int = 0o644) -> int:
    """write_at for a plain path: its folder is opened as given (the caller's own folder)."""
    folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    try:
        return write_at(folder, path.name, chunks, mode)
    finally:
        os.close(folder)


# ------------------------------------------------------------------ files outside the archive

def inside_state(path: Path, state_dir: Path, opened: os.stat_result) -> bool:
    """True when an opened file is the saved login or lies inside .state/, compared by device
    and inode (so neither links nor letter case matter)."""
    secrets = secret_inodes(state_dir)
    if (opened.st_dev, opened.st_ino) in secrets:
        return True
    try:
        state = os.stat(state_dir)
    except OSError:
        return False
    target = (state.st_dev, state.st_ino)
    for folder in Path(os.path.realpath(path)).parents:
        try:
            st = os.stat(folder)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == target:
            return True
    return False


# ------------------------------------------------------------------ locks

@contextmanager
def hold_lock(path, busy: str = "Another fitdata sync is running on this archive, so this one did not start.",
              hint: str = "Nothing to do: the next run picks up where that one ends."):
    """An exclusive, non-blocking lock on a file in .state/ (owner-only). The lock file is opened
    without following a link and is never truncated. Busy: Temporary (exit 75)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # .state holds the login token
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC, 0o600)
    except OSError as e:
        from .errors import FitdataError, Refused
        if e.errno in _LINK_ERRNOS:
            raise Refused(f"The lock file {path.name} in the folder of the saved Garmin login (.state) is a "
                          "symbolic link, so fitdata did not use it and did not start.",
                          hint=f"Remove .state/{path.name} in the data folder; fitdata makes a new one.") from None
        raise FitdataError(f"fitdata could not open its lock file .state/{path.name} ({e.strerror}), so it "
                           "did not start.", hint="Check that the data folder belongs to you.") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            from .errors import Temporary
            raise Temporary(busy, hint=hint) from None
        yield
    finally:
        os.close(fd)
