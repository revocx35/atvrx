"""IQ sources: SpyServer, rtl_tcp and recorded 8-bit IQ files."""
from __future__ import annotations

import socket
import struct
import time
from pathlib import Path

import numpy as np


class SourceError(Exception):
    """A problem the user can act on (wrong address, no device, file missing)."""


# unsigned 8-bit sample -> float in -1..1
_LUT = ((np.arange(256, dtype=np.float32) - 127.5) / 127.5).astype(np.float32)


def u8_to_complex(buf) -> np.ndarray:
    """Interleaved unsigned 8-bit I/Q bytes -> complex64."""
    a = _LUT[np.frombuffer(buf, np.uint8)]
    return a[: len(a) & ~1].view(np.complex64)


class Source:
    kind = "base"
    can_tune = True

    def __init__(self) -> None:
        self.sample_rate = 0.0
        self.center_hz = 0.0
        self.gain_steps = 0
        self.description = ""
        self._pending: list[np.ndarray] = []
        self._pending_n = 0

    def open(self) -> None: ...
    def start(self) -> None: ...
    def tune(self, hz: float) -> None: ...
    def set_gain(self, step: int) -> None: ...
    def close(self) -> None: ...

    def _next_block(self) -> np.ndarray:
        raise NotImplementedError

    def read(self, n: int) -> np.ndarray:
        """Exactly n samples, blocking until they arrive."""
        while self._pending_n < n:
            b = self._next_block()
            self._pending.append(b)
            self._pending_n += len(b)
        data = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        out, rest = data[:n], data[n:]
        self._pending = [rest] if len(rest) else []
        self._pending_n = len(rest)
        return out

    def discard(self, seconds: float) -> None:
        self._pending, self._pending_n = [], 0
        self.read(max(1, int(seconds * self.sample_rate)))


class _TcpSource(Source):
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        super().__init__()
        self.host, self.port, self.timeout = host, int(port), timeout
        self.sock: socket.socket | None = None

    def _connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise SourceError(f"Cannot connect to {self.host}:{self.port} ({e.strerror or e})") from e
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _recv(self, n: int) -> bytearray:
        buf = bytearray(n)
        view, got = memoryview(buf), 0
        while got < n:
            try:
                k = self.sock.recv_into(view[got:], n - got)
            except socket.timeout as e:
                raise SourceError(f"{self.host}:{self.port} stopped sending data") from e
            if k == 0:
                raise SourceError(f"{self.host}:{self.port} closed the connection")
            got += k
        return buf

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


class SpyServerSource(_TcpSource):
    """Client for Airspy SPY Server (protocol 2.0.1700), streaming 8-bit IQ."""

    kind = "spyserver"
    PROTOCOL = (2 << 24) | (0 << 16) | 1700
    CMD_HELLO, CMD_SET = 0, 2
    SET_MODE, SET_ENABLED, SET_GAIN = 0, 1, 2
    SET_IQ_FORMAT, SET_IQ_FREQ, SET_IQ_DECIMATION = 100, 101, 102
    MSG_DEVICE_INFO, MSG_CLIENT_SYNC, MSG_UINT8_IQ = 0, 1, 100
    STREAM_IQ, FORMAT_UINT8 = 1, 1
    DEVICES = {1: "Airspy One", 2: "Airspy HF+", 3: "RTL-SDR"}

    def __init__(self, host: str, port: int = 5555, timeout: float = 5.0):
        super().__init__(host, port, timeout)
        self.device: dict | None = None
        self.sync: dict | None = None
        self._gain: int | None = None

    def _send(self, cmd: int, body: bytes) -> None:
        self.sock.sendall(struct.pack("<II", cmd, len(body)) + body)

    def _set(self, setting: int, value: int) -> None:
        self._send(self.CMD_SET, struct.pack("<II", setting, value))

    def _message(self):
        _proto, mtype, _stream, _seq, size = struct.unpack("<5I", self._recv(20))
        if size > 1 << 22:
            raise SourceError("SpyServer sent a malformed message")
        body = self._recv(size)
        t = mtype & 0xFFFF
        if t == self.MSG_DEVICE_INFO:
            f = struct.unpack("<12I", body[:48])
            self.device = dict(zip(("type", "serial", "max_rate", "max_bw", "decimation_stages", "gain_stages",
                                    "max_gain_index", "min_freq", "max_freq", "resolution", "min_iq_decimation",
                                    "forced_iq_format"), f))
        elif t == self.MSG_CLIENT_SYNC:
            f = struct.unpack("<9I", body[:36])
            self.sync = dict(zip(("can_control", "gain", "device_center", "iq_center", "fft_center",
                                  "min_iq_center", "max_iq_center", "min_fft_center", "max_fft_center"), f))
        return t, body

    def open(self) -> None:
        self._connect()
        self._send(self.CMD_HELLO, struct.pack("<I", self.PROTOCOL) + b"atvrx")
        deadline = time.monotonic() + self.timeout
        while self.device is None or self.sync is None:
            if time.monotonic() > deadline:
                raise SourceError("SpyServer did not answer the handshake")
            self._message()
        if self.device["type"] == 0:
            raise SourceError("SpyServer has no radio attached (is the dongle plugged in?)")
        if not self.sync["can_control"]:
            raise SourceError("SpyServer does not let this client tune. Another client may be connected, "
                              "or allow_control is off in spyserver.config")
        decim = self.device["min_iq_decimation"]
        self.sample_rate = self.device["max_rate"] / (1 << decim)
        self.gain_steps = self.device["max_gain_index"] + 1
        name = self.DEVICES.get(self.device["type"], "SDR")
        self.description = f"{name} via SpyServer {self.host}:{self.port}"
        self._set(self.SET_IQ_FORMAT, self.FORMAT_UINT8)
        self._set(self.SET_MODE, self.STREAM_IQ)
        self._set(self.SET_IQ_DECIMATION, decim)

    def tune(self, hz: float) -> None:
        lo, hi = self.sync["min_iq_center"], self.sync["max_iq_center"]
        if not lo <= hz <= hi:
            raise SourceError(f"{hz / 1e6:.3f} MHz is outside the radio's range "
                              f"({lo / 1e6:.0f}-{hi / 1e6:.0f} MHz)")
        self._set(self.SET_IQ_FREQ, int(round(hz)))
        self.center_hz = hz

    def set_gain(self, step: int) -> None:
        self._gain = int(max(0, min(step, self.gain_steps - 1)))
        self._set(self.SET_GAIN, self._gain)

    def start(self) -> None:
        self._set(self.SET_ENABLED, 1)
        # SpyServer wakes the radio when streaming starts and puts it at its configured initial
        # gain, yet still believes our earlier setting is in force, so it ignores the same value
        # sent again. Step to a neighbouring value and back to make it reach the radio.
        if self._gain is not None:
            self._set(self.SET_GAIN, self._gain - 1 if self._gain > 0 else self._gain + 1)
            self._set(self.SET_GAIN, self._gain)
        if self.center_hz:
            self._set(self.SET_IQ_FREQ, int(round(self.center_hz)))

    def _next_block(self) -> np.ndarray:
        while True:
            t, body = self._message()
            if t == self.MSG_UINT8_IQ and body:
                return u8_to_complex(body)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._set(self.SET_ENABLED, 0)
            except OSError:
                pass
        super().close()


