"""The local recording. What the frame loop hands it must end up on disk in
the recorder's own session format, so it is read back here with
embedded/recorder's reader (recorder/format.py), CRCs included."""

from __future__ import annotations

import threading
import time

import pytest

from cam_sender import localrec, wire

fmt = pytest.importorskip("recorder.format")

NAME = "20261107T143000Z_drill"


def jpeg(i: int) -> bytes:
    return b"\xff\xd8" + bytes([i % 256]) * 500 + b"\xff\xd9"


def make(tmp_path, **kw) -> localrec.LocalRecorder:
    kw.setdefault("now_ns", time.perf_counter_ns)
    kw.setdefault("min_free_mb", 0)
    kw.setdefault("free", lambda _p: 10**12)
    return localrec.LocalRecorder(tmp_path, "cam1", **kw)


def offer(sess: localrec.Session, seqs) -> None:
    """As the frame loop does: header with `rec`, stamped and packed."""
    for i in seqs:
        hdr = wire.make_header("cam1", i, 1000.0 + i * 66.7, w=640, h=360)
        hdr["rec"] = sess.name
        parts = wire.stamp_and_pack(hdr, jpeg(i), lambda i=i: 1010.0 + i * 66.7)
        sess.offer(parts[0], parts[1], hdr["t_send_ms"])


def until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def stopped(rec: localrec.LocalRecorder, name: str) -> bool:
    """A guard stop has *finished* (meta.json written), not just begun:
    `active` is cleared first, so the frame loop stops offering at once."""
    return rec.last is not None and rec.last["session"] == name


def test_session_reads_back_with_the_recorders_reader(tmp_path):
    rec = make(tmp_path)
    anchor = {"recorder": {"tm": 1, "tw": 2.0, "state": 1},
              "local": {"tm": 3, "tw": 4.0}}
    rec.want(NAME, anchor)
    assert rec.active is not None and rec.active.name == NAME
    offer(rec.active, range(20))
    rec.want(None)
    assert rec.active is None
    assert rec.last["stop_reason"] == "recorder stopped"

    with fmt.Session(tmp_path / NAME) as sess:
        assert sess.cameras() == ["cam1"]
        evs = list(sess.frames("cam1"))
        assert len(evs) == 20
        for i, ev in enumerate(evs):
            assert sess.jpeg(ev) == jpeg(i)          # CRC checked too
            hdr = fmt.frame_header(ev.pre)
            assert hdr["seq"] == i and hdr["rec"] == NAME
            assert ev.tm == int(round(hdr["t_send_ms"] * 1e6))
        m = sess.meta
    assert m["complete"] is True and m["stop_reason"] == "recorder stopped"
    assert m["camera"] == "cam1" and m["name"] == NAME
    assert m["summary"]["written"] == {"cam1": 20}
    assert m["summary"]["dropped"] == {"cam1": 0}
    assert m["segments"][0]["control_start"] == anchor


def test_restart_resumes_the_same_session(tmp_path):
    rec = make(tmp_path)
    rec.want(NAME)
    offer(rec.active, range(5))
    rec.shutdown()
    # A crash cut the index's last line short.
    with open(tmp_path / NAME / "cam1.idx.jsonl", "a", encoding="utf-8") as f:
        f.write('{"tm":12')

    again = make(tmp_path)                      # a restarted cam-sender
    again.want(NAME)
    assert again.active is not None and again.active.resumed
    offer(again.active, range(5))               # seq restarts with the process
    again.want(None)

    with fmt.Session(tmp_path / NAME) as sess:
        evs = list(sess.frames("cam1"))
        assert [fmt.frame_header(e.pre)["seq"] for e in evs] == [*range(5), *range(5)]
        assert all(sess.jpeg(e) for e in evs)
        m = sess.meta
    assert [s["stop_reason"] for s in m["segments"]] == ["shutdown", "recorder stopped"]
    assert [s["resumed"] for s in m["segments"]] == [False, True]
    assert m["summary"]["written"] == {"cam1": 10}
    assert m["complete"] is True


