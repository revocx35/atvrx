import numpy as np
import pytest

import synth
from atvrx.dsp import STANDARDS, Demodulator, FieldSlicer, carrier_offset_for, find_carrier

FS = 2.4e6


def decode(x, std, positive=False):
    f, cnr = find_carrier(x, FS, carrier_offset_for(FS))
    demod = Demodulator(FS, f)
    slicer = FieldSlicer(FS, std, width=384, positive=positive)
    fields = []
    for i in range(0, len(x), 96000):
        fields += slicer.feed(demod.process(x[i:i + 96000]))
    return fields, f, cnr


def corr(a, b):
    return float(np.corrcoef(a.ravel(), b.ravel())[0, 1])


@pytest.mark.parametrize("cnr, min_corr", [(30, 0.9), (15, 0.8)])
def test_recovers_picture(cnr, min_corr):
    std = STANDARDS["625"]
    pic = synth.picture(std.active_lines, 384)
    env = synth.envelope(std, FS, 0.5, pic, rate_ppm=40)
    true_carrier = carrier_offset_for(FS) + 3e3
    fields, f, measured_cnr = decode(synth.iq(env, FS, true_carrier, cnr), std)
    assert len(fields) >= 20
    assert abs(f - true_carrier) < 100
    assert abs(measured_cnr - cnr) < 3
    assert all(fl.vlocked for fl in fields[1:])
    avg = np.mean([fl.image for fl in fields[1:]], axis=0)
    assert corr(avg, pic) > min_corr


def test_single_fields_survive_timing_jumps():
    std = STANDARDS["625"]
    pic = synth.picture(std.active_lines, 384)
    # one jump between fields, one in the middle of a field
    env = synth.envelope(std, FS, 0.5, pic, jumps={0.200: 23.0, 0.3105: -11.0})
    fields, _, _ = decode(synth.iq(env, FS, carrier_offset_for(FS), 25), std)
    scores = [corr(fl.image, pic) for fl in fields[1:]]
    assert len(scores) >= 20
    assert np.median(scores) > 0.85
    assert min(scores) > 0.5          # the field with the mid-field jump is torn but still a picture


def test_relocks_at_once_after_a_big_field_jump():
    std = STANDARDS["625"]
    pic = synth.picture(std.active_lines, 384)
    env = synth.envelope(std, FS, 0.5, pic, jumps={0.2: 7000.0})     # 7 ms, about 110 lines
    fields, _, _ = decode(synth.iq(env, FS, carrier_offset_for(FS), 25), std)
    assert len(fields) >= 20
    assert sum(not fl.vlocked for fl in fields[1:]) <= 1
    assert np.median([corr(fl.image, pic) for fl in fields[1:]]) > 0.85


def test_525_line_standard():
    std = STANDARDS["525"]
    pic = synth.picture(std.active_lines, 384)
    env = synth.envelope(std, FS, 0.4, pic)
    fields, _, _ = decode(synth.iq(env, FS, carrier_offset_for(FS), 25), std)
    assert len(fields) >= 20
    assert corr(np.mean([fl.image for fl in fields[1:]], axis=0), pic) > 0.85


def test_positive_modulation():
    std = STANDARDS["625"]
    pic = synth.picture(std.active_lines, 384)
    env = synth.envelope(std, FS, 0.4, pic, positive=True)
    fields, _, _ = decode(synth.iq(env, FS, carrier_offset_for(FS), 25), std, positive=True)
    assert len(fields) >= 15
    assert corr(np.mean([fl.image for fl in fields[1:]], axis=0), pic) > 0.85


def test_noise_alone_gives_no_picture():
    rng = np.random.default_rng(3)
    x = (rng.normal(size=int(FS * 0.4)) + 1j * rng.normal(size=int(FS * 0.4))).astype(np.complex64)
    fields, _, _ = decode(x, STANDARDS["625"])
    assert not any(fl.vlocked for fl in fields)
