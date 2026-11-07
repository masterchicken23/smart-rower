"""The player: the schedule, the clock rewriting that keeps streams aligned,
and -- over real loopback sockets -- that what goes out is what came in."""

from __future__ import annotations

import json
import socket
import threading
import time

from recorder import play as play_mod
from recorder.format import Session, frame_header, json_obj
from recorder.play import (
    Timebase,
    default_video_binds,
    parse_args,
    prescan,
    rewrite_april,
    rewrite_header,
    rewrite_mqtt,
)

from .helpers import SEC, write_session


def _plan(tmp_path, *extra):
    w = write_session(tmp_path, seconds=2.0)
    args = parse_args([str(tmp_path), "--apriltag", *extra])
    sess = Session(tmp_path)
    return w, args, sess, prescan(sess, args)


def test_default_video_ports_follow_camera_numbers():
    assert default_video_binds(["cam1", "cam2"]) == {
        "cam1": "tcp://*:5581", "cam2": "tcp://*:5582"}
    assert default_video_binds(["cam3"]) == {"cam3": "tcp://*:5583"}
    assert default_video_binds(["bow", "cam1"]) == {
        "cam1": "tcp://*:5581", "bow": "tcp://*:5582"}


def test_schedule_reproduces_recorded_spacing(tmp_path):
    _, _, sess, plan = _plan(tmp_path)
    tb = Timebase(plan, 1.0, play_t0_ns=7 * SEC, play_wall0=0.0)
    evs = list(sess.events())
    for a, b in zip(evs, evs[1:], strict=False):
        assert tb.at(b.tm) - tb.at(a.tm) == b.tm - a.tm
    tb2 = Timebase(plan, 2.0, play_t0_ns=7 * SEC, play_wall0=0.0)
    assert tb2.at(evs[-1].tm) - tb2.at(evs[0].tm) == (evs[-1].tm - evs[0].tm) // 2
    sess.close()


def test_sender_clocks_keep_their_offset_to_arrival(tmp_path):
    """The invariant the alignment rests on: for every device, (sender clock
    - send time) changes exactly as (sender clock - arrival) did when it was
    recorded. The estimators downstream then see what they saw live."""
    _, _, sess, plan = _plan(tmp_path)
    tb = Timebase(plan, 1.0, play_t0_ns=50 * SEC, play_wall0=0.0)
    for topic in ("rower/1/seat", "rower/2/seat"):
        rec_off, play_off = [], []
        for ev in sess.mqtt():
            if ev.source != topic:
                continue
            t = json_obj(ev.payload)["t_ms"]
            t2 = json_obj(rewrite_mqtt(topic, ev.payload, tb, 0))["t_ms"]
            rec_off.append(t - ev.tm / 1e6)
            play_off.append(t2 - tb.at(ev.tm) / 1e6)
        d = [p - r for p, r in zip(play_off, rec_off, strict=True)]
        assert max(d) - min(d) <= 1.0      # t_ms is whole milliseconds
    sess.close()


def test_clocks_and_seq_only_move_forward_across_loops_and_runs(tmp_path):
    _, _, sess, plan = _plan(tmp_path)
    seat = [ev for ev in sess.mqtt() if ev.source == "rower/1/seat"]
    frames = list(sess.frames("cam1"))

    def run(t0_ns):
        tb = Timebase(plan, 1.0, play_t0_ns=t0_ns, play_wall0=0.0)
        t_ms, seqs, cam_t = [], [], []
        for p in (0, 1, 2):
            for ev in seat:
                o = json_obj(rewrite_mqtt(ev.source, ev.payload, tb, p))
                t_ms.append(o["t_ms"])
                seqs.append(o["seq"])
            for ev in frames:
                cam_t.append(frame_header(rewrite_header("cam1", ev.pre, tb, p))
                             ["t_send_ms"])
        return t_ms, seqs, cam_t

    t_ms, seqs, cam_t = run(100 * SEC)
    assert t_ms == sorted(t_ms) and len(set(t_ms)) == len(t_ms)
    assert cam_t == sorted(cam_t)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))   # gapless
    later, _, _ = run(200 * SEC)                                # next run
    assert later[0] > t_ms[-1]
    sess.close()


def test_epoch_fields_are_shifted_onto_now(tmp_path):
    w, _, sess, plan = _plan(tmp_path)
    now = 1_900_000_000.0
    tb = Timebase(plan, 1.0, play_t0_ns=1 * SEC, play_wall0=now)
    shift = now - plan.tw0
    for ev in sess.apriltag():
        a, b = json_obj(ev.payload), json_obj(rewrite_april(ev.source, ev.payload, tb, 0))
        assert abs(b["t"] - (a["t"] + shift)) < 1e-6
        assert all(abs(b["ts"][k] - (a["ts"][k] + shift)) < 1e-6 for k in a["ts"])
        assert b["tags"] == a["tags"] and b["tick"] == a["tick"]
    sess.close()


