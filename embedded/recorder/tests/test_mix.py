"""recorder.mix: sessions moved onto seats and cameras of their own, placed on
a shared timeline by an offset, the offset found from the strokes, and -- over
real sockets -- two recorded rowers coming out as one crew."""

from __future__ import annotations

import bisect
import json
import math
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from recorder import mix
from recorder import play as engine
from recorder.format import (
    AprilMsg,
    MqttMsg,
    Session,
    SessionWriter,
    json_obj,
    write_meta,
)
from recorder.play import place, prescan, rewrite_april, rewrite_mqtt

from .helpers import SEC, seat, write_session
from .test_play import LEAD_IN, _free_port, _receive

TW0 = 1_760_000_000.0


def meta_april(cam: str, t: float) -> bytes:
    return json.dumps({"type": "apriltag_meta", "v": 1, "cam": cam, "t": t,
                       "started": t - 100}).encode()


def two_sessions(tmp_path: Path, seconds: float = 1.0) -> tuple[Path, Path]:
    """Two single-rower sessions recorded ~25 min apart, each with a boat
    topic and an idle detector's apriltag_meta (cam2), as the real ones."""
    out = []
    for name, tm0 in (("A", 1000 * SEC), ("B", 2500 * SEC)):
        extra = (MqttMsg(tm0 + SEC // 10, TW0, "boat/imu", 0, False,
                         b'{"seq":1,"t_ms":10,"ax":0.1}'),
                 AprilMsg(tm0 + SEC // 5, TW0, b"apriltag_meta",
                          meta_april("cam2", TW0)))
        write_session(tmp_path / name, seconds=seconds, seats=(1,), meta=True,
                      tm0=tm0, extra=extra)
        out.append(tmp_path / name)
    return out[0], out[1]


def build(paths, *flags):
    args = mix.parse_args([*map(str, paths), *flags])
    sessions = [Session(p) for p in paths]
    members = mix.crew(sessions, args)
    return args, sessions, members, mix.tracks(members, args)


# ---- who plays as whom -------------------------------------------------- #

def test_later_sessions_move_past_the_seats_and_cameras_in_use():
    assert mix.assign([([1], ["cam1"]), ([1], ["cam1"])]) == [
        ({1: 1}, {"cam1": "cam1"}), ({1: 2}, {"cam1": "cam2"})]
    assert mix.assign([([1, 2], ["cam1", "cam2"]), ([1], ["cam1"]),
                       ([3], ["bow"])]) == [
        ({1: 1, 2: 2}, {"cam1": "cam1", "cam2": "cam2"}),
        ({1: 3}, {"cam1": "cam3"}),
        ({3: 4}, {"bow": "cam4"})]


def test_a_session_is_its_seats_and_the_cameras_with_frames_or_tags(tmp_path):
    a, _ = two_sessions(tmp_path)
    with Session(a) as sess:
        assert mix.discover(sess) == ([1], ["cam1"])     # not the idle cam2


def test_tracks_move_topics_seats_and_detector_records(tmp_path):
    a, b = two_sessions(tmp_path)
    args, sessions, _, (ta, tb_) = build([a, b], "--apriltag", "--no-video")
    try:
        assert {ev.source for ev in ta.events() if ev.kind == "mqtt"} == {
            "rower/1/status", "rower/1/seat", "boat/imu"}
        b_mqtt = {ev.source for ev in tb_.events() if ev.kind == "mqtt"}
        assert b_mqtt == {"rower/1/status", "rower/1/seat"}   # one boat
        assert [tb_.played_topic(t) for t in sorted(b_mqtt)] == [
            "rower/2/seat", "rower/2/status"]
        # The idle detector's cam2 would land on B's moved camera: left out.
        cams = {json_obj(ev.payload)["cam"] for t in (ta, tb_)
                for ev in t.events() if ev.kind == "apriltag"}
        assert cams == {"cam1"}

        for t in (ta, tb_):
            prescan(t)
        place([ta, tb_], 1.0, 50 * SEC, 1_900_000_000.0)
        status = next(ev for ev in tb_.events() if ev.source == "rower/1/status")
        assert json_obj(rewrite_mqtt(status.source, status.payload, tb_.tb, 0,
                                     seat=2))["seat"] == 2
        recs = [ev for ev in tb_.events() if ev.source == "apriltag"]
        out = [json_obj(rewrite_april(ev.source, ev.payload, tb_.tb, 1, tb_.cams))
               for ev in recs]
        assert {o["cam"] for o in out} == {"cam2"}
        ticks = [o["tick"] for o in out]                # pass 2 carries on
        assert ticks[0] == json_obj(recs[-1].payload)["tick"] + 1
    finally:
        for s in sessions:
            s.close()


