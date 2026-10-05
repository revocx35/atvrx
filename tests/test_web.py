import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import synth
from atvrx.dsp import STANDARDS
from atvrx.auth import UserStore
from atvrx.session import Config, Session
from atvrx.web import create_app
from fakes import FS, FakeSpyServer, Transmitter


def wait_for(fn, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def spy():
    srv = FakeSpyServer(Transmitter(487.25e6, cnr_db=25))
    yield srv
    srv.close()


@pytest.fixture
def client(tmp_path):
    sess = Session(Config(host="127.0.0.1"), tmp_path, idle_release_s=60)
    users = UserStore(tmp_path / "users.json")
    users.add("tester", "tester password")
    with TestClient(create_app(sess, users=users), headers={"X-ATVRX": "1"}) as c:
        assert c.post("/api/login", json={"username": "tester", "password": "tester password"}).status_code == 200
        c.session = sess
        yield c
    sess.stop()


def picture_score(sess):
    frame = sess._frame
    pic = synth.picture(STANDARDS["625"].active_lines, 384)
    return float(np.corrcoef(frame.ravel(), pic.ravel())[0, 1])


def test_watch_decodes_picture_and_serves_it(client, spy):
    r = client.post("/api/watch", json={"source": "spyserver", "host": "127.0.0.1", "port": spy.port,
                                        "freq_mhz": 487.25, "gain": 25, "average": 4})
    assert r.status_code == 200
    wait_for(lambda: client.get("/api/state").json()["frame_seq"] > 30)
    st = client.get("/api/state").json()
    assert st["state"] == "watching" and "RTL-SDR" in st["device"]
    assert picture_score(client.session) > 0.85
    wait_for(lambda: client.get("/api/state").json()["stats"].get("fields_per_s", 0) > 0)
    stats = client.get("/api/state").json()["stats"]
    assert abs(stats["carrier_mhz"] - 487.25) < 0.002
    assert stats["vlock"] >= 90
    png = client.get("/api/snapshot.png")
    assert png.status_code == 200 and Image.open(io.BytesIO(png.content)).size == (768, 576)
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        got_jpeg = got_json = False
        for _ in range(40):
            m = ws.receive()
            if m.get("bytes"):
                got_jpeg = got_jpeg or m["bytes"][:2] == b"\xff\xd8"
            elif m.get("text"):
                got_json = True
            if got_jpeg and got_json:
                break
        assert got_jpeg and got_json
    assert client.post("/api/stop").json()["state"] == "idle"


def test_live_retune_and_gain(client, spy):
    client.post("/api/watch", json={"source": "spyserver", "host": "127.0.0.1", "port": spy.port, "freq_mhz": 479.25})
    wait_for(lambda: client.get("/api/state").json()["state"] == "watching")
    client.post("/api/settings", json={"freq_mhz": 487.25, "gain": 7})
    wait_for(lambda: spy.settings.get(2) == 7 and spy.settings.get(101) == int(487.25e6 + 0.9e6))
    wait_for(lambda: client.session.frame_seq > 20 and picture_score(client.session) > 0.8)


def test_scan_finds_the_channel(client, spy):
    client.post("/api/settings", json={"source": "spyserver", "host": "127.0.0.1", "port": spy.port})
    r = client.post("/api/scan", json={"plan": "e-uhf"})
    assert r.status_code == 200 and r.json()["scan"]["running"] is True
    wait_for(lambda: (s := client.get("/api/state").json())["state"] == "scanning" and s["scan"]["running"])
    st = wait_for(lambda: (s := client.get("/api/state").json())["scan"]["running"] is False
                  and s["scan"]["progress"] == 1.0 and s, timeout=60)
    video = [r for r in st["scan"]["results"] if r["video"]]
    assert [r["channel"] for r in video] == ["E23"]
    assert st["state"] == "idle" and "1 TV picture carrier found" in st["message"]


def test_file_playback(client, tmp_path):
    std = STANDARDS["625"]
    x = synth.iq(synth.envelope(std, FS, 0.3, synth.picture()), FS, -0.9e6, 30)
    (tmp_path / "ch23.u8").write_bytes(synth.to_u8(x))
    assert client.get("/api/recordings").json() == ["ch23.u8"]
    r = client.post("/api/watch", json={"source": "file", "file": "ch23.u8", "file_rate": FS,
                                        "file_center_mhz": 488.15, "freq_mhz": 487.25})
    assert r.status_code == 200
    wait_for(lambda: client.session.frame_seq > 10)
    assert picture_score(client.session) > 0.8


@pytest.mark.parametrize("body, text", [
    ({"standard": "819"}, "Unknown TV standard"),
    ({"source": "file", "file": "../../etc/passwd"}, "Pick a recording"),
    ({"average": 99}, "Averaging"),
    ({"port": 0}, "Port"),
])
def test_bad_settings_are_rejected(client, body, text):
    r = client.post("/api/settings", json=body)
    assert r.status_code == 400 and text in r.json()["detail"]


def test_unreachable_server_reports_error(client):
    client.post("/api/watch", json={"source": "spyserver", "host": "127.0.0.1", "port": 1})
    st = wait_for(lambda: (s := client.get("/api/state").json())["state"] == "error" and s)
    assert "Cannot connect" in st["message"]


def test_radio_released_when_nobody_watches(tmp_path, spy):
    sess = Session(Config(host="127.0.0.1", port=spy.port), tmp_path, idle_release_s=0.5)
    sess.watch()
    wait_for(lambda: sess.state == "watching")
    wait_for(lambda: sess.state == "idle", timeout=10)
    assert "nobody was watching" in sess.message
    wait_for(lambda: spy.settings.get(1) == 0, timeout=5)
