import numpy as np

import synth
from atvrx.dsp import STANDARDS, carrier_offset_for
from atvrx.scan import PLANS, analyse, channel_label, merge, targets

FS = 2.4e6


def test_finds_video_carrier_and_standard():
    for key in ("625", "525"):
        std = STANDARDS[key]
        env = synth.envelope(std, FS, 0.15, synth.picture(std.active_lines, 384))
        x = synth.iq(env, FS, -0.5e6, 20)
        found = analyse(x, FS, 488.0e6)
        video = [c for c in found if c["video"]]
        assert len(video) == 1
        assert abs(video[0]["freq_hz"] - 487.5e6) < 2e3
        assert video[0]["standard"] == key


def test_plain_carrier_is_not_video():
    t = np.arange(int(FS * 0.15)) / FS
    rng = np.random.default_rng(0)
    x = (np.exp(2j * np.pi * 0.3e6 * t) + 0.05 * (rng.normal(size=len(t)) + 1j * rng.normal(size=len(t))))
    found = analyse(x.astype(np.complex64), FS, 450e6)
    assert found and not any(c["video"] for c in found)
    assert abs(found[0]["freq_hz"] - 450.3e6) < 2e3


def test_plan_targets_put_carrier_at_decoder_offset():
    centers = targets("e-uhf", FS)
    assert len(centers) == 49
    assert abs(centers[2] + carrier_offset_for(FS) - 487.25e6) < 1
    assert channel_label(487.26e6) == "E23"
    assert PLANS["e-vhf"][1][0] == ("E5", 175.25e6)


def test_range_targets_cover_range():
    c = targets("range", FS, 470, 480)
    assert c[0] - 0.4 * FS <= 470e6 and c[-1] + 0.4 * FS >= 480e6


def test_merge_keeps_stronger():
    r = merge([], [{"freq_hz": 1e6, "level_db": 10}])
    r = merge(r, [{"freq_hz": 1.01e6, "level_db": 20}, {"freq_hz": 2e6, "level_db": 5}])
    assert [x["level_db"] for x in r] == [20, 5]


def test_centre_spike_is_ignored():
    rng = np.random.default_rng(1)
    n = int(FS * 0.12)
    x = 0.05 * (rng.normal(size=n) + 1j * rng.normal(size=n)) + 0.8      # DC offset, as RTL-SDRs have
    assert analyse(x.astype(np.complex64), FS, 500e6) == []