class RtlTcpSource(_TcpSource):
    """Client for rtl_tcp servers."""

    kind = "rtltcp"
    TUNERS = {1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}

    def __init__(self, host: str, port: int = 1234, sample_rate: float = 2.4e6, timeout: float = 5.0):
        super().__init__(host, port, timeout)
        self.sample_rate = float(sample_rate)

    def _cmd(self, cmd: int, value: int) -> None:
        self.sock.sendall(struct.pack(">BI", cmd, int(value) & 0xFFFFFFFF))

    def open(self) -> None:
        self._connect()
        hdr = bytes(self._recv(12))
        if hdr[:4] != b"RTL0":
            raise SourceError(f"{self.host}:{self.port} is not an rtl_tcp server")
        tuner, ngain = struct.unpack(">II", hdr[4:])
        self.gain_steps = int(ngain)
        self.description = f"RTL-SDR ({self.TUNERS.get(tuner, 'unknown tuner')}) via rtl_tcp {self.host}:{self.port}"
        self._cmd(0x02, int(self.sample_rate))   # sample rate
        self._cmd(0x03, 1)                       # manual gain
        self._cmd(0x08, 0)                       # RTL AGC off

    def tune(self, hz: float) -> None:
        self._cmd(0x01, int(round(hz)))
        self.center_hz = hz

    def set_gain(self, step: int) -> None:
        self._cmd(0x0D, int(max(0, min(step, max(self.gain_steps - 1, 0)))))

    def _next_block(self) -> np.ndarray:
        return u8_to_complex(self._recv(65536))


class FileSource(Source):
    """Plays an unsigned 8-bit IQ recording (rtl_sdr format) in real time, looping."""

    kind = "file"
    can_tune = False

    def __init__(self, path: Path, sample_rate: float, center_hz: float, realtime: bool = True):
        super().__init__()
        self.path = Path(path)
        self.sample_rate = float(sample_rate)
        self.center_hz = float(center_hz)
        self.realtime = realtime
        self.gain_steps = 1
        self._data: np.ndarray | None = None
        self._pos = 0
        self._t0 = 0.0
        self._sent = 0

    def open(self) -> None:
        if not self.path.is_file():
            raise SourceError(f"Recording {self.path.name} not found")
        data = np.memmap(self.path, np.uint8, mode="r")
        self._data = data[: len(data) & ~1]               # keep I/Q pairs aligned when looping
        if len(self._data) < 2 * int(self.sample_rate * 0.1):
            raise SourceError(f"Recording {self.path.name} is shorter than 0.1 s")
        self.description = f"Recording {self.path.name}"

    def start(self) -> None:
        self._t0, self._sent = time.monotonic(), 0

    def _next_block(self) -> np.ndarray:
        n = 2 * 65536
        end = self._pos + n
        if end <= len(self._data):
            raw = self._data[self._pos:end]
            self._pos = end if end < len(self._data) - 1 else 0
        else:
            raw = np.concatenate([self._data[self._pos:], self._data[: end - len(self._data)]])
            self._pos = end - len(self._data)
        block = u8_to_complex(np.ascontiguousarray(raw))
        self._sent += len(block)
        if self.realtime:
            ahead = self._sent / self.sample_rate - (time.monotonic() - self._t0)
            if ahead > 0:
                time.sleep(ahead)
        return block

    def close(self) -> None:
        self._data = None
