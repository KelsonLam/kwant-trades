"""HTTP layer of the local dashboard: static files + JSON API, bound to 127.0.0.1 only.

Entry point: `main(argv)` (used by `python -m kwanttrades web` and `python -m kwanttrades.web.server`).
Default (local) mode: loopback only, no login. Opt-in REMOTE mode (`--remote`): binds 0.0.0.0 (or --host), needs the
KWANT_WEB_PASSWORD environment variable (>= 12 characters), every route except the login page and its assets needs
a session cookie, and Host/Origin are checked against this machine's own names and addresses.
Security: Host allow-list (DNS-rebinding guard), same-origin check on every POST, JSON bodies only with a size cap,
strict Content-Security-Policy, no CORS, no directory listing, path-traversal protection, no secrets in any response.
"""
from __future__ import annotations

import argparse
import json
import logging
import ipaddress
import re
import sys
import time
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from kwanttrades.web import auth as authmod
from kwanttrades.web.app import HttpError, WebApp
from kwanttrades.web.serialize import jsonsafe

log = logging.getLogger("kwanttrades.web")

BIND_HOST = "127.0.0.1"  # deliberately not configurable
STATIC_ROOT = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")
MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8", ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8", ".webmanifest": "application/manifest+json; charset=utf-8",
}
# Files reachable without a session in remote mode: the sign-in page and what it (and the installable shell) needs.
PUBLIC_FILES = {"login.html", "css/app.css", "js/theme.js", "js/login.js", "js/pwa.js", "manifest.webmanifest", "sw.js"}
PUBLIC_PREFIXES = ("icons/",)
PUBLIC_API = {"/api/login", "/api/session"}
_BAD_CHARS = set('<>:"|?*\x00\\')
_POST_ROUTES = ("/api/scan", "/api/config", "/api/session/start", "/api/session/stop", "/api/session/flatten", "/api/backtest")
_CLOSE_RE = re.compile(r"^/api/positions/([^/]+)/close$")


class _Forbidden(Exception):
    pass


