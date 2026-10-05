"""Analog TV picture recovery: vision-carrier demodulation and field slicing.

The input is complex IQ with the vision carrier somewhere inside the captured
band. The output is a stream of greyscale fields (float32, 0 = black, 1 = white).
Colour is not decoded: the colour subcarrier sits 3.58-4.43 MHz above the vision
carrier, outside what an RTL-SDR captures at once.
"""
from __future__ import annotations

import dataclasses

import numpy as np
from scipy import ndimage, signal


@dataclasses.dataclass(frozen=True)
class Standard:
    key: str
    label: str
    line_rate: float          # Hz
    lines_per_field: float
    sync_us: float            # horizontal sync pulse width
    active_start_us: float    # from the sync leading edge to the first visible pixel
    active_us: float          # visible part of a line
    first_active_line: int    # lines between the start of the vertical sync and the first visible line
    active_lines: int         # visible lines per field


STANDARDS = {
    "625": Standard("625", "625 lines / 50 Hz (PAL, SECAM)", 15625.0, 312.5, 4.7, 10.5, 52.0, 22, 288),
    "525": Standard("525", "525 lines / 60 Hz (NTSC)", 15734.264, 262.5, 4.7, 10.9, 52.6, 17, 240),
}


def carrier_offset_for(sample_rate: float) -> float:
    """Where to put the vision carrier relative to the tuned centre.

    The video sits almost entirely above the vision carrier (vestigial sideband),
    so the carrier goes near the bottom of the band, leaving 0.3 MHz below it.
    """
    return -(sample_rate / 2 - 0.3e6)


def find_carrier(iq: np.ndarray, sample_rate: float, near_hz: float, search_hz: float = 150e3):
    """Strongest spectral line within +-search_hz of near_hz.

    Returns (frequency relative to the IQ centre, carrier-to-noise ratio in dB
    referred to a 2 MHz video bandwidth).
    """
    n = min(len(iq), 1 << 16)
    x = iq[:n] * np.hanning(n).astype(np.float32)
    spec = np.abs(np.fft.fftshift(np.fft.fft(x))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / sample_rate))
    win = np.abs(freqs - near_hz) <= search_hz
    if not win.any():
        return near_hz, float("-inf")
    k = np.flatnonzero(win)[np.argmax(spec[win])]
    # parabolic refinement of the peak bin
    if 0 < k < n - 1:
        a, b, c = np.log(spec[k - 1] + 1e-12), np.log(spec[k] + 1e-12), np.log(spec[k + 1] + 1e-12)
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    else:
        d = 0.0
    f = freqs[k] + d * sample_rate / n
    noise_per_bin = np.percentile(spec, 20)
    bins_in_2mhz = 2e6 / (sample_rate / n)
    cnr = 10 * np.log10(spec[k] / (noise_per_bin * bins_in_2mhz + 1e-12))
    return float(f), float(cnr)


class Demodulator:
    """Moves the vision carrier to 0 Hz, keeps the video band and returns the AM envelope."""

    def __init__(self, sample_rate: float, carrier_hz: float, ntaps: int = 97):
        self.fs = sample_rate
        self.carrier_hz = carrier_hz              # carrier relative to the IQ centre
        lo = max(-0.3e6, -(sample_rate / 2 + carrier_hz) + 0.02 * sample_rate)
        hi = min(5.0e6, sample_rate / 2 - carrier_hz - 0.04 * sample_rate)
        taps = signal.firwin(ntaps, (hi - lo) / 2, fs=sample_rate)
        mid = (hi + lo) / 2
        self.taps = (taps * np.exp(2j * np.pi * mid * np.arange(ntaps) / sample_rate)).astype(np.complex64)
        self.band = (lo, hi)
        self._phase = 0.0
        self._tail = np.zeros(ntaps - 1, np.complex64)

    def retune(self, carrier_hz: float) -> None:
        self.carrier_hz = carrier_hz

    def mix(self, iq: np.ndarray) -> np.ndarray:
        step = -2 * np.pi * self.carrier_hz / self.fs
        ph = self._phase + step * np.arange(len(iq))
        self._phase = float((self._phase + step * len(iq)) % (2 * np.pi))
        return iq * np.exp(1j * ph).astype(np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._tail, self.mix(iq)])
        self._tail = x[-(len(self.taps) - 1):]
        y = signal.oaconvolve(x, self.taps, mode="valid")
        return np.abs(y).astype(np.float32)