# ---- the shared timeline ------------------------------------------------ #

def _first(track, kind="mqtt"):
    return next(ev for ev in track.events() if ev.kind == kind
                and ev.source.endswith("/seat"))


@pytest.mark.parametrize("offset", [0.5, -0.5, 0.0])
def test_the_offset_places_one_session_against_the_other(tmp_path, offset):
    a, b = two_sessions(tmp_path)
    _, sessions, _, (ta, tb_) = build([a, b], "--offset", str(offset), "--no-video")
    try:
        for t in (ta, tb_):
            prescan(t)
        loop_ns = place([ta, tb_], 1.0, 50 * SEC, 1_900_000_000.0)
        ea, eb = _first(ta), _first(tb_)
        # Recorded identically, so their first seat samples are `offset` apart.
        d = tb_.tb.at(eb.tm) - ta.tb.at(ea.tm)
        assert d == pytest.approx(offset * SEC, abs=2)
        # Wall-clock fields move with the schedule.
        assert tb_.tb.epoch(TW0 + 0.3) - ta.tb.epoch(TW0 + 0.3) == pytest.approx(
            offset, abs=1e-6)
        # One period for both, spanning both.
        span = (max(t.pos(t.plan.t1) for t in (ta, tb_))
                - min(t.pos(t.plan.t0) for t in (ta, tb_)))
        assert loop_ns == span + int(engine.LOOP_GAP_S * SEC)
        assert ta.tb.loop_ns == tb_.tb.loop_ns == loop_ns
        assert min(t.tb.at(t.plan.t0) for t in (ta, tb_)) == 50 * SEC
    finally:
        for s in sessions:
            s.close()


def test_each_session_keeps_its_clocks_across_loops(tmp_path):
    """Per moved device: (sender clock - send time) is what (sender clock -
    arrival) was, on every pass; clocks and seq only move forward."""
    a, b = two_sessions(tmp_path)
    _, sessions, _, trks = build([a, b], "--offset", "0.37", "--no-video")
    try:
        for t in trks:
            prescan(t)
        place(trks, 1.0, 80 * SEC, 0.0)
        for t in trks:
            seats = [ev for ev in t.events() if ev.source == "rower/1/seat"]
            offs, t_ms, seqs = [], [], []
            for p in (0, 1, 2):
                for ev in seats:
                    o = json_obj(rewrite_mqtt(ev.source, ev.payload, t.tb, p))
                    offs.append((o["t_ms"] - t.tb.at(ev.tm, p) / 1e6)
                                - (json_obj(ev.payload)["t_ms"] - ev.tm / 1e6))
                    t_ms.append(o["t_ms"])
                    seqs.append(o["seq"])
            assert max(offs) - min(offs) <= 1.0
            assert t_ms == sorted(t_ms) and len(set(t_ms)) == len(t_ms)
            assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    finally:
        for s in sessions:
            s.close()


