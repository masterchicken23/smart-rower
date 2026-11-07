"""Session lifecycle: start, record, stop, the meta.json it leaves behind,
and the two ways a session ends without being asked (low disk, shutdown)."""

from __future__ import annotations

import time

import pytest

from recorder import session as session_mod
from recorder.format import FrameMsg, MqttMsg, Session, read_meta
from recorder.session import BusyError, IdleError, LowDiskError, SessionManager
from recorder.writer import WriteQueue

from .helpers import JPEG, settings


def test_start_offer_stop_writes_a_complete_session(tmp_path):
    m = SessionManager(settings(tmp_path))
    m.offer(MqttMsg(1, 1.0, "rower/1/seat", 0, False, b"{}"))   # idle: dropped
    info = m.start("trial")
    path = tmp_path / info["session"]
    assert info["session"].endswith("_trial")
    meta = read_meta(path)
    assert meta["complete"] is False and meta["start"]["tm"] > 0

    for i in range(5):
        m.offer(MqttMsg(10 + i, 2.0, "rower/1/seat", 0, False,
                        b'{"seq":%d,"t_ms":1}' % i))
    m.offer(FrameMsg("cam1", 20, 3.0, [b'{"msg":{}}'], JPEG))
    summary = m.stop()

    meta = read_meta(path)
    assert meta["complete"] is True and meta["stop_reason"] == "request"
    assert meta["end"]["tm"] > meta["start"]["tm"]
    assert summary["written"] == {"mqtt": 5, "cam1": 1}
    assert meta["summary"]["written"] == {"mqtt": 5, "cam1": 1}
    with Session(path) as s:
        assert len(list(s.mqtt())) == 5
        assert [s.jpeg(e) for e in s.frames("cam1")] == [JPEG]


def test_one_session_at_a_time(tmp_path):
    m = SessionManager(settings(tmp_path))
    with pytest.raises(IdleError):
        m.stop()
    m.start()
    with pytest.raises(BusyError):
        m.start()
    m.stop()
    m.start()                               # and a new one after
    m.stop()
    assert len(m.sessions()) == 2


def test_refuses_to_start_on_a_full_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "disk_free", lambda _p: 10)
    m = SessionManager(settings(tmp_path, min_free_gb=1.0))
    with pytest.raises(LowDiskError):
        m.start()
    assert not m.recording and not any(tmp_path.iterdir())


def test_stops_itself_when_the_disk_fills(tmp_path, monkeypatch):
    # The start check passes; the writer's own check then finds no space.
    monkeypatch.setattr(session_mod, "disk_free", lambda _p: 10**18)
    m = SessionManager(settings(tmp_path, min_free_gb=1e9, disk_check_s=0.01))
    name = m.start()["session"]
    deadline = time.monotonic() + 5.0
    while m.last is None and time.monotonic() < deadline:
        time.sleep(0.02)                    # `last` is set once stop() is done
    assert not m.recording
    assert m.last is not None and m.last["stop_reason"] == "low disk"
    assert read_meta(tmp_path / name)["stop_reason"] == "low disk"


def test_queue_drops_oldest_and_counts_per_stream():
    q = WriteQueue(max_bytes=3000)
    for i in range(10):
        q.put(FrameMsg("cam1", i, 0.0, [], b"x" * 900))
    items = q.take_all(0.0)
    assert [it.tm for it in items] == [8, 9]        # the newest survive
    assert q.dropped == {"cam1": 8}
