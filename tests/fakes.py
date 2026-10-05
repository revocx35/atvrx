"""Stand-in SDR servers that 'receive' a synthetic TV transmitter."""
from __future__ import annotations

import select
import socket
import struct
import threading
import time

import numpy as np

import synth
from atvrx.dsp import STANDARDS

FS = 2.4e6


class Transmitter:
    """A still picture on a vision carrier at freq_hz, as seen by a receiver tuned anywhere."""

    _env_cache: dict = {}

    def __init__(self, freq_hz: float, cnr_db: float = 25.0, standard: str = "625"):
        self.freq_hz, self.cnr_db = freq_hz, cnr_db
        key = standard
        if key not in self._env_cache:
            std = STANDARDS[key]
            # a whole number of synthetic fields (312 or 262 lines each) so the loop has no timing jump
            seconds = 10 * int(std.lines_per_field) / std.line_rate
            self._env_cache[key] = synth.envelope(std, FS, seconds, synth.picture(std.active_lines, 384))
        self.env = self._env_cache[key]
        self.pos = 0
        self.phase = 0.0
        self.rng = np.random.default_rng(7)

    def block(self, center_hz: float, n: int) -> np.ndarray:
        idx = (self.pos + np.arange(n)) % len(self.env)
        self.pos = (self.pos + n) % len(self.env)
        off = self.freq_hz - center_hz
        if abs(off) < FS / 2:
            ph = self.phase + 2 * np.pi * off / FS * np.arange(n)
            self.phase = float((self.phase + 2 * np.pi * off / FS * n) % (2 * np.pi))
            x = self.env[idx] * np.exp(1j * ph)
        else:
            x = np.zeros(n, np.complex128)
        sigma = np.sqrt(synth.BLANK ** 2 / 10 ** (self.cnr_db / 10) * (FS / 2e6) / 2)
        x = x + self.rng.normal(0, sigma, n) + 1j * self.rng.normal(0, sigma, n)
        return x.astype(np.complex64)


class _Pacer:
    """Sends at the radio's real sample rate, as a real server does."""

    def __init__(self):
        self.t0, self.sent = None, 0

    def wait(self, n: int) -> None:
        if self.t0 is None:
            self.t0 = time.monotonic()
        self.sent += n
        ahead = self.sent / FS - (time.monotonic() - self.t0)
        if ahead > 0:
            time.sleep(ahead)


class _Server:
    def __init__(self, tx: Transmitter):
        self.tx = tx
        self.lsock = socket.socket()
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(4)
        self.port = self.lsock.getsockname()[1]
        self.commands: list[tuple[int, int]] = []
        self.center = 100e6
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([self.lsock], [], [], 0.1)
            if not r:
                continue
            conn, _ = self.lsock.accept()
            try:
                self._client(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self.lsock.close()
        self.thread.join(timeout=5)


class FakeSpyServer(_Server):
    def __init__(self, tx: Transmitter, device_type: int = 3, can_control: int = 1):
        self.device_type, self.can_control = device_type, can_control
        self.settings: dict[int, int] = {}
        self._requested_gain = 5
        super().__init__(tx)

    def _msg(self, conn, mtype: int, body: bytes) -> None:
        conn.sendall(struct.pack("<5I", 0x02000000 | 1700, mtype, 0, 0, len(body)) + body)

    def _client(self, conn) -> None:
        hdr = conn.recv(8)
        _cmd, size = struct.unpack("<II", hdr)
        conn.recv(size)
        info = (self.device_type, 0, int(FS), 2000000, 9, 0, 29, 24000000, 1800000000, 8, 0, 0)
        self._msg(conn, 0, struct.pack("<12I", *info))
        self._msg(conn, 1, struct.pack("<9I", self.can_control, 5, int(self.center), int(self.center), int(self.center),
                                       25000000, 1799000000, 25000000, 1799000000))
        buf = b""
        pace = _Pacer()
        while not self._stop.is_set():
            r, _, _ = select.select([conn], [], [], 0)
            if r:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                while len(buf) >= 8:
                    cmd, size = struct.unpack("<II", buf[:8])
                    if len(buf) < 8 + size:
                        break
                    body, buf = buf[8:8 + size], buf[8 + size:]
                    if cmd == 2:
                        k, v = struct.unpack("<II", body[:8])
                        self.commands.append((k, v))
                        # like SpyServer: a gain equal to the last one requested is ignored, and the
                        # radio wakes up at its initial gain when streaming starts
                        if k == 2 and v == self._requested_gain:
                            continue
                        if k == 2:
                            self._requested_gain = v
                        self.settings[k] = v
                        if k == 101:
                            self.center = float(v)
                        if k == 1 and v == 1:
                            self.settings[2] = 5
            if self.settings.get(1) == 1:
                x = self.tx.block(self.center, 32768)
                u8 = np.clip(np.round(np.stack([x.real, x.imag], 1).ravel() * 60 + 127.5), 0, 255).astype(np.uint8)
                self._msg(conn, 100, u8.tobytes())
                pace.wait(32768)
            else:
                select.select([conn], [], [], 0.05)


class FakeRtlTcp(_Server):
    def _client(self, conn) -> None:
        conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
        conn.setblocking(False)
        buf = b""
        pace = _Pacer()
        while not self._stop.is_set():
            try:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
            except BlockingIOError:
                pass
            while len(buf) >= 5:
                cmd, val = struct.unpack(">BI", buf[:5])
                buf = buf[5:]
                self.commands.append((cmd, val))
                if cmd == 1:
                    self.center = float(val)
            x = self.tx.block(self.center, 32768)
            u8 = np.clip(np.round(np.stack([x.real, x.imag], 1).ravel() * 60 + 127.5), 0, 255).astype(np.uint8)
            conn.setblocking(True)
            conn.sendall(u8.tobytes())
            conn.setblocking(False)
            pace.wait(32768)
