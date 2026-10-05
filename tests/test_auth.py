import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atvrx.auth import AccountError, LoginThrottle, Sessions, UserStore, hash_password, verify_password
from atvrx.session import Config, Session
from atvrx.web import COOKIE, create_app

PW = "correct horse battery"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def users(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.add("alice", PW)
    return store


@pytest.fixture
def app_parts(tmp_path, users):
    clock = Clock()
    throttle = LoginThrottle(clock=clock)
    sessions = Sessions(users, max_age_s=3600, clock=clock)
    app = create_app(Session(Config(), tmp_path), users=users, throttle=throttle, sessions=sessions)
    return app, clock


@pytest.fixture
def client(app_parts):
    app, _ = app_parts
    with TestClient(app, headers={"X-ATVRX": "1"}) as c:
        yield c


def login(c, name="alice", pw=PW):
    return c.post("/api/login", json={"username": name, "password": pw})


# -- passwords and accounts -------------------------------------------------------------------------
def test_password_hash_roundtrip():
    h = hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert verify_password(PW, h) and not verify_password(PW + "x", h)
    assert not verify_password(PW, "garbage")


def test_store_rules_and_file(tmp_path, users):
    with pytest.raises(AccountError, match="already exists"):
        users.add("alice", PW)
    with pytest.raises(AccountError, match="at least 10"):
        users.add("bob", "short")
    with pytest.raises(AccountError, match="Usernames"):
        users.add("bad name!", PW)
    assert users.verify("alice", PW) and not users.verify("alice", "wrong password")
    assert not users.verify("nobody", PW)
    raw = (tmp_path / "users.json").read_text()
    assert PW not in raw and json.loads(raw)["users"]["alice"]["hash"].startswith("scrypt$")
    assert (tmp_path / "users.json").stat().st_mode & 0o077 == 0
    users.remove("alice")
    assert users.names() == []


def test_store_sees_changes_made_by_another_process(tmp_path, users):
    other = UserStore(tmp_path / "users.json")
    other.add("bob", PW)
    assert users.names() == ["alice", "bob"]


def test_cli_manages_users(tmp_path):
    env = {"ATVRX_DATA": str(tmp_path), "PATH": "/usr/bin:/bin"}
    root = str(Path(__file__).resolve().parent.parent)
    run = lambda *a, stdin="": subprocess.run([sys.executable, "-m", "atvrx.users", *a], input=stdin, text=True,
                                              capture_output=True, cwd=root, env=env)
    r = run("add", "carol", "--password-stdin", stdin=PW + "\n")
    assert r.returncode == 0, r.stderr
    assert run("list").stdout.split() == ["carol"]
    assert run("add", "dave", "--password-stdin", stdin="short\n").returncode == 1
    assert run("passwd", "carol", "--password-stdin", stdin="another long password\n").returncode == 0
    assert UserStore(tmp_path / "users.json").verify("carol", "another long password")
    assert run("remove", "carol").returncode == 0


# -- throttle (fake clock, no real waiting) ------------------------------------------------------------
def test_throttle_locks_account_then_releases():
    clock = Clock()
    t = LoginThrottle(window_s=900, per_ip=10, per_account=5, total=100, clock=clock)
    for _ in range(5):
        assert t.retry_after("1.1.1.1", "alice") == 0
        t.failed("1.1.1.1", "alice")
    assert t.retry_after("1.1.1.1", "alice") == pytest.approx(900)
    assert t.retry_after("2.2.2.2", "ALICE") > 0           # account limit holds from any address
    assert t.retry_after("1.1.1.1", "bob") == 0            # other accounts are not locked yet
    clock.t += 901
    assert t.retry_after("1.1.1.1", "alice") == 0


def test_throttle_per_address_and_success_does_not_reset_address():
    clock = Clock()
    t = LoginThrottle(window_s=900, per_ip=10, per_account=5, total=100, clock=clock)
    for i in range(10):
        t.failed("1.1.1.1", f"user{i}")
    t.succeeded("1.1.1.1", "mallory")
    assert t.retry_after("1.1.1.1", "someone-else") > 0


def test_throttle_global_cap():
    t = LoginThrottle(window_s=900, per_ip=1000, per_account=1000, total=3, clock=Clock())
    for i in range(3):
        t.failed(f"10.0.0.{i}", f"u{i}")
    assert t.retry_after("10.9.9.9", "fresh") > 0


# -- sessions -------------------------------------------------------------------------------------
def test_sessions_expire_and_end_on_password_change(users):
    clock = Clock()
    s = Sessions(users, max_age_s=100, clock=clock)
    tok = s.create("alice")
    assert s.user(tok) == "alice"
    clock.t += 101
    assert s.user(tok) is None
    tok = s.create("alice")
    users.set_password("alice", "a brand new password")
    assert s.user(tok) is None
    assert s.user("forged-token") is None


# -- the web app ------------------------------------------------------------------------------------
def test_everything_needs_sign_in(client):
    assert client.get("/healthz").status_code == 200
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/api/state", "/api/options", "/api/snapshot.png", "/static/app.js"):
        assert client.get(path, follow_redirects=False).status_code in (401, 303), path
    assert client.post("/api/watch", json={}).status_code == 401
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path, follow_redirects=False).status_code in (303, 404), path
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}):
            pass
    assert client.get("/login").status_code == 200
    assert client.get("/api/login-info").json() == {"has_users": True}


