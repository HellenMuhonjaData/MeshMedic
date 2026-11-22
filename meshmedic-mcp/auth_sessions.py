"""Web-app sign-in sessions, CSRF tokens, PIN re-entry windows and lockouts.

Pure in-memory state for one clinic machine (a restart signs everyone out,
which is the safe direction). Kept free of HTTP so it can be tested with a
fake clock; bridge_auth.py wires it to routes.

Rules:
* Session: idle timeout 60 min, absolute limit 12 h.
* Sign-in: 5 failed passwords for an account within 15 min locks that
  account's sign-in for 15 min.
* Signing PIN: entering it opens a 15-minute signing window for that
  session (one PIN per review session). 5 wrong PINs lock PIN entry for
  15 min. Each signature still needs its own attestation.
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

IDLE_SECONDS = 60 * 60
ABSOLUTE_SECONDS = 12 * 60 * 60
SIGNING_WINDOW_SECONDS = 15 * 60
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60


@dataclass
class Session:
    token: str
    clinician_id: str
    display_name: str
    csrf_token: str
    created_at: float
    last_seen: float
    signing_until: float = 0.0


@dataclass
class _Failures:
    times: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class AuthState:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._login_failures: dict[str, _Failures] = {}
        self._pin_failures: dict[str, _Failures] = {}

    # ---- sessions ----
    def create_session(self, clinician_id: str, display_name: str) -> Session:
        now = self._clock()
        session = Session(secrets.token_urlsafe(32), clinician_id, display_name, secrets.token_urlsafe(32), now, now)
        self._sessions[session.token] = session
        self._login_failures.pop(clinician_id, None)
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        now = self._clock()
        if now - session.last_seen > IDLE_SECONDS or now - session.created_at > ABSOLUTE_SECONDS:
            del self._sessions[token]
            return None
        session.last_seen = now
        return session

    def end(self, token: str | None) -> Session | None:
        return self._sessions.pop(token, None) if token else None

    # ---- signing window ----
    def open_signing_window(self, session: Session) -> float:
        session.signing_until = self._clock() + SIGNING_WINDOW_SECONDS
        self._pin_failures.pop(session.clinician_id, None)
        return session.signing_until

    def signing_window_open(self, session: Session) -> bool:
        return self._clock() < session.signing_until

    # ---- lockouts ----
    def _locked(self, table: dict[str, _Failures], key: str) -> bool:
        record = table.get(key)
        return bool(record and self._clock() < record.locked_until)

    def _fail(self, table: dict[str, _Failures], key: str) -> bool:
        """Record a failure; True if this failure locks the account."""
        now = self._clock()
        record = table.setdefault(key, _Failures())
        record.times = [t for t in record.times if now - t < FAILURE_WINDOW_SECONDS] + [now]
        if len(record.times) >= MAX_FAILURES:
            record.locked_until = now + LOCK_SECONDS
            record.times = []
            return True
        return False

    def login_locked(self, clinician_id: str) -> bool:
        return self._locked(self._login_failures, clinician_id)

    def record_login_failure(self, clinician_id: str) -> bool:
        return self._fail(self._login_failures, clinician_id)

    def pin_locked(self, clinician_id: str) -> bool:
        return self._locked(self._pin_failures, clinician_id)

    def record_pin_failure(self, clinician_id: str) -> bool:
        return self._fail(self._pin_failures, clinician_id)
