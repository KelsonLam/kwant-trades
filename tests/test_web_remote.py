"""Remote (password-protected) mode of the dashboard server. Test passwords are fabricated values."""
from __future__ import annotations

import http.client
import inspect
import json
import logging
import re
import socket
import struct
import threading
from pathlib import Path

import pytest

from kwanttrades.web import auth as authmod
from kwanttrades.web import server as srvmod
from kwanttrades.web.auth import AuthManager
from kwanttrades.web.server import PUBLIC_API, PUBLIC_FILES, create_server
from tests.test_web_server import make_app

PW = "fabricated-Test-pw-0001"
WRONG = "not-the-password-123"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class R:
    """A remote-mode server around a shared WebApp; records every response so tests can scan for leaks."""
    seen: list = []

    def __init__(self, app, clock=None, **kw):
        self.clock = clock or Clock()
        self.auth = AuthManager(PW, clock=self.clock, sleep=lambda s: None)
        self.server = create_server(app, 0, host="127.0.0.1", auth=self.auth, **kw)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def raw(self, method, path, body=None, headers=None, origin="auto", cookie=None, host=None):
        h = {}
        if origin == "auto":
            h["Origin"] = f"http://{host or '127.0.0.1:%d' % self.port}"
        elif origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = f"kwanttrades_session={cookie}"
        if host:
            h["Host"] = host
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            out = (r.status, r.read(), {k.lower(): v for k, v in r.getheaders()})
            R.seen.append(out)
            return out
        finally:
            conn.close()

    def js(self, method, path, body=None, **kw):
        s, data, h = self.raw(method, path, body, **kw)
        try:
            return s, json.loads(data), h
        except ValueError:
            return s, data, h

    def login(self, pw=PW, **kw):
        s, d, h = self.js("POST", "/api/login", {"password": pw}, **kw)
        tok = None
        m = re.search(r"kwanttrades_session=([^;]*)", h.get("set-cookie", ""))
        if m and s == 200:
            tok = m.group(1)
        return s, d, h, tok

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    a = make_app(tmp_path_factory.mktemp("remote"), fake=True)
    yield a
    a.close()


@pytest.fixture()
def make(app):
    made = []

    def build(**kw):
        r = R(app, **kw)
        made.append(r)
        return r

    yield build
    for r in made:
        r.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)

    def boom(*a, **k):
        raise AssertionError("AlpacaBroker must not be constructed in tests")

    monkeypatch.setattr("kwanttrades.execution.alpaca.AlpacaBroker", boom)