def test_sign_in_sets_a_strict_cookie_and_opens_the_app(client):
    assert login(client, pw="wrong password").status_code == 401
    assert login(client, name="nobody").json()["detail"] == "Wrong username or password."
    r = login(client)
    assert r.status_code == 200 and r.json() == {"user": "alice"}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert client.get("/api/me").json() == {"user": "alice"}
    assert client.get("/api/state").status_code == 200
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert "state" in ws.receive_json()
    assert client.get("/login", follow_redirects=False).status_code == 303
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/state").status_code == 401


def test_cookie_is_secure_over_https(app_parts):
    app, _ = app_parts
    with TestClient(app, base_url="https://testserver", headers={"X-ATVRX": "1"}) as c:
        assert "secure" in login(c).headers["set-cookie"].lower()


def test_cross_site_and_headerless_requests_are_refused(client):
    login(client)
    assert client.post("/api/stop", headers={"X-ATVRX": ""}).status_code == 403
    assert client.post("/api/stop", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/stop", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
            pass
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass


def test_failed_sign_ins_are_throttled(app_parts):
    app, clock = app_parts
    with TestClient(app, headers={"X-ATVRX": "1"}) as c:
        for _ in range(5):
            assert login(c, pw="wrong password").status_code == 401
        r = login(c)                                            # even the right password waits now
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
        assert "Try again in 15 minutes" in r.json()["detail"]
        clock.t += 901
        assert login(c).status_code == 200


def test_password_change(client, app_parts):
    app, _ = app_parts
    login(client)
    r = client.post("/api/password", json={"current": "wrong password", "new": "a new long password"})
    assert r.status_code == 400 and "current password is wrong" in r.json()["detail"]
    r = client.post("/api/password", json={"current": PW, "new": "short"})
    assert r.status_code == 400 and "at least 10" in r.json()["detail"]
    with TestClient(app, headers={"X-ATVRX": "1"}) as other:            # a second device
        login(other)
        assert client.post("/api/password", json={"current": PW, "new": "a new long password"}).status_code == 200
        assert other.get("/api/state").status_code == 401              # signed out elsewhere
    assert client.get("/api/state").status_code == 200                 # this device keeps a fresh session
    assert app.state.users.verify("alice", "a new long password")


def test_admin_bootstrap_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ATVRX_DATA", str(tmp_path))
    monkeypatch.setenv("ATVRX_ADMIN_USER", "admin")
    monkeypatch.setenv("ATVRX_ADMIN_PASSWORD", PW)
    app = create_app(Session(Config(), tmp_path))
    assert app.state.users.verify("admin", PW)
    monkeypatch.setenv("ATVRX_ADMIN_PASSWORD", "something else entirely")
    create_app(Session(Config(), tmp_path))                            # existing account is left alone
    assert UserStore(tmp_path / "users.json").verify("admin", PW)


def test_proxy_trust_takes_rightmost_untrusted_address(monkeypatch):
    from uvicorn.middleware.proxy_headers import _TrustedHosts
    from atvrx.web import trusted_proxies
    hosts = _TrustedHosts(trusted_proxies())
    # client-sent junk first, then the real client, then the proxy (as Nginx Proxy Manager appends)
    assert hosts.get_trusted_client_address("6.6.6.6, 1.2.3.4, 192.168.1.30")[0] == "1.2.3.4"
    assert "203.0.113.9" not in hosts
    monkeypatch.setenv("ATVRX_TRUSTED_PROXIES", "none")
    assert trusted_proxies() == ""
    monkeypatch.setenv("ATVRX_TRUSTED_PROXIES", "192.168.1.10")
    assert "192.168.1.10" in _TrustedHosts(trusted_proxies()) and "192.168.1.11" not in _TrustedHosts(trusted_proxies())


def test_no_users_shows_setup(tmp_path):
    app = create_app(Session(Config(), tmp_path), users=UserStore(tmp_path / "empty.json"))
    with TestClient(app) as c:
        assert c.get("/api/login-info").json() == {"has_users": False}