def test_start_and_end_count_on_the_shared_timeline(tmp_path):
    a, b = two_sessions(tmp_path, seconds=2.0)
    _, sessions, _, (ta, tb_) = build([a, b], "--offset", "0.5", "--no-video",
                                      "--start", "0.8", "--end", "1.6")
    try:
        for t, at_s in ((ta, 0.0), (tb_, 0.5)):
            prescan(t)
            assert t.at_s == at_s
            assert 0.8 - at_s <= (t.plan.t0 - t.origin) / 1e9 < 0.8 - at_s + 0.06
            assert (t.plan.t1 - t.origin) / 1e9 <= 1.6 - at_s
    finally:
        for s in sessions:
            s.close()


def test_offsets_are_checked():
    assert mix.parse_offsets(["1.5"], 3) == [1.5, 0.0]
    assert mix.parse_offsets(["auto", "-2"], 3) == [None, -2.0]
    with pytest.raises(ValueError):
        mix.parse_offsets(["1", "2"], 2)
    with pytest.raises(ValueError):
        mix.parse_offsets(["soon"], 2)


# ---- lining the strokes up ---------------------------------------------- #

def stroke_signal(seed: int = 7):
    """A rower's seat distance: strokes of irregular length, so only one lag
    lines two stretches of it up."""
    rng = random.Random(seed)
    bounds = [-40.0]
    while bounds[-1] < 80.0:
        bounds.append(bounds[-1] + rng.uniform(2.0, 3.4))

    def d(t: float) -> float:
        i = bisect.bisect_right(bounds, t) - 1
        ph = (t - bounds[i]) / (bounds[i + 1] - bounds[i])
        return 650 - 300 * math.cos(2 * math.pi * ph)
    return d


def write_rowing(path: Path, signal, shift_s: float, *, tm0: int, clock_ms: int,
                 seed: int, seconds: float = 40.0) -> None:
    """A session whose seat 1 samples signal(u + shift_s) at its own time u,
    with Wi-Fi delay, an unrelated sensor clock, and no-echo spikes."""
    rng = random.Random(seed)
    path.mkdir(parents=True)
    w = SessionWriter(path)
    for i in range(int(seconds * 20)):
        u = i / 20
        d = 1030 if rng.random() < 0.03 else signal(u + shift_s)
        delay = rng.uniform(0.004, 0.040)
        w.write(MqttMsg(tm0 + int((u + delay) * SEC), TW0 + u, "rower/1/seat", 0,
                        False, seat(i + 1, clock_ms + round(u * 1000), int(d))))
    w.close()
    write_meta(path, {"format": 1, "start": {"tm": tm0, "tw": TW0},
                      "end": {"tm": tm0 + int(seconds * SEC), "tw": TW0 + seconds}})


def test_auto_finds_the_offset_that_lines_the_strokes_up(tmp_path):
    s = stroke_signal()
    write_rowing(tmp_path / "A", s, 0.0, tm0=1000 * SEC, clock_ms=5_000, seed=1)
    write_rowing(tmp_path / "B", s, -7.3, tm0=2600 * SEC, clock_ms=900_000, seed=2)
    _, sessions, members, _ = build([tmp_path / "A", tmp_path / "B"],
                                    "--offset", "auto", "--no-video")
    try:
        sug = members[1].auto
        assert sug is not None
        assert members[1].offset_s == pytest.approx(-7.3, abs=0.05)
        assert sug.r > 0.9
        assert all(r < sug.r for _, r in sug.others)
    finally:
        for x in sessions:
            x.close()


def test_auto_needs_the_traces_to_overlap():
    a = [(i / 20, 500.0 + (i % 40)) for i in range(200)]       # 10 s
    b = [(100 + i / 20, 500.0 + (i % 40)) for i in range(200)]
    assert mix.suggest_offset(a, b, search_s=5.0) is None


# ---- the command ------------------------------------------------------- #

def test_a_dry_run_sends_nothing(tmp_path, monkeypatch):
    a, b = two_sessions(tmp_path)

    def boom(self):
        raise AssertionError("a dry run opened an output")
    monkeypatch.setattr(engine.Outputs, "open", boom)
    assert mix.main([str(a), str(b), "--offset", "0.5", "--dry-run"]) == 0


