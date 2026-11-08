"""The control channel: a recorder-like PUB on loopback drives the camera's
local recording through start, a new session, a stop, silence (which must
change nothing), and the recorder's sync switch."""

from __future__ import annotations

import json
import threading
import time

import pytest

from cam_sender import localrec
from cam_sender.control import ControlListener, parse

zmq = pytest.importorskip("zmq")


def until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def recording(rec: localrec.LocalRecorder, name: str) -> bool:
    return rec.active is not None and rec.active.name == name


def stopped(rec: localrec.LocalRecorder, name: str) -> bool:
    """Finished stopping (meta.json written). `active` clears first, so the
    frame loop stops offering before the drain."""
    return rec.last is not None and rec.last["session"] == name


class FakeRecorder:
    """What recorder/camsync.py does: the current state, every 50 ms."""

    def __init__(self, port: int = 0) -> None:
        self.session = None
        self.sync = True
        self.state = 0
        self._halt = threading.Event()
        self.sock = zmq.Context.instance().socket(zmq.PUB)
        self.sock.setsockopt(zmq.LINGER, 0)
        deadline = time.monotonic() + 3.0
        while True:                     # rebinding a just-closed port can lag
            try:
                if port:
                    self.sock.bind(f"tcp://127.0.0.1:{port}")
                    self.port = port
                else:
                    self.port = self.sock.bind_to_random_port("tcp://127.0.0.1")
                break
            except zmq.ZMQError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.endpoint = f"tcp://127.0.0.1:{self.port}"
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def set(self, session, sync=True) -> None:
        self.session, self.sync = session, sync
        self.state += 1

    def _run(self) -> None:
        while not self._halt.wait(0.05):
            self.sock.send(json.dumps({
                "v": 1, "type": "rec", "sync": self.sync,
                "session": self.session if self.sync else None,
                "state": self.state, "tm": time.perf_counter_ns(),
                "tw": time.time()}).encode())

    def close(self) -> None:
        self._halt.set()
        self._t.join(2.0)
        self.sock.close(0)


def test_parse_accepts_only_recorder_states():
    ok = {"v": 1, "type": "rec", "sync": True, "session": "S", "state": 1}
    assert parse(json.dumps(ok).encode()) == ok
    assert parse(json.dumps({**ok, "session": None}).encode())["session"] is None
    assert parse(b"not json") is None
    assert parse(b"\xff\xfe") is None
    assert parse(json.dumps({**ok, "type": "other"}).encode()) is None
    assert parse(json.dumps({**ok, "v": 2}).encode()) is None
    assert parse(json.dumps({**ok, "session": 5}).encode()) is None
    assert parse(b"[1, 2]") is None


def test_listener_follows_the_recorder_and_holds_through_silence(tmp_path):
    rec = localrec.LocalRecorder(tmp_path, "cam1", now_ns=time.perf_counter_ns,
                                 min_free_mb=0)
    fake = FakeRecorder()
    halt = threading.Event()
    ctl = ControlListener(fake.endpoint, rec.want, halt, warn_s=0.3)
    ctl.start()
    try:
        assert until(lambda: ctl.n > 0)
        assert rec.active is None                   # idle until told

        fake.set("A")
        assert until(lambda: recording(rec, "A"))
        seg = localrec.read_meta(tmp_path / "A")["segments"][0]
        assert seg["control_start"]["recorder"]["state"] == fake.state

        fake.set("B")
        assert until(lambda: recording(rec, "B"))
        assert stopped(rec, "A")                    # stopped before B began
        assert localrec.read_meta(tmp_path / "A")["stop_reason"] == "superseded"

        fake.set(None)
        assert until(lambda: stopped(rec, "B"))
        assert rec.active is None
        assert localrec.read_meta(tmp_path / "B")["stop_reason"] == "recorder stopped"

        # The link goes down mid-session: the camera keeps recording.
        fake.set("C")
        assert until(lambda: recording(rec, "C"))
        port = fake.port
        fake.close()
        time.sleep(1.0)
        assert recording(rec, "C")
        assert ctl.silent_s() > 0.5

        # It comes back with sync switched off on the recorder: stop.
        fake = FakeRecorder(port)
        fake.set("C", sync=False)
        assert until(lambda: stopped(rec, "C"), timeout=15.0)
        assert rec.active is None and ctl.sync is False
        assert localrec.read_meta(tmp_path / "C")["stop_reason"] == "recorder stopped"
    finally:
        halt.set()
        ctl.join(3.0)
        fake.close()
        rec.shutdown()
    assert not ctl.is_alive()
