"""Garmin Connect session: login once (tokens saved, ~1 year), then polite, throttled calls."""
from __future__ import annotations

import getpass
import logging
import time
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from . import paths

log = logging.getLogger("fitdata")

# Seconds between calls. Garmin rate-limits hard; a backfill is ~35 calls per day of history.
THROTTLE = 0.7


class NotLoggedIn(RuntimeError):
    pass


class Session:
    def __init__(self) -> None:
        self.api: Garmin | None = None
        self._last = 0.0
        self.calls = 0

    def connect(self) -> "Session":
        if not paths.TOKENS.exists():
            raise NotLoggedIn("No saved Garmin login. Run: fitdata login")
        api = Garmin()
        try:
            api.login(tokenstore=str(paths.TOKENS))
        except GarminConnectAuthenticationError as e:
            raise NotLoggedIn(f"Saved Garmin login no longer works ({e}). Run: fitdata login") from e
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
            except GarminConnectTooManyRequestsError:
                if attempt == len(delays):
                    raise
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
