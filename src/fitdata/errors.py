"""Errors with fixed exit codes, the same codes every my-easy-software app uses.

    0   ok
    1   error            something went wrong (a config value fitdata cannot use, a broken file...)
    2   usage            the command line or a value in it was not understood (BadInput)
    3   not found        the day, activity, index or bucket asked for does not exist
    4   not allowed      the command is above the level MES_ALLOW gives this session
    5   refused          refused for safety (a forwarded decode, a copy that did not verify)
    6   needs a person   a person must do something first, for example `fitdata login`
    75  temporary        try again later: Garmin's rate limit, or another sync holds the lock

Every message says what happened and what to do next, in plain words.
"""
from __future__ import annotations

OK, ERROR, USAGE, NOT_FOUND, NOT_ALLOWED, REFUSED, NEEDS_HUMAN, TEMPORARY = 0, 1, 2, 3, 4, 5, 6, 75

EXIT_CODES = {
    "0": "ok", "1": "error", "2": "usage", "3": "not found", "4": "not allowed",
    "5": "refused for safety", "6": "needs a person", "75": "temporary failure, retry",
}


class FitdataError(Exception):
    """Base class: exit code 1. `kind` is the short machine word printed with --json."""

    exit_code = ERROR
    kind = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class NotFound(FitdataError):
    exit_code = NOT_FOUND
    kind = "not_found"


class NotAllowed(FitdataError):
    exit_code = NOT_ALLOWED
    kind = "not_allowed"


class Refused(FitdataError):
    exit_code = REFUSED
    kind = "refused"


class Temporary(FitdataError):
    exit_code = TEMPORARY
    kind = "temporary"


class ConfigProblem(FitdataError):
    kind = "config_invalid"


class NeedsHuman(FitdataError):
    """A person has to act first (exit 6). `action` says what, `rerun` what to run afterwards."""

    exit_code = NEEDS_HUMAN
    kind = "needs_human"

    def __init__(self, message: str, *, id: str, action: str, rerun: str | None = None) -> None:
        super().__init__(message)
        self.id = id
        self.action = action
        self.rerun = rerun

    def payload(self) -> dict:
        # The shape of my-easy-software's needs_human result, so agents and mes read it the same way.
        return {"status": "needs_human", "id": self.id, "action": self.action, "why": self.message,
                "url": None, "rerun": self.rerun}
