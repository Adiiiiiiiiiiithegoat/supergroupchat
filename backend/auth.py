"""Single-user auth: argon2 password + TOTP, server-side sessions, Origin checks, login lockout."""
import base64
import hashlib
import hmac
import html
import os
import secrets
import struct
import time
from urllib.parse import parse_qs

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection

from . import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_ORIGIN = "https://groupchat.solutionhaven.net"
PORT = os.getenv("PORT", "8787")
ORIGINS = {PUBLIC_ORIGIN, f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
COOKIE = "sgc_session"
SESSION_TTL = 7 * 86400
MAX_FAILS, LOCKOUT = 5, 15 * 60

PASSWORD_HASH = os.getenv("PASSWORD_HASH", "")
TOTP_SECRET = os.getenv("TOTP_SECRET", "")
# ponytail: without SESSION_SECRET in .env (auth_setup writes it) every restart logs you out
SESSION_SECRET = (os.getenv("SESSION_SECRET") or secrets.token_hex(32)).encode()
ALLOW_LOCAL = os.getenv("ALLOW_LOCAL") == "1"
LOG = os.path.join(os.path.dirname(db.DB_PATH), "auth.log")
LOGIN_HTML = open(os.path.join(ROOT, "frontend", "login.html"), encoding="utf-8").read()

# ponytail: in memory, so a restart clears lockouts and re-allows a TOTP code from the last ~90s
fails: dict[str, list[float]] = {}
locked_until: dict[str, float] = {}
last_counter = 0


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(f"[sgc] {line}", flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def totp(secret, counter):
    """RFC 6238, SHA-1, 6 digits, 30s steps (what authenticator apps default to)."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def check_totp(code):
    """Accepts one step of clock drift either way; a code (or an older one) can't be used twice."""
    global last_counter
    c = int(time.time() // 30)
    for n in (c - 1, c, c + 1):
        if n > last_counter and hmac.compare_digest(totp(TOTP_SECRET, n).encode(), code.encode()):
            last_counter = n
            return True
    return False


def check_password(pw):
    try:
        return PasswordHasher().verify(PASSWORD_HASH, pw)
    except (VerificationError, InvalidHashError):
        return False


def sign(sid):
    return hmac.new(SESSION_SECRET, sid.encode(), hashlib.sha256).hexdigest()


def digest(sid):
    # the db holds only a hash, so a copied sgc.db doesn't hand out live sessions
    return hashlib.sha256(sid.encode()).hexdigest()


def new_session():
    sid = secrets.token_urlsafe(32)
    db.conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
    db.insert("INSERT INTO sessions(id, expires_at) VALUES(?,?)", digest(sid), time.time() + SESSION_TTL)
    return f"{sid}.{sign(sid)}"


def session_ok(cookie):
    sid, _, sig = (cookie or "").partition(".")
    return bool(sid) and hmac.compare_digest(sig.encode(), sign(sid).encode()) and \
        db.one("SELECT 1 AS ok FROM sessions WHERE id=? AND expires_at>?", digest(sid), time.time()) is not None


def client_ip(conn):
    # cloudflared connects from 127.0.0.1, so the real client is only in the header Cloudflare's edge sets
    return conn.headers.get("cf-connecting-ip") or (conn.client.host if conn.client else "?")


def is_local(conn):
    # Cloudflare always adds CF-Connecting-IP, so loopback without it didn't come through the tunnel
    return ALLOW_LOCAL and conn.client is not None and conn.client.host in ("127.0.0.1", "::1") \
        and "cf-connecting-ip" not in conn.headers


class Guard:
    """ASGI middleware: one gate for HTTP and the websocket handshake."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        ws = scope["type"] == "websocket"
        if (ws or scope["method"] not in ("GET", "HEAD")) and conn.headers.get("origin") not in ORIGINS:
            status, detail = 403, "bad origin"
        elif conn.url.path == "/login" or is_local(conn) or session_ok(conn.cookies.get(COOKIE)):
            return await self.app(scope, receive, send)
        else:
            status, detail = 401, "login required"
        if ws:
            return await send({"type": "websocket.close", "code": 1008})  # before accept: handshake gets 403
        if scope["method"] == "GET" and not conn.url.path.startswith("/api/") and status == 401:
            resp = RedirectResponse("/login", 303)
        else:
            resp = JSONResponse({"detail": detail}, status)
        await resp(scope, receive, send)


router = APIRouter()


def login_page(msg="", status=200):
    return HTMLResponse(LOGIN_HTML.replace("{{msg}}", html.escape(msg)), status)


@router.get("/login")
def login_form():
    return login_page()


@router.post("/login")
async def login(request: Request):
    ip = client_ip(request)
    form = parse_qs((await request.body())[:4096].decode(errors="replace"))
    pw, code = (form.get(k, [""])[0] for k in ("password", "code"))
    if locked_until.get(ip, 0) > time.time():
        log(f"failed login from {ip}: locked out")
        return login_page("Too many failed attempts. Try again in 15 minutes.", 429)
    if not (PASSWORD_HASH and TOTP_SECRET and check_password(pw) and check_totp(code.strip())):
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < LOCKOUT] + [now]
        if len(recent) >= MAX_FAILS:
            locked_until[ip], recent = now + LOCKOUT, []
        fails[ip] = recent
        log(f"failed login from {ip}" + (" -> locked out for 15 min" if not recent else ""))
        return login_page("Wrong password or code.", 401)
    fails.pop(ip, None)
    log(f"login from {ip}")
    resp = RedirectResponse("/", 303)
    resp.set_cookie(COOKIE, new_session(), max_age=SESSION_TTL, httponly=True, secure=True, samesite="strict")
    return resp


@router.post("/logout")
def logout(request: Request):
    # ponytail: websockets already open on this session stay open until they drop; the page leaves, so they do
    db.conn.execute("DELETE FROM sessions WHERE id=?", (digest(request.cookies.get(COOKIE, "").partition(".")[0]),))
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE, httponly=True, secure=True, samesite="strict")
    return resp
