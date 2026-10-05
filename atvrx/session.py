"""One radio, one job at a time: watching a channel or scanning for channels."""
from __future__ import annotations

import collections
import dataclasses
import io
import logging
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import scan
from .dsp import STANDARDS, Demodulator, FieldSlicer, carrier_offset_for, find_carrier
from .sources import FileSource, RtlTcpSource, SourceError, SpyServerSource

log = logging.getLogger("atvrx")

SOURCE_KEYS = {"source", "host", "port", "file", "file_rate", "file_center_mhz"}


@dataclasses.dataclass
class Config:
    source: str = "spyserver"      # spyserver | rtltcp | file
    host: str = "127.0.0.1"
    port: int = 5555
    file: str = ""                 # file name inside the recordings folder
    file_rate: float = 2.4e6
    file_center_mhz: float = 0.0
    freq_mhz: float = 487.25       # vision carrier
    gain: int = 29                 # gain step
    standard: str = "625"
    positive: bool = False         # positive modulation (System L)
    average: int = 2               # fields averaged per frame
    h_smooth: int = 9              # lines the horizontal lock is smoothed over
    v_shift: int = 0               # move the picture up/down by whole lines
    afc: bool = True               # follow carrier drift


def frame_image(frame: np.ndarray, width: int = 768) -> Image.Image:
    img = Image.fromarray((np.clip(frame, 0, 1) * 255).astype(np.uint8))
    return img.resize((width, width * 3 // 4), Image.BILINEAR)


class Session:
    def __init__(self, defaults: Config, recordings: Path, idle_release_s: float = 30.0):
        self.cfg = defaults
        self.recordings = recordings
        self.idle_release_s = idle_release_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: dict = {}
        self.state = "idle"            # idle | connecting | watching | scanning | error
        self.message = ""
        self.device = ""
        self.gain_steps = 30
        self.stats: dict = {}
        self.spectrum: dict | None = None
        self.scan_state = {"running": False, "progress": 0.0, "plan": "", "results": []}
        self._frame: np.ndarray | None = None
        self.frame_seq = 0
        self._jpeg: tuple[int, bytes] | None = None
        self.viewers = 0
        self._viewer_seen = time.monotonic()
        self._streamed = False

    # -- public API (called from the web layer) ------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "message": self.message, "device": self.device,
                    "gain_steps": self.gain_steps,
                    "config": dataclasses.asdict(self.cfg), "stats": dict(self.stats),
                    "scan": {**self.scan_state, "results": list(self.scan_state["results"])},
                    "frame_seq": self.frame_seq, "viewers": self.viewers}

    def apply(self, changes: dict) -> None:
        """Change settings. Tuning and picture settings apply live; source changes reconnect."""
        changes = {k: v for k, v in changes.items() if k in Config.__dataclass_fields__}
        cfg = dataclasses.replace(self.cfg, **changes)
        _validate(cfg, self.recordings)
        reconnect = any(getattr(cfg, k) != getattr(self.cfg, k) for k in SOURCE_KEYS)
        with self._lock:
            self.cfg = cfg
            self._pending.update(changes)
        if reconnect and self.state in ("watching", "connecting"):
            self.watch()

    def watch(self, changes: dict | None = None) -> None:
        if changes:
            cfg = dataclasses.replace(self.cfg, **{k: v for k, v in changes.items() if k in Config.__dataclass_fields__})
            _validate(cfg, self.recordings)
            self.cfg = cfg
        self._run(self._watch_loop)

    def start_scan(self, plan: str, start_mhz: float = 0, stop_mhz: float = 0) -> None:
        if plan not in scan.PLANS and plan != "range":
            raise ValueError("Unknown channel plan")
        if plan == "range" and not (0 < start_mhz < stop_mhz):
            raise ValueError("The range must end above where it starts")
        self._run(lambda: self._scan_loop(plan, start_mhz, stop_mhz), scan_plan=plan)

    def stop(self, message: str = "") -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=10)
        with self._lock:
            self.state, self.message = "idle", message
            self.scan_state["running"] = False

    def viewer_join(self) -> None:
        with self._lock:
            self.viewers += 1
            self._viewer_seen = time.monotonic()

    def viewer_leave(self) -> None:
        with self._lock:
            self.viewers = max(0, self.viewers - 1)
            self._viewer_seen = time.monotonic()

    def jpeg(self) -> tuple[int, bytes] | None:
        """Latest frame as JPEG, encoded once per new frame."""
        with self._lock:
            frame, seq, cached = self._frame, self.frame_seq, self._jpeg
        if frame is None:
            return None
        if cached and cached[0] == seq:
            return cached
        buf = io.BytesIO()
        frame_image(frame, 640).save(buf, "JPEG", quality=82)
        out = (seq, buf.getvalue())
        with self._lock:
            self._jpeg = out
        return out

    def png(self) -> bytes | None:
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        buf = io.BytesIO()
        frame_image(frame, 768).save(buf, "PNG")
        return buf.getvalue()

    def spectrum_snapshot(self) -> dict | None:
        with self._lock:
            return self.spectrum

    # -- worker threads ------------------------------------------------------------------
    def _run(self, target, scan_plan: str | None = None) -> None:
        self.stop()
        self._stop = threading.Event()
        with self._lock:
            self._pending = {}
            self.state, self.message, self.stats = "connecting", "", {}
            self._frame, self._viewer_seen = None, time.monotonic()
            if scan_plan is not None:
                self.scan_state = {"running": True, "progress": 0.0, "plan": scan_plan, "results": []}
        self._thread = threading.Thread(target=self._guard, args=(target, self._stop), daemon=True)
        self._thread.start()

    def _guard(self, target, stop: threading.Event) -> None:
        try:
            target()
        except SourceError as e:
            self._fail(str(e), stop)
        except Exception as e:  # keep the server alive and tell the user what broke
            log.exception("worker failed")
            self._fail(f"Unexpected error: {e}", stop)

    def _fail(self, msg: str, stop: threading.Event) -> None:
        if stop.is_set():
            return
        with self._lock:
            self.state, self.message = "error", msg
            self.scan_state["running"] = False

    def _open(self, cfg: Config):
        if cfg.source == "spyserver":
            src = SpyServerSource(cfg.host, cfg.port)
        elif cfg.source == "rtltcp":
            src = RtlTcpSource(cfg.host, cfg.port)
        else:
            src = FileSource(_recording(self.recordings, cfg.file), cfg.file_rate, cfg.file_center_mhz * 1e6)
        src.open()
        with self._lock:
            self.device = src.description
            self.gain_steps = max(1, src.gain_steps)
        return src

    def _tune(self, src, freq_hz: float) -> float:
        """Tune so the vision carrier lands where the decoder wants it; return its offset from centre."""
        if src.can_tune:
            src.tune(freq_hz - carrier_offset_for(src.sample_rate))
        return freq_hz - src.center_hz

    def _watch_loop(self) -> None:
        """Watch, and reconnect if the radio drops out while someone is looking."""
        stop = self._stop
        while True:
            self._streamed = False
            try:
                self._watch_once(stop)
                return
            except SourceError as e:
                with self._lock:
                    retry = self._streamed and self.viewers > 0 and not stop.is_set()
                    if retry:
                        self.state, self.message = "connecting", f"{e}. Reconnecting in 5 s."
                if not retry:
                    raise
                if stop.wait(5):
                    return

    def _watch_once(self, stop: threading.Event) -> None:
        cfg = self.cfg
        src = self._open(cfg)
        try:
            fs = src.sample_rate
            nominal = self._tune(src, cfg.freq_mhz * 1e6)
            src.set_gain(cfg.gain)
            src.start()
            src.discard(0.25)
            demod, slicer, carrier, cnr = self._decoder(src, cfg, nominal)
            with self._lock:
                self.state, self.message = "watching", ""
            self._streamed = True
            history: collections.deque = collections.deque(maxlen=16)
            chunk = int(fs * 0.04)
            t_spec = t_afc = t_rate = 0.0
            n_fields = n_locked = 0
            rate = 0.0
            last_field = time.monotonic()
            while not stop.is_set():
                with self._lock:
                    pending, self._pending = self._pending, {}
                    cfg = self.cfg
                if pending:
                    if {"freq_mhz", "standard", "positive", "h_smooth", "v_shift"} & pending.keys():
                        if "freq_mhz" in pending:
                            nominal = self._tune(src, cfg.freq_mhz * 1e6)
                            src.discard(0.15)
                            demod, slicer, carrier, cnr = self._decoder(src, cfg, nominal)
                        else:
                            slicer = self._slicer(fs, cfg)
                        history.clear()
                    if "gain" in pending:
                        src.set_gain(cfg.gain)
                x = src.read(chunk)
                now = time.monotonic()
                if now - t_spec > 0.25:
                    self._publish_spectrum(x, fs, src.center_hz, carrier)
                    t_spec = now
                if cfg.afc and now - t_afc > 1.0:
                    f, c = find_carrier(x, fs, carrier, search_hz=30e3)
                    if np.isfinite(c) and c > 3:
                        carrier += 0.3 * (f - carrier)
                        demod.retune(carrier)
                    cnr, t_afc = c, now
                for fl in slicer.feed(demod.process(x)):
                    history.append(fl.image)
                    n_fields += 1
                    n_locked += fl.vlocked
                    last_field = now
                    frame = np.mean(list(history)[-max(1, cfg.average):], axis=0)
                    with self._lock:
                        self._frame = frame
                        self.frame_seq += 1
                        self.stats.update(hlock=round(fl.hlock * 100), vlocked=bool(fl.vlocked),
                                          sync_snr_db=round(fl.sync_snr_db, 1) if fl.vlocked else None)
                if now - t_rate >= 1.0:
                    rate = n_fields / (now - t_rate) if t_rate else 0.0
                    locked_share = n_locked / n_fields if n_fields else 0.0
                    n_fields = n_locked = 0
                    t_rate = now
                    with self._lock:
                        self.stats.update(fields_per_s=round(rate, 1), vlock=round(locked_share * 100),
                                          carrier_mhz=round((src.center_hz + carrier) / 1e6, 5),
                                          cnr_db=round(cnr, 1), signal=now - last_field < 0.5)
                with self._lock:
                    idle = self.viewers == 0 and now - self._viewer_seen > self.idle_release_s
                if idle:
                    with self._lock:
                        self.state, self.message = "idle", "Released the radio because nobody was watching."
                    return
        finally:
            src.close()

    def _slicer(self, fs: float, cfg: Config) -> FieldSlicer:
        return FieldSlicer(fs, STANDARDS[cfg.standard], width=384, h_smooth=cfg.h_smooth,
                           positive=cfg.positive, v_shift=cfg.v_shift)

    def _decoder(self, src, cfg: Config, nominal: float):
        """Find the carrier near where it should be and build a fresh decoder for it."""
        fs = src.sample_rate
        x = src.read(1 << 16)
        carrier, cnr = (find_carrier(x, fs, nominal, search_hz=150e3) if cfg.afc
                        else (nominal, find_carrier(x, fs, nominal, search_hz=2e3)[1]))
        return Demodulator(fs, carrier), self._slicer(fs, cfg), carrier, cnr

    def _publish_spectrum(self, x: np.ndarray, fs: float, center: float, carrier: float) -> None:
        n = 1024
        seg = x[: (len(x) // n) * n].reshape(-1, n) * np.hanning(n).astype(np.float32)
        p = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(0))
        db = 10 * np.log10(p + 1e-12)
        db = db.reshape(-1, 2).max(1)                          # 512 points
        with self._lock:
            self.spectrum = {"center_mhz": center / 1e6, "span_mhz": fs / 1e6, "carrier_mhz": (center + carrier) / 1e6,
                             "db": np.round(db - db.max(), 1).tolist()}

    def _scan_loop(self, plan: str, start_mhz: float, stop_mhz: float) -> None:
        stop = self._stop
        cfg = self.cfg
        src = self._open(cfg)
        try:
            fs = src.sample_rate
            src.set_gain(cfg.gain)
            src.start()
            centers = scan.targets(plan, fs, start_mhz, stop_mhz) if src.can_tune else [src.center_hz]
            with self._lock:
                self.state, self.message = "scanning", ""
            results: list[dict] = []
            for i, c in enumerate(centers):
                if stop.is_set():
                    return
                if src.can_tune:
                    src.tune(c)
                    src.discard(0.12)
                x = src.read(int(0.12 * fs))
                results = scan.merge(results, scan.analyse(x, fs, src.center_hz))
                with self._lock:
                    self.scan_state.update(progress=(i + 1) / len(centers), results=list(results))
            with self._lock:
                self.scan_state["running"] = False
                found = sum(r["video"] for r in results)
                self.state = "idle"
                self.message = f"Scan finished: {found} TV picture carrier{'s' if found != 1 else ''} found."
        finally:
            src.close()


def _recording(folder: Path, name: str) -> Path:
    p = (folder / name).resolve()
    if not name or p.parent != folder.resolve():
        raise SourceError("Pick a recording from the list")
    return p


def _validate(cfg: Config, recordings: Path) -> None:
    if cfg.source not in ("spyserver", "rtltcp", "file"):
        raise ValueError("Unknown source type")
    if cfg.standard not in STANDARDS:
        raise ValueError("Unknown TV standard")
    if not 1 <= int(cfg.port) <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not 1 <= cfg.freq_mhz <= 6000:
        raise ValueError("Frequency must be between 1 and 6000 MHz")
    if not 0 <= int(cfg.gain) <= 100:
        raise ValueError("Gain step must be between 0 and 100")
    if not 1 <= int(cfg.average) <= 16:
        raise ValueError("Averaging must be between 1 and 16 fields")
    if not 1 <= int(cfg.h_smooth) <= 51:
        raise ValueError("Horizontal smoothing must be between 1 and 51 lines")
    if not -40 <= int(cfg.v_shift) <= 40:
        raise ValueError("Vertical shift must be between -40 and 40 lines")
    if cfg.source == "file":
        if not 0.25e6 <= cfg.file_rate <= 20e6:
            raise ValueError("Recording sample rate must be between 0.25 and 20 MS/s")
        if cfg.file:
            _recording(recordings, cfg.file)