class FakeMqtt:
    def __init__(self) -> None:
        self.sent: list = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((time.perf_counter_ns(), topic, payload, retain))
        return SimpleNamespace(rc=0)

    def disconnect(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass


def _with_fake_broker(monkeypatch) -> FakeMqtt:
    fake = FakeMqtt()
    orig = engine.Outputs.open

    def open_(self):
        no = self.args.no_mqtt
        self.args.no_mqtt = True
        try:
            orig(self)
        finally:
            self.args.no_mqtt = no
        if not no:
            self.mqtt = fake
    monkeypatch.setattr(engine.Outputs, "open", open_)
    return fake


@pytest.mark.parametrize("offset", [0.4, -0.4])
def test_two_rowers_come_out_on_their_own_topics(tmp_path, monkeypatch, offset):
    a, b = two_sessions(tmp_path)
    fake = _with_fake_broker(monkeypatch)
    engine.STOP.clear()
    assert mix.main([str(a), str(b), "--offset", str(offset), "--no-video",
                     "--lead-in-s", "0", "--stats-interval", "0"]) == 0
    topics = {t for _, t, _, _ in fake.sent}
    assert topics == {"rower/1/seat", "rower/1/status", "rower/2/seat",
                      "rower/2/status", "boat/imu"}
    status2 = [json.loads(p) for _, t, p, _ in fake.sent if t == "rower/2/status"]
    assert status2 and all(s["seat"] == 2 for s in status2)
    with Session(a) as sa:
        n = sum(1 for ev in sa.mqtt() if ev.source == "rower/1/seat")
    for t in ("rower/1/seat", "rower/2/seat"):
        assert sum(1 for x in fake.sent if x[1] == t) == n
    first = {t: next(x[0] for x in fake.sent if x[1] == t)
             for t in ("rower/1/seat", "rower/2/seat")}
    gap = (first["rower/2/seat"] - first["rower/1/seat"]) / 1e9
    assert gap == pytest.approx(offset, abs=0.025)     # coarse Windows timers


def test_two_cameras_come_out_on_their_own_ports(tmp_path):
    a, b = two_sessions(tmp_path)
    p1, p2, pa = _free_port(), _free_port(), _free_port()
    got1: list = []
    got2: list = []
    gota: list = []
    stop = threading.Event()
    ths = [threading.Thread(target=_receive, args=(f"tcp://127.0.0.1:{p}", out, stop))
           for p, out in ((p1, got1), (p2, got2), (pa, gota))]
    for th in ths:
        th.start()
    engine.STOP.clear()
    mix.main([str(a), str(b), "--offset", "0.3", "--no-mqtt", "--video-bind",
              f"cam1=tcp://127.0.0.1:{p1},cam2=tcp://127.0.0.1:{p2}",
              "--apriltag", f"tcp://127.0.0.1:{pa}", "--lead-in-s", LEAD_IN,
              "--stats-interval", "0"])
    time.sleep(0.2)
    stop.set()
    for th in ths:
        th.join()
    with Session(a) as sa, Session(b) as sb:
        fa, fb = list(sa.frames("cam1")), list(sb.frames("cam1"))
        ja = [sa.jpeg(e) for e in fa]
        jb = [sb.jpeg(e) for e in fb]
    assert [p[-1] for _, p in got1] == ja
    assert [p[-1] for _, p in got2] == jb
    assert (got2[0][0] - got1[0][0]) / 1e9 == pytest.approx(0.3, abs=0.025)
    cams = [json.loads(p[1])["cam"] for _, p in gota if p[0] == b"apriltag"]
    assert cams.count("cam1") == cams.count("cam2") == len(fa)
    assert not [p for _, p in gota if p[0] == b"apriltag_meta"]   # idle cam2's