@dataclasses.dataclass
class Field:
    image: np.ndarray        # (active_lines, width) float32, 0..1
    vlocked: bool            # vertical sync found (False = flywheel)
    hlock: float             # share of lines whose sync matched the smoothed timing
    sync_snr_db: float


def _moving_mean(x: np.ndarray, w: int) -> np.ndarray:
    """Centred moving average, same length as x."""
    return ndimage.uniform_filter1d(x, int(w), mode="nearest")


class FieldSlicer:
    """Cuts an envelope stream into fields and lines.

    Every field is locked to its own vertical sync, and every line to its own
    horizontal sync, smoothed over a few lines. This copes with sources whose
    timing jumps between fields or in the middle of one.
    """

    def __init__(self, sample_rate: float, std: Standard, width: int = 384, h_smooth: int = 9,
                 positive: bool = False, v_shift: int = 0):
        self.fs = sample_rate
        self.std = std
        self.width = width
        self.h_smooth = max(1, int(h_smooth))
        self.sign = -1.0 if positive else 1.0     # make sync tips the maximum in both polarities
        self.v_shift = int(v_shift)
        self.spl = sample_rate / std.line_rate
        self.field_len = self.spl * std.lines_per_field
        self.sync_w = max(3, int(round(std.sync_us * 1e-6 * sample_rate)))
        self.nlines = int(std.lines_per_field)
        self._buf = np.zeros(0, np.float32)
        self._expect: float | None = None          # expected vsync position in _buf
        self._misses = 0
        self._levels: dict[str, float] | None = None

    # -- vertical sync -------------------------------------------------------------
    def _vsync(self, la: np.ndarray, mf: np.ndarray, lo: int, hi: int):
        """Start of the broad-pulse region inside la[lo:hi], or None.

        During vertical sync the carrier stays at sync level for most of each
        line, so the line average climbs close to the sync-tip level. Picture
        lines cannot get there, because their sync pulse is only 7 % of the line.
        """
        lo, hi = max(lo, 1), min(hi, len(la) - 1)
        if hi - lo < self.spl * 4:
            return None
        seg = la[lo:hi]
        p = int(np.argmax(seg))
        top = float(seg[p])
        # reference levels come from ~100 lines around the search window, minus the sync region itself
        guard, ctx = int(10 * self.spl), int(50 * self.spl)
        a, b = max(lo - ctx, 0), min(hi + ctx, len(la))
        c = lo + p
        others = np.concatenate([la[a:max(c - guard, a)], la[min(c + guard, b):b]])
        if len(others) < self.spl * 20:
            return None
        tip = float(np.percentile(mf[a:b], 99.5))         # sync-tip level from the line syncs
        mid = float(np.median(others))
        ref = float(np.percentile(others, 90))            # brightest-carrier ordinary lines
        if top < mid + 0.7 * (tip - mid) or top - ref < 0.25 * (tip - mid):
            return None
        thr = 0.5 * (top + ref)
        i = p
        while i > 0 and seg[i] > thr:
            i -= 1
        j = p
        while j < len(seg) - 1 and seg[j] > thr:
            j += 1
        if j - i < 1.5 * self.spl:                        # vertical sync lasts 2.5-3 lines; noise spikes don't
            return None
        a, b = float(seg[i]), float(seg[min(i + 1, len(seg) - 1)])
        frac = (thr - a) / (b - a) if b != a else 0.0
        # the line-long moving average is centred, so its half-way crossing marks where the region starts
        return lo + i + frac

    # -- one field ----------------------------------------------------------------
    def _slice(self, s: np.ndarray, mf: np.ndarray, v: float, vlocked: bool) -> Field:
        std, spl, n = self.std, self.spl, len(s)
        base = v + np.arange(self.nlines) * spl
        first = std.first_active_line + self.v_shift
        rows = slice(max(first, 0), min(first + std.active_lines, self.nlines))
        # dominant sync phase of this field: fold the sync matched filter over its lines
        span = np.arange(int(spl))
        idx = np.clip((base[rows][:, None] + span[None, :]).astype(np.int64), 0, n - 1)
        phase = int(np.argmax(mf[idx].mean(0)))
        base = base + phase
        # per-line sync search within half a line, then circular smoothing
        half = int(spl // 2) - 1
        win = np.arange(-half, half + 1)
        idx = np.clip((np.round(base)[:, None] + win[None, :]).astype(np.int64), 0, n - 1)
        raw = win[np.argmax(mf[idx], axis=1)].astype(np.float64)
        z = np.exp(2j * np.pi * raw / spl)
        k = self.h_smooth
        zs = np.convolve(np.pad(z, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")
        off = np.angle(zs) * spl / (2 * np.pi)
        hlock = float(np.mean(np.abs(raw[rows] - off[rows]) < 3))
        lead = base + off - self.sync_w / 2              # sync leading edge of every line
        us = self.fs * 1e-6
        # levels: sync tip, blanking (back porch) and the brightest picture content
        pos = np.arange(n)
        tip = np.interp(lead[rows] + self.sync_w / 2, pos, s)
        porch = np.interp(lead[rows, None] + (std.sync_us + np.linspace(1.0, 4.0, 6)) * us, pos, s)
        x = (std.active_start_us + np.arange(self.width) * std.active_us / self.width) * us
        pix = np.interp((lead[rows, None] + x[None, :]).ravel(), pos, s).reshape(-1, self.width)
        lv = {"sync": float(np.median(tip)), "blank": float(np.median(porch)),
              "white": float(np.percentile(pix, 1)), "noise": float(np.std(porch - porch.mean(1, keepdims=True)))}
        if self._levels is None:
            self._levels = lv
        else:
            for key, val in lv.items():
                self._levels[key] += 0.25 * (val - self._levels[key])
        L = self._levels
        span_v = max(L["blank"] - L["white"], 1e-6)
        img = np.clip((L["blank"] - pix) / span_v, 0.0, 1.0).astype(np.float32)
        snr = 20 * np.log10(max(L["sync"] - L["blank"], 1e-9) / max(L["noise"], 1e-9))
        return Field(img, vlocked, hlock, float(snr))

    # -- streaming ------------------------------------------------------------------
    def feed(self, env: np.ndarray) -> list[Field]:
        self._buf = np.concatenate([self._buf, self.sign * env.astype(np.float32)])
        out: list[Field] = []
        spl, flen = self.spl, self.field_len
        need = int(flen + 3 * spl)
        while len(self._buf) >= int(1.25 * flen) + need:
            s = self._buf
            la = _moving_mean(s, int(round(spl)))
            mf = _moving_mean(s, self.sync_w)
            if self._expect is None:
                v = self._vsync(la, mf, 0, int(1.1 * flen))
                if v is None:
                    self._drop(int(flen / 2))
                    continue
                vlocked = True
            else:
                e = self._expect
                v = self._vsync(la, mf, int(e - 8 * spl), int(e + 8 * spl))
                if v is None:                          # the source may have jumped: look across a whole field
                    v = self._vsync(la, mf, int(e - 0.55 * flen), int(e + 0.55 * flen))
                vlocked = v is not None
                if v is None:
                    self._misses += 1
                    if self._misses > 25:          # lost the picture: search from scratch
                        self._expect = None
                        self._misses = 0
                        continue
                    v = e
                else:
                    self._misses = 0
            if v + need > len(s):
                break
            out.append(self._slice(s, mf, v, vlocked))
            self._expect = v + flen
            self._drop(int(v + 0.45 * flen))          # keep room to find a sync that comes early
        if len(self._buf) > 4 * flen:                 # never let the buffer grow without bound
            self._drop(len(self._buf) - int(2 * flen))
        return out

    def _drop(self, k: int) -> None:
        k = max(0, min(k, len(self._buf)))
        self._buf = self._buf[k:]
        if self._expect is not None:
            self._expect -= k
            if self._expect < 0:
                self._expect = None