def resolve_static(root: Path, url_path: str) -> Path | None:
    """Map a URL path to a file under `root`. Raises _Forbidden for traversal attempts, returns None for 404."""
    raw = unquote(url_path)
    if any(c in _BAD_CHARS for c in raw):
        raise _Forbidden()
    segs = [p for p in raw.split("/") if p not in ("", ".")]
    if segs and segs[0] == "static":  # tolerate /static/... as an alias of /...
        segs = segs[1:]
    if any(p == ".." for p in segs):
        raise _Forbidden()
    if any(p.startswith(".") for p in segs):
        return None
    root_r = root.resolve()
    target = (root_r / "/".join(segs or ["index.html"])).resolve()
    if not target.is_relative_to(root_r):
        raise _Forbidden()
    if target.suffix.lower() not in MIME or not target.is_file():
        return None
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "Kwant Trades"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    # ------------------------------------------------------------------ plumbing
    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet; never log headers/bodies
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str, head_only: bool = False, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # One request per connection. Idle keep-alive sockets that the server later drops are reported by
        # mobile Safari as failed requests ("network connection was lost") instead of being retried, and an
        # unread request body would otherwise be parsed as the next request on a reused connection.
        self.close_connection = True
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any, extra: dict | None = None) -> None:
        body = json.dumps(jsonsafe(obj), allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra=extra)

    def _error(self, status: int, message: str, extra: dict | None = None) -> None:
        self._json(status, {"error": message}, extra)

    # ------------------------------------------------------------------ guards
    @property
    def remote(self) -> bool:
        return self.server.auth is not None  # type: ignore[attr-defined]

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not self.remote:
            return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
        return self.server.host_allowed(host)  # type: ignore[attr-defined]

    def _same_origin(self, value: str, allow_path: bool) -> bool:
        try:
            u = urlsplit(value)
            if self.remote:  # origin must be exactly the host:port the request was addressed to (already allow-listed)
                host = (self.headers.get("Host") or "").strip().lower()
                return (u.scheme in ("http", "https") and u.netloc.lower() == host and u.username is None
                        and (allow_path or u.path in ("", "/")) and not u.query and not u.fragment)
            return (u.scheme == "http" and (u.hostname or "").lower() in ("127.0.0.1", "localhost") and u.port == self.port
                    and u.username is None and (allow_path or u.path in ("", "/")) and not u.query and not u.fragment)
        except ValueError:
            return False

    def _guard(self, post: bool) -> bool:
        if not self._host_ok():
            self._error(403, "Forbidden: unexpected Host header.")
            return False
        if post:
            origin, referer = self.headers.get("Origin"), self.headers.get("Referer")
            if origin is not None:
                ok = self._same_origin(origin.strip(), allow_path=False)
            elif referer is not None:
                ok = self._same_origin(referer.strip(), allow_path=True)
            else:
                ok = False
            if not ok:
                self._error(403, "Forbidden: cross-origin request.")
                return False
        return True

    # ------------------------------------------------------------------ remote-mode identity
    def _client_ip(self) -> str:
        peer = self.client_address[0]
        if self.server.behind_proxy:  # type: ignore[attr-defined]
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(xff))
            except ValueError:
                return peer
        return peer

    def _https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        return bool(self.server.behind_proxy and proto == "https")  # type: ignore[attr-defined]

    def _token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
            m = c.get(authmod.COOKIE_NAME)
            return m.value if m else None
        except Exception:
            return None

    def _authed(self) -> bool:
        a = self.server.auth  # type: ignore[attr-defined]
        return a is None or a.valid(self._token())

    def _cookie_header(self, token: str, max_age: int) -> str:
        parts = [f"{authmod.COOKIE_NAME}={token}", "HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={max_age}"]
        if self._https():
            parts.append("Secure")
        return "; ".join(parts)

    def _read_json(self) -> Any:
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "Content-Length is required.")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Bad Content-Length.") from None
        if n < 0:
            raise HttpError(400, "Bad Content-Length.")
        if n > MAX_BODY:
            raise HttpError(413, f"Request body too large (limit {MAX_BODY // 1024} KiB).")
        raw = self.rfile.read(n) if n else b""
        self._body_read = True
        if not raw.strip():
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(415, "Send JSON with Content-Type: application/json.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "The request body is not valid JSON.") from None

    # ------------------------------------------------------------------ verbs
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _not_allowed(self) -> None:
        self._error(405, "Method not allowed.", {"Allow": "GET, HEAD, POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = lambda self: self._not_allowed()  # noqa: E731

    def _dispatch(self, method: str) -> None:
        self._body_read = False
        try:
            if len(self.path) > 2048:
                return self._error(414, "URI too long.")
            if not self.path.startswith("/"):
                return self._error(400, "Bad request path.")
            if not self._guard(method == "POST"):
                return
            parts = urlsplit(self.path)
            path = parts.path
            is_api = path.startswith("/api/") or path == "/api"
            auth = self.server.auth  # type: ignore[attr-defined]
            if auth is not None and is_api:
                if path.rstrip("/") not in PUBLIC_API and not self._authed():
                    return self._error(401, "Sign in required.")
            if is_api:
                return self._api(method, path, parse_qs(parts.query, keep_blank_values=True))
            if method == "POST":
                if auth is not None and not self._authed():
                    return self._error(401, "Sign in required.")
                return self._error(404, "Not found.")
            return self._static(path)
        except HttpError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("unhandled error serving %s", method)
            try:
                self._error(500, "Internal server error.")
            except Exception:
                pass

    # ------------------------------------------------------------------ static
    def _static(self, path: str) -> None:
        try:
            f = resolve_static(STATIC_ROOT, path)
        except _Forbidden:
            return self._error(403, "Forbidden.")
        if self.server.auth is not None and not self._authed():  # type: ignore[attr-defined]
            rel = f.relative_to(STATIC_ROOT.resolve()).as_posix() if f is not None else ""
            if not (rel in PUBLIC_FILES or rel.startswith(PUBLIC_PREFIXES)):
                return self._send(302, b"", "text/plain; charset=utf-8", extra={"Location": "/login.html"})
        if f is None:
            return self._error(404, "Not found.")
        self._send(200, f.read_bytes(), MIME[f.suffix.lower()])

    # ------------------------------------------------------------------ api
    def _api(self, method: str, path: str, q: dict[str, list[str]]) -> None:
        app = self.app
        path = path.rstrip("/") or "/"
        m = _CLOSE_RE.match(path)
        if path == "/api/session" and method in ("GET", "HEAD"):
            return self._json(200, {"remote": self.remote, "authenticated": self._authed()})
        if path in ("/api/login", "/api/logout") and self.remote:
            if method != "POST":
                return self._error(405, "Method not allowed.", {"Allow": "POST"})
            return self._login(self._read_json()) if path == "/api/login" else self._logout()
        if method == "POST":
            if path in _POST_ROUTES or m:
                body = self._read_json()
                return self._json(200, self._post(app, path, m, body))
            return self._error(405 if path.startswith("/api/") and self._is_get_route(path) else 404,
                               "Method not allowed." if self._is_get_route(path) else "Not found.")
        # GET / HEAD
        if (path in _POST_ROUTES and path != "/api/config") or m:
            return self._error(405, "Method not allowed.", {"Allow": "POST"})
        one = lambda k, default=None: (q.get(k) or [default])[0]  # noqa: E731
        if path == "/api/status":
            return self._json(200, app.status())
        if path == "/api/scan/status":
            return self._json(200, app.scan_status())
        if path == "/api/ideas":
            return self._json(200, app.ideas())
        if path == "/api/signals":
            return self._json(200, app.signals())
        if path == "/api/surface":
            sym = one("symbol")
            if not sym:
                raise HttpError(400, "symbol: required, e.g. /api/surface?symbol=SPY")
            return self._json(200, app.surface(sym))
        if path == "/api/positions":
            return self._json(200, app.positions())
        if path == "/api/equity":
            return self._json(200, app.equity())
        if path == "/api/decisions":
            raw = one("limit", "100")
            if not raw.isdigit() or not 1 <= int(raw) <= 500:
                raise HttpError(400, "limit: expected a whole number from 1 to 500")
            return self._json(200, app.decisions(int(raw)))
        if path == "/api/news":
            return self._json(200, app.news_items(one("symbol", "ALL")))
        if path == "/api/config":
            return self._json(200, app.get_config())
        if path == "/api/backtest/result":
            return self._json(200, app.backtest_result())
        self._error(404, "Not found.")

    def _login(self, body: Any) -> None:
        auth = self.server.auth  # type: ignore[attr-defined]
        ip = self._client_ip()
        pw = body.get("password") if isinstance(body, dict) else None
        if not isinstance(pw, str) or not pw or len(pw) > 256:
            raise HttpError(400, "password: required")
        wait = auth.locked_for(ip)
        if wait > 0:
            return self._json(429, {"error": f"Too many failed attempts. Try again in {max(1, round(wait / 60))} minute(s).",
                                    "retry_after": wait}, {"Retry-After": str(wait)})
        if auth.check_password(pw):
            auth.record_success(ip)
            token = auth.new_session()
            return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie_header(token, int(authmod.ABSOLUTE_LIFETIME))})
        auth.record_failure(ip)
        auth.sleep(authmod.FAIL_DELAY)
        self._error(401, "Wrong password.")

    def _logout(self) -> None:
        self.server.auth.drop(self._token())  # type: ignore[attr-defined]
        self._json(200, {"ok": True}, {"Set-Cookie": self._cookie_header("", 0)})

    @staticmethod
    def _is_get_route(path: str) -> bool:
        return path in ("/api/status", "/api/scan/status", "/api/ideas", "/api/signals", "/api/surface", "/api/positions",
                        "/api/equity", "/api/decisions", "/api/news", "/api/config", "/api/backtest/result")

    @staticmethod
    def _post(app: WebApp, path: str, m: re.Match | None, body: Any) -> Any:
        if m:
            return app.close_position(unquote(m.group(1)))
        if path == "/api/config":
            return app.update_config(body)
        if not isinstance(body, dict):
            raise HttpError(400, "The request body must be a JSON object.")
        if path == "/api/scan":
            if set(body) - {"symbols"}:
                raise HttpError(400, f"{sorted(set(body) - {'symbols'})[0]}: unknown field")
            return app.start_scan(body.get("symbols"))
        if path == "/api/session/start":
            if set(body) - {"mode"}:
                raise HttpError(400, f"{sorted(set(body) - {'mode'})[0]}: unknown field")
            return app.start_session(body.get("mode"))
        if path == "/api/session/stop":
            return app.stop_session()
        if path == "/api/session/flatten":
            return app.flatten()
        if path == "/api/backtest":
            return app.start_backtest(body)
        raise HttpError(404, "Not found.")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, port: int, app: WebApp, *, host: str = BIND_HOST, auth: "authmod.AuthManager | None" = None,
                 allowed_hosts: tuple[str, ...] = (), behind_proxy: bool = False):
        if host not in ("127.0.0.1", "0.0.0.0") and not is_tailscale_ip(host):
            raise ValueError("host must be 127.0.0.1, 0.0.0.0 or this machine's Tailscale address")
        if host != "127.0.0.1" and auth is None:
            raise ValueError("listening beyond this computer requires remote mode (authentication)")
        self.app = app
        self.auth = auth
        self.behind_proxy = behind_proxy
        self.extra_hosts = tuple(h.strip().lower() for h in allowed_hosts if h and h.strip())
        self._names_at = -1e9
        self._own: set[str] = set()
        super().__init__((host, int(port)), Handler)

    def host_allowed(self, host: str) -> bool:
        """Remote-mode Host allow-list: loopback names, this machine's hostname and current IPv4s (with the port),
        and any --allowed-host (bare, or with a port)."""
        port = self.server_address[1]
        now = time.monotonic()
        if now - self._names_at > 30.0:
            self._own = {f"{n}:{port}" for n in ["localhost", "127.0.0.1", *authmod.machine_names(), *authmod.lan_ipv4s()]}
            bound = self.server_address[0]
            if bound not in ("0.0.0.0", ""):
                self._own.add(f"{bound}:{port}")  # the address we listen on is always a valid Host
            self._names_at = now
        if host in self._own:
            return True
        return any(host == h or (":" not in h and host == f"{h}:{port}") for h in self.extra_hosts)