# --------------------------------------------------------------------------- startup rules
def test_remote_refuses_without_or_with_short_password(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here, so load_env finds nothing
    monkeypatch.delenv("KWANT_WEB_PASSWORD", raising=False)
    assert srvmod.main(["--remote", "--no-browser", "--port", "0", "--provider", "synthetic", "--config", str(tmp_path / "c.toml")]) == 2
    err = capsys.readouterr().err
    assert "KWANT_WEB_PASSWORD" in err and ".env" in err and "12" in err
    monkeypatch.setenv("KWANT_WEB_PASSWORD", "elevenchars")
    assert srvmod.main(["--remote", "--no-browser", "--port", "0", "--provider", "synthetic"]) == 2
    out = capsys.readouterr()
    assert "elevenchars" not in out.err + out.out
    with pytest.raises(authmod.PasswordError):
        AuthManager("elevenchars")
    AuthManager("twelvechars!")  # exactly 12 is accepted


def test_non_loopback_bind_needs_remote(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert srvmod.main(["--host", "0.0.0.0", "--no-browser", "--port", "0"]) == 2
    assert srvmod.main(["--behind-proxy", "--no-browser", "--port", "0"]) == 2
    assert srvmod.main(["--allowed-host", "x.test", "--no-browser", "--port", "0"]) == 2
    with pytest.raises(SystemExit):
        srvmod.main(["--host", "192.168.1.5"])  # exactly 127.0.0.1 or 0.0.0.0
    with pytest.raises(ValueError):
        create_server(None, 0, host="0.0.0.0", auth=None)
    with pytest.raises(ValueError):
        create_server(None, 0, host="192.168.1.5", auth=AuthManager(PW))


def test_remote_banner_content():
    text = srvmod.remote_banner(8787, "0.0.0.0")
    assert "plain HTTP" in text and "NOT encrypted" in text and "Tailscale" in text
    assert "firewall" in text and "PRIVATE" in text and "awake" in text
    for ip in authmod.lan_ipv4s():
        assert f"http://{ip}:8787" in text


def test_local_mode_unchanged(app):
    srv = create_server(app, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        def get(path, **h):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path, headers=h)
            r = c.getresponse()
            return r.status, r.read()
        assert get("/api/status")[0] == 200 and get("/")[0] == 200  # no login in local mode
        s, body = get("/api/session")
        assert s == 200 and json.loads(body) == {"remote": False, "authenticated": True}
        assert get("/api/status", Host="192.168.1.5:%d" % port)[0] == 403  # strict loopback-only Host
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/api/login", body=b'{"password":"x"}', headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
        assert c.getresponse().status == 404  # no login endpoint at all
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- everything is behind the login
def _api_routes():
    src = inspect.getsource(srvmod)
    paths = set(re.findall(r'"(/api/[a-z/]+)"', src))
    return sorted(paths)


def test_all_api_routes_and_static_files_require_a_session(make):
    r = make()
    routes = _api_routes()
    assert len(routes) >= 15, routes  # sanity: the enumeration really found the routes
    for p in routes + ["/api/positions/abc/close", "/api/surface?symbol=SPY", "/api/decisions?limit=5", "/api/nothing"]:
        if p in PUBLIC_API:
            continue
        for method in ("GET", "POST"):
            s, d, h = r.js(method, p, {} if method == "POST" else None)
            assert s == 401 and "error" in d, (method, p, s)
            assert "set-cookie" not in h
    root = srvmod.STATIC_ROOT
    files = [f.relative_to(root).as_posix() for f in root.rglob("*") if f.is_file()]
    assert "index.html" in files and "js/main.js" in files and "login.html" in files
    for f in files:
        s, body, h = r.raw("GET", "/" + f)
        if f in PUBLIC_FILES or f.startswith("icons/"):
            assert s == 200, f
        else:
            assert s == 302 and h["location"] == "/login.html" and body == b"", (f, s)
    for p in ("/", "/index.html", "/nope.js", "/js/screens/overview.js", "/.env", "/anything/at/all"):
        s, _, h = r.raw("GET", p)
        assert s == 302 and h["location"] == "/login.html", p
    assert r.raw("HEAD", "/")[0] == 302
    assert r.raw("POST", "/", {})[0] == 401
    # ...and with a session the same routes work
    s, d, h, tok = r.login()
    assert s == 200
    assert r.js("GET", "/api/status", cookie=tok)[0] == 200
    assert r.raw("GET", "/", cookie=tok)[0] == 200
    assert r.raw("GET", "/js/main.js", cookie=tok)[0] == 200


def test_public_pieces_and_session_endpoint(make):
    r = make()
    s, d, h = r.js("GET", "/api/session")
    assert s == 200 and d == {"remote": True, "authenticated": False}
    for p, mime in (("/login.html", "text/html"), ("/css/app.css", "text/css"), ("/js/theme.js", "text/javascript"),
                    ("/js/login.js", "text/javascript"), ("/sw.js", "text/javascript"),
                    ("/manifest.webmanifest", "application/manifest+json"), ("/icons/icon-192.png", "image/png"),
                    ("/js/pwa.js", "text/javascript")):
        s, body, h = r.raw("GET", p)
        assert s == 200 and h["content-type"].startswith(mime), (p, s, h.get("content-type"))
    html = r.raw("GET", "/login.html")[1].decode()
    assert "<script>" not in html and 'autocomplete="current-password"' in html and "<form" in html
    s, d, h, tok = r.login()
    assert r.js("GET", "/api/session", cookie=tok)[1] == {"remote": True, "authenticated": True}


# --------------------------------------------------------------------------- login, cookie, logout
def test_login_cookie_attributes_and_plain_http(make):
    r = make()
    s, d, h, tok = r.login()
    assert s == 200 and d == {"ok": True} and tok and len(tok) >= 40
    c = h["set-cookie"]
    assert c.startswith("kwanttrades_session=") and "HttpOnly" in c and "SameSite=Strict" in c and "Path=/" in c
    assert "Secure" not in c
    # X-Forwarded-Proto is ignored without --behind-proxy
    s, d, h, tok2 = r.login(headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in h["set-cookie"] and tok2 != tok


def test_secure_flag_only_behind_proxy_with_https(make):
    r = make(behind_proxy=True)
    assert "Secure" in r.login(headers={"X-Forwarded-Proto": "https"})[2]["set-cookie"]
    assert "Secure" not in r.login(headers={"X-Forwarded-Proto": "http"})[2]["set-cookie"]
    assert "Secure" not in r.login()[2]["set-cookie"]


def test_wrong_password_and_bad_bodies(make):
    r = make()
    s, d, h, tok = r.login(WRONG)
    assert s == 401 and tok is None and "set-cookie" not in h and "Wrong" in d["error"]
    assert r.js("POST", "/api/login", {"password": ""})[0] == 400
    assert r.js("POST", "/api/login", {"password": 5})[0] == 400
    assert r.js("POST", "/api/login", {"nope": 1})[0] == 400
    assert r.js("POST", "/api/login", {"password": "x" * 300})[0] == 400
    assert r.js("GET", "/api/login")[0] == 405


def test_logout_ends_the_session(make):
    r = make()
    tok = r.login()[3]
    assert r.js("GET", "/api/status", cookie=tok)[0] == 200
    s, d, h = r.js("POST", "/api/logout", {}, cookie=tok)
    assert s == 200 and "Max-Age=0" in h["set-cookie"]
    assert r.js("GET", "/api/status", cookie=tok)[0] == 401
    assert r.js("POST", "/api/logout", {})[0] == 401  # needs a session


def test_session_idle_and_absolute_expiry(make):
    clk = Clock()
    r = make(clock=clk)
    tok = r.login()[3]
    clk.advance(11 * 3600)
    assert r.js("GET", "/api/status", cookie=tok)[0] == 200  # activity resets the idle timer
    clk.advance(12 * 3600 + 1)
    assert r.js("GET", "/api/status", cookie=tok)[0] == 401  # idle for more than 12 h
    tok = r.login()[3]
    for _ in range(20):  # stay active every 11 h: the 7-day absolute lifetime still applies
        clk.advance(11 * 3600)
        if r.js("GET", "/api/status", cookie=tok)[0] == 401:
            break
    else:
        pytest.fail("a session must not outlive 7 days")
    assert clk.t - 1_000_000.0 > 7 * 24 * 3600 - 1  # (it died only after the 7th day)


def test_max_ten_sessions_evicts_oldest(make):
    clk = Clock()
    r = make(clock=clk)
    toks = []
    for _ in range(11):
        toks.append(r.login()[3])
        clk.advance(1)
    assert r.js("GET", "/api/status", cookie=toks[0])[0] == 401  # oldest evicted
    assert all(r.js("GET", "/api/status", cookie=t)[0] == 200 for t in toks[1:])
    assert r.auth.session_count() == 10


def test_sessions_are_dropped_on_restart(app):
    a = R(app)
    tok = a.login()[3]
    assert a.js("GET", "/api/status", cookie=tok)[0] == 200
    a.close()
    b = R(app)  # a new server process has a fresh session table
    try:
        assert b.js("GET", "/api/status", cookie=tok)[0] == 401
    finally:
        b.close()


# --------------------------------------------------------------------------- brute force
def test_lockout_after_five_failures_with_growth_and_recovery(make):
    clk = Clock()
    r = make(clock=clk)
    for i in range(5):
        assert r.login(WRONG)[0] == 401, i
    s, d, h, _ = r.login(WRONG)
    assert s == 429 and int(h["retry-after"]) > 0 and d["retry_after"] == int(h["retry-after"])
    assert r.login(PW)[0] == 429  # even the right password is refused while locked
    first = int(h["retry-after"])
    clk.advance(first + 1)
    assert r.login(PW)[0] == 200  # recovered (and a success clears the record)
    # second lock-out is longer
    for i in range(5):
        r.login(WRONG)
    clk.advance(first + 1)
    for i in range(5):
        r.login(WRONG)
    s, d, h, _ = r.login(WRONG)
    assert s == 429


def test_lockout_growth_is_exponential_and_capped():
    clk = Clock()
    a = AuthManager(PW, clock=clk, sleep=lambda s: None)
    waits = []
    for _ in range(9):
        for _ in range(5):
            a.record_failure("10.0.0.9")
        w = a.locked_for("10.0.0.9")
        waits.append(w)
        clk.advance(w + 1)
    assert waits[0] <= 61 and waits[1] > waits[0] and waits[2] > waits[1]
    assert max(waits) <= 3600 + 1 and waits[-1] >= 3599


def test_per_ip_isolation_and_xff_handling(make):
    r = make()  # not behind a proxy: X-Forwarded-For is ignored, all attempts come from the socket peer
    for i in range(5):
        assert r.login(WRONG, headers={"X-Forwarded-For": f"203.0.113.{i}"})[0] == 401
    assert r.login(PW, headers={"X-Forwarded-For": "198.51.100.7"})[0] == 429
    p = make(behind_proxy=True)  # behind a proxy the first hop is the client
    for i in range(5):
        assert p.login(WRONG, headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})[0] == 401
    assert p.login(WRONG, headers={"X-Forwarded-For": "203.0.113.5"})[0] == 429
    assert p.login(PW, headers={"X-Forwarded-For": "203.0.113.99"})[0] == 200  # a different client is unaffected
    assert p.login(PW, headers={"X-Forwarded-For": "garbage"})[0] == 200  # bad header falls back to the socket peer


def test_global_failure_cap(make):
    clk = Clock()
    p = make(clock=clk, behind_proxy=True)
    for i in range(30):
        assert p.login(WRONG, headers={"X-Forwarded-For": f"203.0.113.{i + 1}"})[0] == 401
    s, d, h, _ = p.login(PW, headers={"X-Forwarded-For": "198.51.100.200"})
    assert s == 429 and 0 < int(h["retry-after"]) <= 301
    clk.advance(301)
    assert p.login(PW, headers={"X-Forwarded-For": "198.51.100.200"})[0] == 200


def test_failed_attempts_are_delayed_and_logged_without_the_password(app, caplog):
    slept = []
    caplog.set_level(logging.DEBUG)
    r = R(app)
    r.auth.sleep = slept.append
    try:
        r.login(WRONG)
        assert slept == [authmod.FAIL_DELAY] and 0.25 <= authmod.FAIL_DELAY <= 0.35
        assert "failed login from 127.0.0.1" in caplog.text
        assert WRONG not in caplog.text
        r.login(PW)
        assert slept == [authmod.FAIL_DELAY]  # no delay on success
    finally:
        r.close()


# --------------------------------------------------------------------------- Host / Origin
def test_host_allow_list(make):
    r = make(allowed_hosts=("vpn.example.test", "tunnel.test:9999"))
    p = r.port
    names = ["127.0.0.1", "localhost", socket.gethostname().lower(), *authmod.lan_ipv4s()]
    for n in names:
        assert r.raw("GET", "/api/session", host=f"{n}:{p}")[0] == 200, n
    for bad in ("evil.com", f"evil.com:{p}", "127.0.0.1", f"127.0.0.1:{p + 1}", f"192.0.2.99:{p}", f"[::1]:{p}",
                f"127.0.0.1.evil.com:{p}"):
        assert r.raw("GET", "/api/session", host=bad)[0] == 403, bad
        assert r.raw("GET", "/login.html", host=bad)[0] == 403, bad
    assert r.raw("GET", "/api/session", host="vpn.example.test")[0] == 200  # --allowed-host without port
    assert r.raw("GET", "/api/session", host=f"vpn.example.test:{p}")[0] == 200
    assert r.raw("GET", "/api/session", host="VPN.Example.Test")[0] == 200
    assert r.raw("GET", "/api/session", host="tunnel.test:9999")[0] == 200  # exactly as given
    assert r.raw("GET", "/api/session", host="tunnel.test")[0] == 403
    assert r.raw("GET", "/api/session", host=f"tunnel.test:{p}")[0] == 403
    assert r.raw("GET", "/api/session", host="other.test")[0] == 403


def test_origin_must_match_the_request_host(make):
    r = make(allowed_hosts=("vpn.example.test",))
    p = r.port
    me = f"127.0.0.1:{p}"
    assert r.login()[0] == 200
    ok = lambda **kw: r.js("POST", "/api/login", {"password": PW}, **kw)[0]  # noqa: E731
    assert ok(origin=f"http://{me}") == 200
    assert ok(origin=f"http://localhost:{p}") == 403  # a different host than the one requested
    assert ok(origin="http://evil.com") == 403
    assert ok(origin=f"http://127.0.0.1:{p + 1}") == 403
    assert ok(origin="null") == 403
    assert ok(origin=None) == 403  # neither Origin nor Referer
    assert ok(origin=None, headers={"Referer": f"http://{me}/login.html"}) == 200
    assert ok(origin=None, headers={"Referer": "http://evil.com/login.html"}) == 403
    assert ok(origin="http://evil.com", headers={"Referer": f"http://{me}/"}) == 403
    assert ok(origin=f"http://vpn.example.test", host="vpn.example.test") == 200
    assert ok(origin=f"http://{me}", host="vpn.example.test") == 403
    assert ok(origin=f"https://vpn.example.test", host="vpn.example.test") == 200
    tok = r.login()[3]
    assert r.js("POST", "/api/session/stop", {}, cookie=tok, origin="http://evil.com")[0] == 403
    assert r.js("POST", "/api/session/stop", {}, cookie=tok)[0] == 200


# --------------------------------------------------------------------------- still strict once signed in
def test_traversal_env_and_alpaca_live_when_authenticated(make):
    r = make()
    tok = r.login()[3]
    for p in ("/.env", "/.env.example", "/../.env", "/..%2f.env", "/static/../../.env", "/%2e%2e/%2e%2e/.env",
              "/..%2f..%2fconfig.toml", "/../server.py", "/js/../../app.py", "/C:/Windows/win.ini", "//etc/passwd",
              "/..\\..\\.env", "/js", "/%252e%252e/x"):
        s, body, _ = r.raw("GET", p, cookie=tok)
        assert s in (403, 404), (p, s)
        assert b"API_KEY" not in body and b"import " not in body, p
    assert r.raw("GET", "/.env", cookie=tok)[0] == 404
    s, d, _ = r.js("POST", "/api/session/start", {"mode": "alpaca_live"}, cookie=tok)
    assert s == 400 and "cannot be started" in d["error"]
    assert r.js("POST", "/api/session/start", {"mode": "alpaca_paper"}, cookie=tok)[0] == 400  # no keys, never constructed


def test_security_headers_everywhere(make):
    r = make()
    tok = r.login()[3]
    cases = [("GET", "/api/session", None, None), ("GET", "/api/status", None, None), ("GET", "/api/status", None, tok),
             ("GET", "/", None, None), ("GET", "/", None, tok), ("GET", "/login.html", None, None),
             ("GET", "/js/login.js", None, None), ("GET", "/nope", None, tok), ("POST", "/api/login", {"password": WRONG}, None)]
    for m, p, b, c in cases:
        s, body, h = r.raw(m, p, b, cookie=c)
        assert h["x-content-type-options"] == "nosniff", (p, s)
        assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
        assert h["cache-control"] == "no-store"
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "manifest-src 'self'" in csp and "worker-src 'self'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "script-src 'self'" in csp


def test_password_never_leaks(make, caplog):
    caplog.set_level(logging.DEBUG)
    R.seen.clear()
    r = make()
    r.login(WRONG)
    s, d, h, tok = r.login()
    for p in ("/api/status", "/api/config", "/api/session", "/api/ideas", "/api/positions"):
        r.raw("GET", p, cookie=tok)
    for p in ("/login.html", "/", "/js/login.js", "/js/api.js"):
        r.raw("GET", p, cookie=tok)
    r.js("POST", "/api/logout", {}, cookie=tok)
    assert len(R.seen) >= 12
    needle = PW.encode()
    for status, body, headers in R.seen:
        assert needle not in body, status
        assert needle not in json.dumps(headers).encode()
    assert PW not in caplog.text and WRONG not in caplog.text
    cfg = json.dumps(r.js("GET", "/api/config", cookie=r.login()[3])[1])
    assert "password" not in cfg.lower()


# --------------------------------------------------------------------------- installable app files
def test_manifest_icons_and_meta():
    root = srvmod.STATIC_ROOT
    m = json.loads((root / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["name"] == "Kwant Trades" and m["display"] == "standalone" and m["start_url"] == "/"
    assert m["theme_color"] == m["background_color"] == "#0f1215"
    purposes = {i["purpose"] for i in m["icons"]}
    assert {"any", "maskable"} <= purposes
    for icon in m["icons"]:
        data = (root / icon["src"]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", data[16:24])
        assert f"{w}x{h}" == icon["sizes"]
    apple = (root / "icons/apple-touch-icon.png").read_bytes()
    assert struct.unpack(">II", apple[16:24]) == (180, 180)
    for page in ("index.html", "login.html"):
        html = (root / page).read_text(encoding="utf-8")
        assert "viewport-fit=cover" in html and 'rel="manifest"' in html and "apple-touch-icon" in html
        assert "apple-mobile-web-app-capable" in html and 'name="theme-color"' in html
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "no inline scripts"
    sw = (root / "sw.js").read_text(encoding="utf-8")
    assert "/api/" in sw and "login" in sw  # explicitly excluded from caching
    pwa = (root / "js/pwa.js").read_text(encoding="utf-8")
    assert "isSecureContext" in pwa


def test_rejected_post_does_not_poison_a_keep_alive_connection(make):
    """A POST answered without reading its body (401/403) must not leave bytes that corrupt the next request."""
    r = make()
    body = json.dumps({"mode": "shadow"}).encode()
    for headers in ({"Origin": f"http://127.0.0.1:{r.port}"}, {"Origin": "http://evil.com"}):
        c = http.client.HTTPConnection("127.0.0.1", r.port, timeout=10)
        c.request("POST", "/api/session/start", body=body, headers={**headers, "Content-Type": "application/json"})
        resp = c.getresponse()
        resp.read()
        assert resp.status in (401, 403)
        try:
            c.request("GET", "/api/session")  # same connection, if the server kept it open
            resp = c.getresponse()
            assert resp.status == 200 and json.loads(resp.read()) == {"remote": True, "authenticated": False}
        except (http.client.HTTPException, ConnectionError):
            pass  # the server closed the connection: also correct
        finally:
            c.close()
