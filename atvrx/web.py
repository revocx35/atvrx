"""HTTP API, live-picture WebSocket and the browser UI."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, scan
from .dsp import STANDARDS
from .session import Config, Session
from .sources import SourceError

STATIC = Path(__file__).parent / "static"
RECORDINGS = Path(os.environ.get("ATVRX_RECORDINGS", "/recordings"))


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


def create_app(session: Session | None = None) -> FastAPI:
    app = FastAPI(title="ATV-RX", version=__version__)
    sess = session or Session(default_config(), RECORDINGS, float(os.environ.get("ATVRX_IDLE_RELEASE_S", "30")))
    app.state.session = sess

    def guarded(fn, *args):
        try:
            fn(*args)
        except (ValueError, SourceError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return sess.snapshot()

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
                "recordings": recordings()}

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
    def recordings():
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
        await ws.accept()
        sess.viewer_join()
        sent_seq, ticks = -1, 0
        try:
            while True:
                j = await asyncio.to_thread(sess.jpeg)
                if j is not None and j[0] != sent_seq:
                    sent_seq = j[0]
                    await ws.send_bytes(j[1])
                if ticks % 6 == 0:
                    await ws.send_json({"state": sess.snapshot(), "spectrum": sess.spectrum_snapshot()})
                ticks += 1
                await asyncio.sleep(0.04)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sess.viewer_leave()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