def create_server(app: WebApp, port: int = 8787, **kw: Any) -> DashboardServer:
    return DashboardServer(port, app, **kw)


# --------------------------------------------------------------------------- entry point
def build_app(args: argparse.Namespace) -> WebApp:
    from kwanttrades.app.session import ManualClock, default_provider_name, effective_time, make_provider, market_state
    from kwanttrades.config import load_config
    from kwanttrades.types import now_ny

    cfg = load_config(args.config)
    if args.db:
        cfg.broker.db_path = args.db
    name = args.provider or default_provider_name()
    clock = None
    if name == "synthetic":  # simulated data: analyse inside the entry window of the latest session, as the CLI does
        now = now_ny()
        clock = ManualClock(now if market_state(now) in ("opening_range", "open") else effective_time(now).replace(hour=11, minute=0))
    provider = make_provider(name, symbols=list(cfg.scan.universe), clock=clock)
    news = None
    if name != "synthetic" and cfg.sentiment.lookback_hours:
        from kwanttrades.data.news import CompositeNewsProvider

        news = CompositeNewsProvider()
    return WebApp(cfg, provider, provider_name=name, news=news, clock=clock, config_path=args.config)


def is_tailscale_ip(host: str) -> bool:
    """True for an IPv4 address in Tailscale's range (100.64.0.0/10)."""
    import ipaddress

    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def tailscale_ipv4() -> str | None:
    """This machine's Tailscale address, or None when Tailscale is not connected."""
    return next((ip for ip in authmod.lan_ipv4s() if is_tailscale_ip(ip)), None)