LEAD_IN = "2.5"
"""The receivers connect before the player binds. On Linux a refused connect
fails at once and ZMQ retries within 100 ms; on Windows it takes ~2 s to
fail, so a shorter lead-in intermittently loses the first frame."""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _receive(endpoint: str, out: list, stop: threading.Event) -> None:
    import zmq

    sock = zmq.Context.instance().socket(zmq.SUB)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.setsockopt(zmq.RCVTIMEO, 100)
    sock.connect(endpoint)
    while not stop.is_set():
        try:
            parts = sock.recv_multipart()
        except zmq.Again:
            continue
        out.append((time.perf_counter_ns(), parts))
    sock.close(0)


def _play_over_loopback(tmp_path, *flags):
    write_session(tmp_path, seconds=1.0)
    vport, aport = _free_port(), _free_port()
    got_v: list = []
    got_a: list = []
    stop = threading.Event()
    threads = [threading.Thread(target=_receive, args=(f"tcp://127.0.0.1:{p}", out, stop))
               for p, out in ((vport, got_v), (aport, got_a))]
    for t in threads:
        t.start()
    args = parse_args([str(tmp_path), "--no-mqtt", "--video-bind",
                       f"cam1=tcp://127.0.0.1:{vport}", "--apriltag",
                       f"tcp://127.0.0.1:{aport}", "--lead-in-s", LEAD_IN,
                       "--stats-interval", "0", *flags])
    play_mod.STOP.clear()
    with Session(tmp_path) as sess:
        summary = play_mod.play(sess, args)
        frames = list(sess.frames("cam1"))
        jpegs = [sess.jpeg(e) for e in frames]
        records = list(sess.apriltag())
    time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join()
    return summary, frames, jpegs, records, got_v, got_a


def test_verbatim_replay_is_byte_exact_and_on_time(tmp_path):
    summary, frames, jpegs, records, got_v, got_a = _play_over_loopback(
        tmp_path, "--verbatim")
    assert summary["sent"] == {"frame": len(frames), "apriltag": len(records)}
    assert [p for _, p in got_v] == [[*e.pre, j] for e, j in zip(frames, jpegs, strict=True)]
    assert [p[1] for _, p in got_a] == [e.payload for e in records]
    # Spacing as recorded. Generous: CI and Windows timers are coarse.
    rec = [e.tm for e in frames]
    out = [t for t, _ in got_v]
    err_ms = [abs((o - out[0]) - (r - rec[0])) / 1e6 for o, r in zip(out, rec, strict=True)]
    assert max(err_ms) < 20.0
    assert summary["late_ms"]["p50"] < 5.0


def test_rewritten_replay_keeps_jpegs_and_moves_clocks(tmp_path):
    _, frames, jpegs, _, got_v, _ = _play_over_loopback(tmp_path)
    assert [p[-1] for _, p in got_v] == jpegs              # pixels untouched
    sent = [json.loads(p[0])["msg"] for _, p in got_v]
    orig = [frame_header(e.pre) for e in frames]
    # Shifted by one constant per device; nothing else in the header changed.
    shift = sent[0]["t_send_ms"] - orig[0]["t_send_ms"]
    for s, o in zip(sent, orig, strict=True):
        assert abs(s["t_send_ms"] - o["t_send_ms"] - shift) < 0.01
        assert abs(s["t_ms"] - o["t_ms"] - shift) < 0.01
        assert (s["seq"], s["cam"], s["w"]) == (o["seq"], o["cam"], o["w"])


def test_a_damaged_frame_is_skipped_and_counted(tmp_path):
    write_session(tmp_path, seconds=1.0)
    with Session(tmp_path) as sess:
        bad = list(sess.frames("cam1"))[4]
    with open(tmp_path / "cam1.mjpeg", "r+b") as f:
        f.seek(bad.rec["off"] + 10)
        f.write(b"\x00\x00\x00")
    vport = _free_port()
    got: list = []
    stop = threading.Event()
    t = threading.Thread(target=_receive, args=(f"tcp://127.0.0.1:{vport}", got, stop))
    t.start()
    args = parse_args([str(tmp_path), "--no-mqtt", "--verbatim", "--video-bind",
                       f"cam1=tcp://127.0.0.1:{vport}", "--lead-in-s", LEAD_IN,
                       "--stats-interval", "0"])
    play_mod.STOP.clear()
    with Session(tmp_path) as sess:
        n = len(list(sess.frames("cam1")))
        summary = play_mod.play(sess, args)
    time.sleep(0.2)
    stop.set()
    t.join()
    assert summary["corrupt"] == {"cam1": 1}
    assert summary["sent"] == {"frame": n - 1}
    assert len(got) == n - 1
    assert bad.pre[0] not in [p[0] for _, p in got]
