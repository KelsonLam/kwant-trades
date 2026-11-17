"""Remote-mode authentication: one shared password, server-side sessions, brute-force protection.

Nothing here logs or returns the password. Sessions live in memory only (all are dropped on restart); the table
stores a SHA-256 of each token, never the token itself. All clocks are injectable for tests.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("kwanttrades.web.auth")

COOKIE_NAME = "kwanttrades_session"
MIN_PASSWORD_LEN = 12
IDLE_TIMEOUT = 12 * 3600.0
ABSOLUTE_LIFETIME = 7 * 24 * 3600.0
MAX_SESSIONS = 10
WINDOW = 15 * 60.0
IP_MAX_FAILURES = 5
LOCK_BASE = 60.0
LOCK_CAP = 3600.0
GLOBAL_MAX_FAILURES = 30
GLOBAL_LOCK = 300.0
FAIL_DELAY = 0.3
MAX_TRACKED_IPS = 5000


class PasswordError(ValueError):
    pass


def password_problem(pw: str | None) -> str | None:
    if not pw or len(pw) < MIN_PASSWORD_LEN:
        return (f"remote mode needs a password of at least {MIN_PASSWORD_LEN} characters in the KWANT_WEB_PASSWORD "
                "environment variable. Add the line KWANT_WEB_PASSWORD=<your password> to the .env file yourself "
                "(Kwant Trades never writes or shows it), then start again.")
    return None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthManager:
    def __init__(self, password: str, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        problem = password_problem(password)
        if problem:
            raise PasswordError(problem)
        self._pw = password.encode("utf-8")
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, float]] = {}
        self._fails: dict[str, deque] = {}
        self._strikes: dict[str, int] = {}
        self._lock_until: dict[str, float] = {}
        self._global_fails: deque = deque()
        self._global_lock_until = 0.0

    # ------------------------------------------------------------------ password
    def check_password(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8"), self._pw)

    # ------------------------------------------------------------------ sessions
    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._purge(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["created"])
                del self._sessions[oldest]
            self._sessions[_digest(token)] = {"created": now, "last": now}
        return token

    def _purge(self, now: float) -> None:
        for k in [k for k, s in self._sessions.items()
                  if now - s["last"] > IDLE_TIMEOUT or now - s["created"] > ABSOLUTE_LIFETIME]:
            del self._sessions[k]

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = self.clock()
        with self._lock:
            s = self._sessions.get(_digest(token))
            if s is None:
                return False
            if now - s["last"] > IDLE_TIMEOUT or now - s["created"] > ABSOLUTE_LIFETIME:
                del self._sessions[_digest(token)]
                return False
            s["last"] = now
            return True

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(_digest(token), None)

    def session_count(self) -> int:
        with self._lock:
            self._purge(self.clock())
            return len(self._sessions)

    # ------------------------------------------------------------------ brute force
    def locked_for(self, ip: str) -> int:
        """Seconds the caller must wait (0 = free to try)."""
        now = self.clock()
        with self._lock:
            until = max(self._global_lock_until, self._lock_until.get(ip, 0.0))
            return int(until - now) + 1 if until > now else 0

    def record_failure(self, ip: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._fails) > MAX_TRACKED_IPS:
                self._fails.clear()
                self._strikes.clear()
                self._lock_until.clear()
            q = self._fails.setdefault(ip, deque())
            q.append(now)
            while q and now - q[0] > WINDOW:
                q.popleft()
            self._global_fails.append(now)
            while self._global_fails and now - self._global_fails[0] > WINDOW:
                self._global_fails.popleft()
            if len(q) >= IP_MAX_FAILURES:
                n = self._strikes.get(ip, 0)
                self._lock_until[ip] = now + min(LOCK_CAP, LOCK_BASE * (2 ** n))
                self._strikes[ip] = n + 1
                q.clear()
            if len(self._global_fails) >= GLOBAL_MAX_FAILURES:
                self._global_lock_until = now + GLOBAL_LOCK
                self._global_fails.clear()
        log.warning("failed login from %s at %s", ip, datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
            self._strikes.pop(ip, None)
            self._lock_until.pop(ip, None)


# --------------------------------------------------------------------------- network helpers
def lan_ipv4s() -> list[str]:
    """This machine's current non-loopback IPv4 addresses (no packets are sent)."""
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    out: list[str] = []
    for a in found:
        try:
            ip = ipaddress.IPv4Address(a)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or a in out:
            continue
        out.append(a)
    return out


def machine_names() -> list[str]:
    try:
        h = socket.gethostname().lower()
    except OSError:
        return []
    return [h, h + ".local"] if h else []