def remote_banner(port: int, host: str) -> str:
    if is_tailscale_ip(host):
        return "\n".join([
            "=" * 72, "Kwant Trades REMOTE mode is ON (password required), Tailscale only.", "",
            "Open this on a phone or computer signed in to your Tailscale network:",
            f"    http://{host}:{port}", "",
            "The dashboard listens on the Tailscale address only: other devices on your Wi-Fi or local",
            "network cannot reach it, and Tailscale encrypts the connection. This PC must stay on and awake.",
            "=" * 72])
    ips = authmod.lan_ipv4s()
    urls = [f"http://{ip}:{port}" for ip in ips] if host == "0.0.0.0" else [f"http://127.0.0.1:{port}"]
    lines = ["=" * 72, "Kwant Trades REMOTE mode is ON (password required).", "",
             "Open one of these on your phone (same Wi-Fi):"]
    lines += [f"    {u}" for u in urls] or ["    (no network address found; connect to Wi-Fi and restart)"]
    lines += ["",
              "WARNING: this is plain HTTP. Traffic on the network is NOT encrypted, so the password and your",
              "data can be read by anyone on that network. Use it only on your own home Wi-Fi, or over a private",
              "VPN such as Tailscale. Never expose this port to the internet.",
              "",
              "Windows may show a firewall prompt: choose PRIVATE networks only and allow it yourself. Kwant Trades",
              "does not change firewall settings. The PC must stay on and awake for the phone to connect.",
              "=" * 72]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import os

    ap = argparse.ArgumentParser(prog="python -m kwanttrades.web.server",
                                 description="Kwant Trades dashboard. Local mode (default) listens on 127.0.0.1 with no login; "
                                             "--remote adds password login and listens on your network.")
    ap.add_argument("--port", type=int, default=8787, help="TCP port (default 8787)")
    ap.add_argument("--provider", choices=["yahoo", "openbb", "crosscheck", "synthetic"],
                    help="market data source (default: crosscheck if OpenBB is installed, else yahoo)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the default browser")
    ap.add_argument("--config", default="config.toml", help="config file read at start and updated by Settings")
    ap.add_argument("--db", help="journal database path (overrides broker.db_path)")
    ap.add_argument("--remote", action="store_true",
                    help="opt-in: require a password (KWANT_WEB_PASSWORD in .env) and listen on all network interfaces")
    ap.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"],
                    help="bind address: exactly 127.0.0.1 or 0.0.0.0 (0.0.0.0 needs --remote; --remote implies it)")
    ap.add_argument("--tailscale", action="store_true",
                    help="remote mode reachable ONLY through Tailscale: listen on this machine's Tailscale address "
                         "instead of every network (implies --remote)")
    ap.add_argument("--behind-proxy", action="store_true",
                    help="trust X-Forwarded-For / X-Forwarded-Proto from a reverse proxy (remote mode only)")
    ap.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                    help="extra Host name accepted in remote mode, e.g. a VPN or tunnel name (repeatable)")
    args = ap.parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("error: --port must be between 0 and 65535", file=sys.stderr)
        return 2
    if args.tailscale:
        if args.host:
            print("error: use either --tailscale or --host, not both", file=sys.stderr)
            return 2
        args.remote = True
        ts_ip = tailscale_ipv4()
        if ts_ip is None:
            print("error: Tailscale is not connected on this computer. Open Tailscale, sign in, and try again.",
                  file=sys.stderr)
            return 2
        args.host = ts_ip
    host = args.host or ("0.0.0.0" if args.remote else BIND_HOST)
    if not args.remote and (host != BIND_HOST or args.behind_proxy or args.allowed_host):
        print("error: --host 0.0.0.0, --behind-proxy and --allowed-host need --remote (a password is mandatory "
              "before the dashboard may leave this computer).", file=sys.stderr)
        return 2
    auth = None
    if args.remote:
        from kwanttrades.config import load_env

        load_env(".env")  # the project's own loader; existing environment variables win
        problem = authmod.password_problem(os.environ.get("KWANT_WEB_PASSWORD"))
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        auth = authmod.AuthManager(os.environ["KWANT_WEB_PASSWORD"])
    try:
        app = build_app(args)
    except Exception as exc:
        print(f"error: cannot start the dashboard: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        server = create_server(app, args.port, host=host, auth=auth, allowed_hosts=tuple(args.allowed_host),
                               behind_proxy=args.behind_proxy)
    except OSError as exc:
        print(f"error: cannot listen on {host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    if args.remote:
        print(remote_banner(port, host), flush=True)
    print(f"Kwant Trades dashboard: {url}  (data source: {app.provider_name}; Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # pragma: no cover
            pass
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping...", flush=True)
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
