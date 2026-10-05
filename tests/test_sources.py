import pytest

from atvrx.dsp import carrier_offset_for, find_carrier
from atvrx.sources import FileSource, RtlTcpSource, SourceError, SpyServerSource
from fakes import FS, FakeRtlTcp, FakeSpyServer, Transmitter
import synth
from atvrx.dsp import STANDARDS


@pytest.fixture
def spy():
    srv = FakeSpyServer(Transmitter(487.25e6))
    yield srv
    srv.close()


def test_spyserver_handshake_tune_and_stream(spy):
    src = SpyServerSource("127.0.0.1", spy.port)
    src.open()
    try:
        assert src.sample_rate == FS
        assert src.gain_steps == 30
        assert "RTL-SDR" in src.description
        src.tune(487.25e6 - carrier_offset_for(FS))
        src.set_gain(20)
        src.start()
        src.discard(0.05)
        x = src.read(1 << 16)
        assert len(x) == 1 << 16
        f, cnr = find_carrier(x, FS, carrier_offset_for(FS))
        assert abs(f - carrier_offset_for(FS)) < 1e3 and cnr > 15
    finally:
        src.close()
    s = spy.settings
    assert s[100] == 1 and s[0] == 1 and s[102] == 0 and s[2] == 20
    assert s[101] == int(487.25e6 - carrier_offset_for(FS))


def test_spyserver_without_device_is_reported():
    srv = FakeSpyServer(Transmitter(487.25e6), device_type=0)
    try:
        with pytest.raises(SourceError, match="no radio"):
            SpyServerSource("127.0.0.1", srv.port).open()
    finally:
        srv.close()


def test_spyserver_without_control_is_reported():
    srv = FakeSpyServer(Transmitter(487.25e6), can_control=0)
    try:
        with pytest.raises(SourceError, match="does not let this client tune"):
            SpyServerSource("127.0.0.1", srv.port).open()
    finally:
        srv.close()


def test_spyserver_rejects_out_of_range_tuning(spy):
    src = SpyServerSource("127.0.0.1", spy.port)
    src.open()
    try:
        with pytest.raises(SourceError, match="outside the radio's range"):
            src.tune(2400e6)
    finally:
        src.close()


def test_connection_refused_is_a_clear_error():
    with pytest.raises(SourceError, match="Cannot connect"):
        SpyServerSource("127.0.0.1", 1, timeout=1).open()


def test_rtltcp_commands_and_stream():
    srv = FakeRtlTcp(Transmitter(487.25e6))
    src = RtlTcpSource("127.0.0.1", srv.port)
    try:
        src.open()
        assert src.gain_steps == 29 and "R820T" in src.description
        src.tune(488.15e6)
        src.set_gain(12)
        src.discard(0.05)
        x = src.read(1 << 16)
        f, _ = find_carrier(x, FS, -0.9e6)
        assert abs(f + 0.9e6) < 1e3
    finally:
        src.close()
        srv.close()
    cmds = dict(srv.commands)
    assert cmds[0x02] == int(FS) and cmds[0x03] == 1 and cmds[0x08] == 0
    assert cmds[0x01] == 488150000 and cmds[0x0D] == 12


def test_file_source_loops_and_keeps_iq_aligned(tmp_path):
    std = STANDARDS["625"]
    x = synth.iq(synth.envelope(std, FS, 0.12, synth.picture()), FS, -0.9e6, 30)
    p = tmp_path / "cap.u8"
    p.write_bytes(synth.to_u8(x) + b"\x80")       # odd length on purpose
    src = FileSource(p, FS, 488.15e6, realtime=False)
    src.open()
    src.start()
    y = src.read(len(x) * 2 + 1000)               # wraps around twice
    f, _ = find_carrier(y[len(x) + 10:len(x) + 10 + 65536], FS, -0.9e6)
    assert abs(f + 0.9e6) < 1e3


def test_file_source_missing_file(tmp_path):
    with pytest.raises(SourceError, match="not found"):
        FileSource(tmp_path / "nope.u8", FS, 0).open()
