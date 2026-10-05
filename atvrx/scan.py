"""Finding analog TV carriers: strong spectral lines whose AM carries the line-rate structure."""
from __future__ import annotations

import numpy as np
from scipy import signal

from .dsp import STANDARDS, carrier_offset_for

PLANS = {
    "e-uhf": ("UHF channels E21-E69 (Europe)", [(f"E{n}", 471.25e6 + 8e6 * (n - 21)) for n in range(21, 70)]),
    "e-vhf": ("VHF channels E5-E12 (Europe)", [(f"E{n}", 175.25e6 + 7e6 * (n - 5)) for n in range(5, 13)]),
}


def channel_label(freq_hz: float) -> str:
    for _, entries in PLANS.values():
        for label, vision in entries:
            if abs(freq_hz - vision) < 0.5e6:
                return label
    return ""


def targets(plan: str, fs: float, start_mhz: float = 0, stop_mhz: float = 0) -> list[float]:
    """Centre frequencies to visit for a channel plan or a free range."""
    off = carrier_offset_for(fs)
    if plan in PLANS:
        return [vision - off for _, vision in PLANS[plan][1]]
    step = 0.8 * fs
    lo, hi = start_mhz * 1e6, stop_mhz * 1e6
    if hi <= lo:
        raise ValueError("The range must end above where it starts")
    return list(np.arange(lo + step / 2, hi + step / 2, step))


def analyse(x: np.ndarray, fs: float, center_hz: float, max_carriers: int = 4) -> list[dict]:
    """Carriers in one capture, each checked for the line-rate signature of analog video."""
    f, p = signal.welch(x, fs, nperseg=8192, return_onesided=False, detrend=False)
    o = np.argsort(f)
    f, pdb = f[o], 10 * np.log10(p[o] + 1e-20)
    floor = np.percentile(pdb, 30)
    usable = (np.abs(f) < 0.42 * fs) & (np.abs(f) > 10e3)    # skip the receiver's own spike at the tuned centre
    peaks, _ = signal.find_peaks(np.where(usable, pdb, floor), height=floor + 12, distance=int(100e3 / (fs / 8192)))
    peaks = peaks[np.argsort(pdb[peaks])[::-1][:3 * max_carriers]]
    t = np.arange(len(x)) / fs
    lp = signal.firwin(63, 1.0e6, fs=fs)
    found = []
    for k in peaks:
        # sidebands, colour and sound carriers of a channel already found belong to that channel
        if any(c["video"] and -1.3e6 < center_hz + f[k] - c["freq_hz"] < 6.5e6 for c in found):
            continue
        y = signal.oaconvolve(x * np.exp(-2j * np.pi * f[k] * t), lp, mode="same")
        env = np.abs(y[::4]).astype(np.float64)
        fs4 = fs / 4
        spec = np.abs(np.fft.rfft((env - env.mean()) * np.hanning(len(env))))
        fe = np.fft.rfftfreq(len(env), 1 / fs4)
        bg = np.median(spec[(fe > 9e3) & (fe < 13e3)]) + 1e-12
        best_key, best_db = None, -99.0
        for key, std in STANDARDS.items():
            m = np.abs(fe - std.line_rate) < 20
            score = 20 * np.log10(spec[m].max() / bg) if m.any() else -99.0
            if score > best_db:
                best_key, best_db = key, score
        freq = center_hz + f[k]
        found.append({
            "freq_hz": float(freq),
            "level_db": round(float(pdb[k] - floor), 1),
            "line_db": round(float(best_db), 1),
            "standard": best_key,
            "video": bool(best_db > 15),
            "channel": channel_label(freq),
        })
        if len(found) == max_carriers:
            break
    return found


def merge(results: list[dict], new: list[dict]) -> list[dict]:
    """Add carriers, keeping the stronger entry when the same one shows up twice."""
    for c in new:
        same = [r for r in results if abs(r["freq_hz"] - c["freq_hz"]) < 30e3]
        if not same:
            results.append(c)
        elif c["level_db"] > same[0]["level_db"]:
            results[results.index(same[0])] = c
    results.sort(key=lambda r: r["freq_hz"])
    return results