def test_want_follows_the_recorder_and_refuses_unsafe_names(tmp_path):
    root = tmp_path / "rec"
    rec = make(root)
    rec.want("A")
    a = rec.active
    rec.want("A")                               # the same state, every second
    assert rec.active is a
    rec.want("B")
    assert rec.active.name == "B"
    assert localrec.read_meta(root / "A")["stop_reason"] == "superseded"
    for bad in ("../evil", "x/y", ".hidden", "", "a" * 200):
        rec.want(bad)
        assert rec.active is not None and rec.active.name == "B"
    rec.want(None)
    assert rec.active is None
    assert localrec.read_meta(root / "B")["stop_reason"] == "recorder stopped"
    assert sorted(p.name for p in root.iterdir()) == ["A", "B"]
    assert not (tmp_path / "evil").exists()


def test_a_guard_stop_is_not_undone_by_the_next_broadcast(tmp_path):
    free = {"v": 10**12}
    rec = make(tmp_path, min_free_mb=100, free=lambda _p: free["v"],
               disk_check_s=0.05)
    rec.want("A")
    free["v"] = 10**6
    assert until(lambda: stopped(rec, "A"))
    assert rec.active is None and rec.refused["A"] == "low disk"
    free["v"] = 10**12
    rec.want("A")                               # still being broadcast
    assert rec.active is None
    # Nor does a restarted cam-sender resume it: the reason is in meta.json.
    again = make(tmp_path)
    again.want("A")
    assert again.active is None and "A" in again.refused
    again.want("B")                             # a new session is fine
    assert again.active is not None
    again.shutdown()


def test_refuses_to_start_on_a_full_disk(tmp_path):
    rec = make(tmp_path, min_free_mb=100, free=lambda _p: 10**6)
    rec.want("A")
    assert rec.active is None and rec.refused["A"].startswith("low disk")
    assert not (tmp_path / "A").exists()


def test_max_duration_stops_a_session_whose_stop_never_came(tmp_path):
    rec = make(tmp_path, max_s=0.2)
    rec.want("A")
    assert until(lambda: stopped(rec, "A"))
    assert rec.active is None and rec.refused["A"] == "max duration"
    assert localrec.read_meta(tmp_path / "A")["stop_reason"] == "max duration"


def test_queue_drops_oldest_and_counts_late_frames():
    one = localrec._Frame(b"h" * 72, b"j" * 800, 0, 0.0)
    q = localrec.WriteQueue(one.size * 3)
    for _ in range(10):
        q.put(localrec._Frame(b"h" * 72, b"j" * 800, 0, 0.0))
    assert len(q) == 3 and q.dropped == 7
    q.close()
    q.put(one)
    assert q.late == 1 and len(q) == 3


def test_offer_never_waits_on_a_stuck_disk(tmp_path, monkeypatch):
    """The frame loop's side of a stalled SD card: offer() keeps returning at
    once, the oldest waiting frames are dropped and counted, and everything
    is accounted for once the card comes back."""
    gate = threading.Event()
    real = localrec._write_all

    def stuck(f, data):
        gate.wait(10)
        real(f, data)

    monkeypatch.setattr(localrec, "_write_all", stuck)
    rec = make(tmp_path, queue_mb=0.01)         # 10 kB: a dozen frames
    rec.want("A")
    sess = rec.active
    worst = 0.0
    for i in range(300):
        hdr = wire.make_header("cam1", i, float(i))
        parts = wire.stamp_and_pack(hdr, jpeg(i), lambda: 1.0)
        t0 = time.perf_counter()
        sess.offer(parts[0], parts[1], hdr["t_send_ms"])
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.005, f"offer() took {worst * 1e3:.2f} ms"
    assert sess.queue.dropped > 0
    gate.set()
    rec.want(None)
    assert sess.written + sess.queue.dropped == 300
    assert rec.last["dropped"] == {"cam1": sess.queue.dropped}
