"""Strict checks for every value a caller passes in that ends up in a file path or a shell command.

Each check either returns the clean value or raises BadInput with a message a non-developer can
act on. Paths are only ever built from values that passed one of these checks, so a value like
"../../x" can never point a read outside the archive.
"""
from __future__ import annotations

import re
from datetime import date

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACTIVITY_ID_RE = re.compile(r"^[0-9]{1,20}$")
SOURCE_RE = re.compile(r"^[a-z0-9_]+$")
# An ssh destination: a host alias, name, address or user@host. Never starts with "-", so ssh
# can't mistake it for an option.
REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:%-]*$")
# The fitdata command on the other machine: plain words made of path characters, so the remote
# shell can expand "~" but finds nothing else to interpret.
REMOTE_COMMAND_RE = re.compile(r"^[A-Za-z0-9_./~+:=-]+( [A-Za-z0-9_./~+:=-]+)*$")


class BadInput(ValueError):
    """A value given to fitdata was refused. The message says why and what to do instead."""


def check_date(value: str, what: str = "date") -> str:
    """A real calendar date written YYYY-MM-DD."""
    text = str(value)
    if DATE_RE.fullmatch(text):
        try:
            date.fromisoformat(text)
            return text
        except ValueError:
            pass
    raise BadInput(f"{_show(text)} is not a valid {what}. Dates are written YYYY-MM-DD, for example "
                   f"2026-11-14, and must be a real day on the calendar. Fix the {what} and try again.")


def check_activity_id(value: str | int) -> str:
    """An activity id: digits only, as Garmin assigns them."""
    text = str(value)
    if ACTIVITY_ID_RE.fullmatch(text):
        return text
    raise BadInput(f"{_show(text)} is not an activity id. Activity ids are numbers only, for example "
                   f"123456789. Run `fitdata activities` to see the ids you have.")


def check_source(value: str, known: list[str], kind: str) -> str:
    """A source name that fitdata knows for this kind of record (day or activity)."""
    if SOURCE_RE.fullmatch(value or "") and value in known:
        return value
    raise BadInput(f"{_show(value)} is not one of the {kind} sources fitdata knows, so nothing was read. "
                   f"The {kind} sources are: {', '.join(sorted(known))}. "
                   f"Run `fitdata sources` to see what each one holds.")


def check_remote_host(value: str) -> str:
    if REMOTE_HOST_RE.fullmatch(value or ""):
        return value
    raise BadInput(f"The remote host in your fitdata config ({_show(value)}) is not a plain ssh host "
                   f"name, so fitdata will not pass it to ssh: a value starting with '-' or containing "
                   f"spaces or symbols could make ssh run something else. Set [remote] host (or "
                   f"FITDATA_REMOTE) to a host alias such as my-server or user@my-server.")


def check_remote_command(value: str) -> str:
    if REMOTE_COMMAND_RE.fullmatch(value or "") and not value.startswith("-"):
        return value
    raise BadInput(f"The remote command in your fitdata config ({_show(value)}) contains characters "
                   f"the other machine's shell would interpret, so fitdata will not send it. Set "
                   f"[remote] command to the path of fitdata there, for example ~/.local/bin/fitdata.")


def _show(value: object) -> str:
    """Quote a refused value for an error message, shortened and with control characters made visible."""
    text = repr(str(value))
    return text if len(text) <= 80 else text[:77] + "...'"
