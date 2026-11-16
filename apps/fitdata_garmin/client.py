"""Garmin Connect session: login once (tokens saved, ~1 year), then polite, throttled calls."""
from __future__ import annotations

import getpass
import logging
import os
import time
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from . import paths
from .errors import NeedsHuman, Temporary

log = logging.getLogger("fitdata")

# Seconds between calls. Garmin rate-limits hard; a backfill is ~35 calls per day of history.
THROTTLE = 0.7


class NotLoggedIn(NeedsHuman):
    """No saved Garmin login, or it stopped working: a person has to run `fitdata login` (exit 6)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, id="garmin_login",
                         action="Sign in to Garmin again: run `fitdata login` in a terminal (it asks for the "
                         "email, password and 2FA code) on the machine that keeps the archive, or on a "
                         "machine whose fitdata reaches it over SSH.",
                         rerun="fitdata sync")


class RateLimited(Temporary):
    """Garmin kept answering "too many requests" after every wait (exit 75)."""


def _unreachable(e: Exception) -> Temporary:
    return Temporary(f"fitdata could not reach Garmin Connect ({str(e)[:160]}). Nothing was lost; the "
                     "next run carries on where this one stopped.", hint="Check the network and try again later.")


# The most bytes one answer from Garmin may hold (after decompression). Real answers are a few MB
# (an activity's details, a zip of FIT files); this keeps a broken or hostile answer from filling
# the memory before it is even looked at.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# The saved login is a small JSON file; anything far larger is not one.
MAX_TOKEN_BYTES = 1024 * 1024


class ResponseTooLarge(Exception):
    """An answer from Garmin was larger than MAX_RESPONSE_BYTES; it was dropped unread."""


def _limit_response(resp, *args: Any, **kwargs: Any):
    """A requests response hook: reads the body in pieces and stops past MAX_RESPONSE_BYTES.
    Hooks run before requests reads the body, so a huge answer is never held in memory."""
    limit = MAX_RESPONSE_BYTES
    too_big = ResponseTooLarge(f"Garmin sent an answer larger than {limit // 2**20} MB, far larger than any "
                               "real one, so fitdata dropped it without reading it all.")
    length = resp.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > limit:
        resp.close()
        raise too_big
    if getattr(resp, "_content", False) is not False:  # already read
        if len(resp._content or b"") > limit:
            raise too_big
        return resp
    chunks, total = [], 0
    for chunk in resp.iter_content(1024 * 1024):
        total += len(chunk)
        if total > limit:
            resp.close()
            raise too_big
        chunks.append(chunk)
    resp._content = b"".join(chunks)
    resp._content_consumed = True
    return resp


def _cap_responses(api: Any) -> None:
    """Install _limit_response on the session garminconnect uses for its data calls."""
    sess = getattr(getattr(api, "client", None), "_api_session", None)
    hooks = getattr(sess, "hooks", None)
    if isinstance(hooks, dict):
        hooks.setdefault("response", [])
        if _limit_response not in hooks["response"]:
            hooks["response"].append(_limit_response)
    else:
        log.warning("fitdata could not cap the size of Garmin's answers with this version of "
                    "garminconnect; it carries on without the cap.")


def _damaged_login(why: str) -> "NotLoggedIn":
    return NotLoggedIn(f"The saved Garmin login is damaged ({why}), so fitdata cannot use it. "
                       "Run: fitdata login")


def check_token_file() -> None:
    """The saved login must be a small, ordinary JSON file holding the fields garminconnect reads.
    Anything else needs a new login (exit 6), not a retry (exit 75)."""
    import json
    import stat
    try:
        fd = os.open(paths.TOKENS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise NotLoggedIn("No saved Garmin login. Run: fitdata login") from None
    except OSError as e:
        raise _damaged_login(f"it cannot be opened: {e.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _damaged_login("it is not an ordinary file")
        data = fh.read(MAX_TOKEN_BYTES + 1)
    if len(data) > MAX_TOKEN_BYTES:
        raise _damaged_login("it is far larger than a login")
    try:
        token = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        raise _damaged_login("it is not valid JSON") from None
    if not isinstance(token, dict) or not isinstance(token.get("di_token"), str) or not token["di_token"]:
        raise _damaged_login("it does not hold a Garmin token")
    for key in ("di_refresh_token", "di_client_id"):
        if token.get(key) is not None and not isinstance(token[key], str):
            raise _damaged_login(f"its {key} is not text")


class Session:
    def __init__(self) -> None:
        self.api: Garmin | None = None
        self._last = 0.0
        self.calls = 0

    def connect(self) -> "Session":
        check_token_file()
        api = Garmin()
        _cap_responses(api)
        try:
            api.login(tokenstore=str(paths.TOKENS))
        except GarminConnectAuthenticationError as e:
            raise NotLoggedIn(f"Saved Garmin login no longer works ({e}). Run: fitdata login") from e
        except GarminConnectTooManyRequestsError as e:
            raise RateLimited("Garmin Connect is limiting requests right now, so fitdata stopped before "
                              "fetching anything.", hint="Try again later; the timers do this by themselves.") from e
        except GarminConnectConnectionError as e:
            if "Token path not loading cleanly" in str(e) or "Token extraction" in str(e):
                raise _damaged_login("garminconnect could not read it") from e
            raise _unreachable(e) from e
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            # garminconnect choked on the content of the saved login itself.
            raise _damaged_login(f"{type(e).__name__}") from e
        self.api = api
        return self

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Throttled call with backoff on rate limits and transient errors."""
        delays = [30, 120, 600]
        for attempt in range(len(delays) + 1):
            wait = THROTTLE - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                return fn(*args, **kwargs)
            except GarminConnectAuthenticationError as e:
                raise NotLoggedIn(f"Garmin login expired ({e}). Run: fitdata login") from e
            except GarminConnectTooManyRequestsError as e:
                if attempt == len(delays):
                    raise RateLimited(
                        "Garmin Connect kept limiting requests after fitdata waited several times, so this "
                        "run stopped. What was fetched so far is kept; the next run carries on from there.",
                        hint="Try again later; the timers do this by themselves.") from e
                log.warning("Garmin rate limit; waiting %ss", delays[attempt])
                time.sleep(delays[attempt])
            except GarminConnectConnectionError as e:
                msg = str(e)
                # 404/400/204-style "no data" answers are final, not transient.
                if any(code in msg for code in ("404", "400", "204", "Not Found")) or attempt == len(delays):
                    raise
                log.warning("Garmin connection error (%s); retrying in %ss", msg[:120], delays[attempt] // 6)
                time.sleep(delays[attempt] // 6)
        raise RuntimeError("unreachable")


def interactive_login() -> None:
    """Ask for email, password and (if needed) the 2FA code in this terminal; save tokens."""
    print("Garmin Connect login. Your password is used once and never stored;")
    print(f"a login token is saved to {paths.TOKENS} (owner-only permissions).\n")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    api = Garmin(email=email, password=password, prompt_mfa=lambda: input("2FA code: ").strip())
    paths.STATE.mkdir(parents=True, exist_ok=True)
    paths.STATE.chmod(0o700)
    api.login(tokenstore=str(paths.TOKENS))
    if not paths.TOKENS.exists():
        api.client.dump(str(paths.TOKENS))
    name = api.get_full_name() or "?"
    print(f"\nLogged in as {name}. Token saved. You can close this window.")
