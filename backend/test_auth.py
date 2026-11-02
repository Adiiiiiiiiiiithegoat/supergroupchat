"""Auth checks against the real app. Run: python -m backend.test_auth"""
import os
import tempfile
import time

from argon2 import PasswordHasher

tmp = tempfile.mkdtemp()
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test key "12345678901234567890"
os.environ.update(SGC_DB=os.path.join(tmp, "test.db"), PASSWORD_HASH=PasswordHasher().hash("right password"),
                  TOTP_SECRET=SECRET, SESSION_SECRET="s" * 64, ALLOW_LOCAL="0")
from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from . import auth, db, main  # noqa: E402  (must import after the env is set)

ORIGIN = {"origin": auth.PUBLIC_ORIGIN}


def client(ip="1.2.3.4", host="testclient"):
    # https: the session cookie is Secure, so the cookie jar only sends it over https
    return TestClient(main.app, base_url="https://testserver", follow_redirects=False,
                      headers={**ORIGIN, "cf-connecting-ip": ip} if ip else ORIGIN, client=(host, 50000))


def code(step=0):
    return auth.totp(SECRET, int(time.time() // 30) + step)


def login(c, pw="right password", totp=None, fresh=True):
    if fresh:
        auth.last_counter = 0  # each test logs in again within the same 30s step
    return c.post("/login", data={"password": pw, "code": totp or code()})


def ws(c, headers=None):
    # TestClient dials ws://, which the jar won't send a Secure cookie over; a browser uses wss://
    cookie = {"cookie": f"{auth.COOKIE}={c.cookies[auth.COOKIE]}"} if auth.COOKIE in c.cookies else {}
    return c.websocket_connect("/ws", headers={**(headers or {}), **cookie})


def ws_rejected(c, headers=None):
    try:
        with ws(c, headers):
            return False
    except WebSocketDisconnect as e:
        return e.code == 1008


def test_rfc6238_vector():
    assert auth.totp(SECRET, 59 // 30) == "287082"  # RFC 6238 appendix B, T=59, last 6 digits


def test_no_session_rejected():
    c = client()
    assert c.get("/api/runs").status_code == 401
    assert c.post("/api/runs", json={"workspace": tmp}).status_code == 401
    r = c.get("/")
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    assert c.get("/login").status_code == 200
    assert ws_rejected(c, ORIGIN)


def test_login_then_everything_works():
    c = client()
    r = login(c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert all(f in cookie for f in ("httponly", "secure", "samesite=strict", f"max-age={7 * 86400}"))
    assert c.get("/api/runs").status_code == 200 and c.get("/").status_code == 200
    with ws(c, ORIGIN):
        pass


def test_wrong_password():
    c = client()
    r = login(c, pw="wrong")
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert c.get("/api/runs").status_code == 401


def test_wrong_and_reused_totp():
    c = client()
    assert login(c, totp=code(5)).status_code == 401  # a code from 2.5 minutes ahead
    assert login(c, totp="abcdef").status_code == 401
    used = code()
    assert login(c, totp=used).status_code == 303
    assert login(client(), totp=used, fresh=False).status_code == 401  # replay of the same code
    assert login(client(), totp=code(-1), fresh=False).status_code == 401  # an older one


def test_lockout_by_cf_connecting_ip():
    c = client(ip="9.9.9.9")
    for _ in range(auth.MAX_FAILS):
        assert login(c, pw="wrong").status_code == 401
    r = login(c)  # right credentials, but locked
    assert r.status_code == 429 and "set-cookie" not in r.headers
    assert login(client(ip="8.8.8.8")).status_code == 303  # other IPs unaffected
    log = open(auth.LOG, encoding="utf-8").read()
    assert log.count("failed login from 9.9.9.9") == auth.MAX_FAILS + 1 and "locked out for 15 min" in log
    auth.locked_until["9.9.9.9"] = time.time() - 1  # 15 minutes later
    assert login(c).status_code == 303


def test_bad_origin():
    c = client()
    login(c)
    for headers in ({"origin": "https://evil.example"}, {"origin": "null"}):
        assert c.post("/api/runs", json={"workspace": tmp}, headers=headers).status_code == 403
        assert ws_rejected(c, headers)
    no_origin = TestClient(main.app, base_url="https://testserver", cookies=c.cookies)
    assert no_origin.post("/api/runs", json={"workspace": tmp}).status_code == 403
    assert ws_rejected(no_origin)
    assert c.post("/login", data={"password": "x", "code": "1"}, headers={"origin": "https://evil.example"}).status_code == 403


def test_expired_or_forged_session():
    c = client()
    login(c)
    sid, _, sig = c.cookies[auth.COOKIE].partition(".")
    c.cookies.set(auth.COOKIE, f"{sid}.{'0' * len(sig)}", domain="testserver.local")
    assert c.get("/api/runs").status_code == 401
    c.cookies.set(auth.COOKIE, f"{sid}.{sig}", domain="testserver.local")
    assert c.get("/api/runs").status_code == 200
    db.conn.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert c.get("/api/runs").status_code == 401
    assert ws_rejected(c, ORIGIN)


def test_logout_kills_session_server_side():
    c = client()
    login(c)
    value = c.cookies[auth.COOKIE]
    r = c.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    replay = TestClient(main.app, base_url="https://testserver", headers=ORIGIN, cookies={auth.COOKIE: value})
    assert replay.get("/api/runs").status_code == 401
    assert ws_rejected(replay, ORIGIN)


def test_allow_local():
    local = client(ip=None, host="127.0.0.1")
    assert local.get("/api/runs").status_code == 401  # ALLOW_LOCAL=0
    auth.ALLOW_LOCAL = True
    try:
        assert local.get("/api/runs").status_code == 200
        with ws(local, ORIGIN):
            pass
        assert client(ip="5.5.5.5", host="127.0.0.1").get("/api/runs").status_code == 401  # via the tunnel
        assert client(ip=None).get("/api/runs").status_code == 401  # not loopback
    finally:
        auth.ALLOW_LOCAL = False


def run_all():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


if __name__ == "__main__":
    run_all()
