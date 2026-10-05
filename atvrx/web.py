"""HTTP API, live-picture WebSocket, sign-in and the browser UI."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, scan
from .auth import AccountError, LoginThrottle, Sessions, UserStore
from .dsp import STANDARDS
from .session import Config, Session
from .sources import SourceError

log = logging.getLogger("atvrx")
STATIC = Path(__file__).parent / "static"
COOKIE = "atvrx_session"
PUBLIC = {"/login", "/api/login", "/api/login-info", "/healthz", "/static/login.js", "/static/style.css"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


PRIVATE_NETWORKS = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1,fc00::/7"


def trusted_proxies() -> str:
    """Addresses whose X-Forwarded-For/-Proto are believed: "private" (default), "none" or a list of IPs/CIDRs."""
    value = os.environ.get("ATVRX_TRUSTED_PROXIES", "private").strip()
    return {"private": PRIVATE_NETWORKS, "none": ""}.get(value.lower(), value)


def default_config() -> Config:
    env = os.environ.get
    return Config(
        source=env("ATVRX_SOURCE", "spyserver"),
        host=env("ATVRX_HOST", "127.0.0.1"),
        port=int(env("ATVRX_PORT_SDR", "5555")),
        freq_mhz=float(env("ATVRX_FREQ_MHZ", "487.25")),
        gain=int(env("ATVRX_GAIN", "29")),
        standard=env("ATVRX_STANDARD", "625"),
    )


class Settings(BaseModel):
    source: str | None = None
    host: str | None = Field(None, max_length=255)
    port: int | None = None
    file: str | None = Field(None, max_length=255)
    file_rate: float | None = None
    file_center_mhz: float | None = None
    freq_mhz: float | None = None
    gain: int | None = None
    standard: str | None = None
    positive: bool | None = None
    average: int | None = None
    h_smooth: int | None = None
    v_shift: int | None = None
    afc: bool | None = None

    def changes(self) -> dict:
        return self.model_dump(exclude_none=True)


class ScanRequest(BaseModel):
    plan: str = "e-uhf"
    start_mhz: float = 0
    stop_mhz: float = 0


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


def bootstrap_admin(users: UserStore) -> None:
    """Create the account named in ATVRX_ADMIN_USER / ATVRX_ADMIN_PASSWORD if it does not exist yet."""
    name, pw = os.environ.get("ATVRX_ADMIN_USER"), os.environ.get("ATVRX_ADMIN_PASSWORD")
    if name and pw and name not in users.names():
        users.add(name, pw)
        log.info("created account %s from ATVRX_ADMIN_USER", name)


def create_app(session: Session | None = None, users: UserStore | None = None,
               throttle: LoginThrottle | None = None, sessions: Sessions | None = None) -> FastAPI:
    data = Path(os.environ.get("ATVRX_DATA", "/data"))
    recordings = Path(os.environ.get("ATVRX_RECORDINGS", "/recordings"))
    sess = session or Session(default_config(), recordings, float(os.environ.get("ATVRX_IDLE_RELEASE_S", "30")))
    if users is None:
        users = UserStore(data / "users.json")
        bootstrap_admin(users)
    throttle = throttle or LoginThrottle()
    sessions = sessions or Sessions(users, max_age_s=float(os.environ.get("ATVRX_SESSION_DAYS", "7")) * 86400)
    secure_mode = os.environ.get("ATVRX_SECURE_COOKIES", "auto").lower()

    app = FastAPI(title="ATV-RX", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session, app.state.users, app.state.sessions = sess, users, sessions

    def secure(request: Request) -> bool:
        return secure_mode == "true" or (secure_mode == "auto" and request.url.scheme == "https")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in UNSAFE:
            # browsers cannot add a custom header to a cross-site request without CORS, which this app never allows
            site = request.headers.get("sec-fetch-site", "same-origin")
            if request.headers.get("x-atvrx") != "1" or site not in ("same-origin", "none"):
                return JSONResponse({"detail": "Request refused."}, status_code=403)
        path = request.url.path
        if path not in PUBLIC:
            user = sessions.user(request.cookies.get(COOKIE))
            if user is None:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Sign in first."}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            request.state.user = user
        resp = await call_next(request)
        host = request.headers.get("host", "")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
            f"connect-src 'self' ws://{host} wss://{host}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def guarded(fn, *args):
        try:
            fn(*args)
        except (ValueError, SourceError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return sess.snapshot()

    # -- sign-in -------------------------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if sessions.user(request.cookies.get(COOKIE)):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC / "login.html")

    @app.get("/api/login-info")
    def login_info():
        return {"has_users": bool(users.names())}

    def start_session(request: Request, resp: Response, user: str) -> None:
        resp.set_cookie(COOKIE, sessions.create(user), max_age=int(sessions.max_age), httponly=True,
                        samesite="strict", secure=secure(request), path="/")

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        wait = throttle.retry_after(ip, body.username)
        if wait > 0:
            minutes = max(1, math.ceil(wait / 60))
            return JSONResponse({"detail": f"Too many failed sign-ins. Try again in {minutes} minute"
                                           f"{'s' if minutes != 1 else ''}."},
                                status_code=429, headers={"Retry-After": str(math.ceil(wait))})
        if not users.verify(body.username, body.password):
            throttle.failed(ip, body.username)
            log.warning("failed sign-in for %r from %s", body.username[:64], ip)
            return JSONResponse({"detail": "Wrong username or password."}, status_code=401)
        throttle.succeeded(ip, body.username)
        start_session(request, response, body.username)
        return {"user": body.username}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.end(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=secure(request))
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return {"user": request.state.user}

    @app.post("/api/password")
    def change_password(body: PasswordChange, request: Request, response: Response):
        user, ip = request.state.user, request.client.host if request.client else "?"
        if throttle.retry_after(ip, user) > 0:
            return JSONResponse({"detail": "Too many failed attempts. Try again later."}, status_code=429)
        if not users.verify(user, body.current):
            throttle.failed(ip, user)
            return JSONResponse({"detail": "The current password is wrong."}, status_code=400)
        try:
            users.set_password(user, body.new)
        except AccountError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        start_session(request, response, user)       # every other session of this account is now signed out
        return {"ok": True}

    # -- the app -------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        return sess.snapshot()

    @app.get("/api/options")
    def options():
        return {"standards": {k: s.label for k, s in STANDARDS.items()},
                "plans": {k: v[0] for k, v in scan.PLANS.items()},
                "recordings": recordings_list()}

    @app.post("/api/watch")
    def watch(body: Settings):
        return guarded(sess.watch, body.changes())

    @app.post("/api/settings")
    def settings(body: Settings):
        return guarded(sess.apply, body.changes())

    @app.post("/api/stop")
    def stop():
        sess.stop("Stopped.")
        return sess.snapshot()

    @app.post("/api/scan")
    def start_scan(body: ScanRequest):
        return guarded(sess.start_scan, body.plan, body.start_mhz, body.stop_mhz)

    @app.get("/api/recordings")
    def recordings_list():
        folder = sess.recordings
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".u8", ".cu8", ".raw", ".bin"))

    @app.get("/api/snapshot.png")
    def snapshot_png():
        png = sess.png()
        if png is None:
            raise HTTPException(status_code=404, detail="No picture yet")
        return Response(png, media_type="image/png",
                        headers={"Content-Disposition": 'attachment; filename="atvrx-snapshot.png"'})

    @app.websocket("/ws")
    async def live(ws: WebSocket):
        # Chromium sends no Sec-Fetch-Site on WebSocket handshakes, so check Origin against Host
        origin, host = ws.headers.get("origin"), ws.headers.get("host")
        token = ws.cookies.get(COOKIE)
        if not origin or urlsplit(origin).netloc != host or sessions.user(token) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        sess.viewer_join()
        sent_seq, ticks = -1, 0
        try:
            while True:
                if ticks % 6 == 0:
                    if sessions.user(token) is None:      # signed out or password changed
                        await ws.close(code=4401)
                        return
                    await ws.send_json({"state": sess.snapshot(), "spectrum": sess.spectrum_snapshot()})
                j = await asyncio.to_thread(sess.jpeg)
                if j is not None and j[0] != sent_seq:
                    sent_seq = j[0]
                    await ws.send_bytes(j[1])
                ticks += 1
                await asyncio.sleep(0.04)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sess.viewer_leave()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
