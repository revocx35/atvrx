"""Synthetic analog TV transmissions for tests: sync pulses, a test picture, AM, noise."""
from __future__ import annotations

import numpy as np

from atvrx.dsp import Standard

# carrier amplitude for negative modulation (System B/G): sync 100 %, blanking 75 %, white 12.5 %
SYNC, BLANK, WHITE = 1.0, 0.75, 0.125


def picture(rows: int = 288, cols: int = 384) -> np.ndarray:
    """Grey bars, a white box, a black disc and a thin-stripe block; 0 = black, 1 = white."""
    y, x = np.mgrid[0:rows, 0:cols]
    img = np.floor(x / cols * 6) / 5.0
    img[(y > rows * 0.15) & (y < rows * 0.45) & (x > cols * 0.1) & (x < cols * 0.4)] = 1.0
    img[(y - rows * 0.65) ** 2 + (x - cols * 0.7) ** 2 < (rows * 0.18) ** 2] = 0.0
    stripes = (y > rows * 0.75) & (x < cols * 0.45)
    img[stripes] = ((y[stripes] // 8) % 2).astype(float)
    return img.astype(np.float32)


def envelope(std: Standard, fs: float, seconds: float, picture: np.ndarray, *,
             jumps: dict[float, float] | None = None, rate_ppm: float = 0.0, positive: bool = False) -> np.ndarray:
    """Carrier amplitude over time for a still picture.

    jumps maps a time in seconds to a timing step in microseconds, to imitate a
    source whose timing jumps (as some cable modulators do).
    """
    n = int(seconds * fs)
    t = np.arange(n) / fs * (1 + rate_ppm * 1e-6)
    for at, step_us in (jumps or {}).items():
        t = t + np.where(t >= at, step_us * 1e-6, 0.0)
    line_f = t * std.line_rate
    line_abs = np.floor(line_f).astype(np.int64)
    tau = (line_f - line_abs) / std.line_rate * 1e6            # microseconds into the line
    nl = int(std.lines_per_field)
    line = line_abs % nl                                       # line inside the field, 0-based
    lvl = np.full(n, BLANK)
    sync = std.sync_us
    # field structure: broad pulses on lines 0-2, equalising on 3-4 and the last two lines
    broad = line <= 2
    equal = (line >= 3) & (line <= 4) | (line >= nl - 2)
    normal = ~(broad | equal)
    half = 1e6 / std.line_rate / 2
    th = np.mod(tau, half)
    lvl[broad & (th < half - sync)] = SYNC
    lvl[equal & (th < sync / 2)] = SYNC
    lvl[normal & (tau < sync)] = SYNC
    first = std.first_active_line
    rows, cols = picture.shape
    act = normal & (line >= first) & (line < first + rows) & (tau >= std.active_start_us) \
        & (tau < std.active_start_us + std.active_us)
    r = (line[act] - first).clip(0, rows - 1)
    c = ((tau[act] - std.active_start_us) / std.active_us * cols).astype(np.int64).clip(0, cols - 1)
    lvl[act] = BLANK - picture[r, c] * (BLANK - WHITE)
    if positive:
        lvl = 1.0 - lvl + WHITE
    return lvl.astype(np.float32)


def iq(env: np.ndarray, fs: float, carrier_hz: float, cnr_db: float, seed: int = 1) -> np.ndarray:
    """AM onto a carrier at carrier_hz (relative to the IQ centre) plus white noise.

    cnr_db is the carrier-to-noise ratio in a 2 MHz bandwidth.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(len(env)) / fs
    x = env * np.exp(2j * np.pi * carrier_hz * t + 0.4j)
    p_carrier = BLANK ** 2                                      # mean-ish carrier power
    noise_total = p_carrier / 10 ** (cnr_db / 10) * (fs / 2e6)
    x = x + rng.normal(0, np.sqrt(noise_total / 2), len(x)) + 1j * rng.normal(0, np.sqrt(noise_total / 2), len(x))
    return x.astype(np.complex64)


def to_u8(x: np.ndarray, scale: float = 40.0) -> bytes:
    """Interleaved unsigned 8-bit IQ, as RTL-SDR tools produce."""
    out = np.empty(2 * len(x), np.float32)
    out[0::2], out[1::2] = x.real * scale + 127.5, x.imag * scale + 127.5
    return np.clip(np.round(out), 0, 255).astype(np.uint8).tobytes()
