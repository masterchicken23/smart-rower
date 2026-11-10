"""A camera's own recording put on a Jetson session's clock: finding it, the
least-delay mapping (frames the Jetson lost filled in, none placed later than
it arrived), a camera reboot, and playing it with recorder.play --cam-copy."""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path

import pytest

from recorder import play as play_mod
from recorder.camcopy import attach, find_copies
from recorder.format import FrameMsg, Session, SessionWriter, frame_header, write_meta
from recorder.play import parse_args

from .helpers import JPEG, SEC, cam_pre, write_cam_copy
from .test_play import LEAD_IN, _free_port, _receive

TM0 = 1000 * SEC
TW0 = 1_760_000_000.0
NAME = "20261110T000213Z"


def sent_rows(n: int, t0_ms: float = 50_000.0, seq0: int = 100) -> list[tuple]:
    """(seq, t_ms, t_send_ms) of `n` frames a camera sent, ~68 ms apart."""
    return [(seq0 + 2 * i, t0_ms + 68.0 * i, t0_ms + 68.0 * i + 0.4)
            for i in range(n)]


def write_jetson(path: Path, runs: list[tuple[list[tuple], int]],
                 lost: set[int], seed: int = 1, end_s: float = 60.0) -> list:
    """The Jetson's copy of the frames in `runs` -- (rows, Jetson tm of
    t_send 0 on that run's camera clock) -- less `lost` (seq), each arriving
    3-120 ms after it was sent and in order, as Wi-Fi delivers them. The
    first frame of each run gets the least delay. Returns (seq, t_send, tm)
    of the frames written."""
    path.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    w = SessionWriter(path)
    out, prev = [], 0
    for rows, base in runs:
        for i, (seq, t_ms, t_send) in enumerate(rows):
            if seq in lost:
                continue
            delay = 3.0 if i == 0 else rng.uniform(3.5, 120.0)
            tm = max(prev + 1, base + int(round((t_send + delay) * 1e6)))
            w.write(FrameMsg("cam1", tm, TW0 + (tm - TM0) / 1e9,
                             cam_pre(seq, t_ms, t_send, cam="cam-pi"),
                             JPEG + bytes([seq % 256])))
            out.append((seq, t_send, tm))
            prev = tm
    w.close()
    write_meta(path, {"format": 1, "name": path.name,
                      "start": {"tm": TM0, "tw": TW0},
                      "end": {"tm": TM0 + int(end_s * SEC), "tw": TW0 + end_s}})
    return out


def test_copies_are_found_in_all_three_layouts(tmp_path):
    write_cam_copy(tmp_path / "pi" / "S1", sent_rows(3), name="S1")
    write_cam_copy(tmp_path / "pi" / "S2", sent_rows(3), name="S2")
    write_jetson(tmp_path / "data" / "S1", [(sent_rows(3), TM0)], set())
    write_cam_copy(tmp_path / "data" / "S1" / "cam1", sent_rows(3), name="S1")

    assert [p.name for p in find_copies([tmp_path / "pi"])] == ["S1", "S2"]
    assert [p.name for p in find_copies([tmp_path / "pi" / "S2"])] == ["S2"]
    assert find_copies([tmp_path / "data" / "S1"]) == [tmp_path / "data" / "S1" / "cam1"]
    assert find_copies([tmp_path / "pi", tmp_path / "pi" / "S1"]) == find_copies(
        [tmp_path / "pi"])                                  # no duplicates
    with pytest.raises(FileNotFoundError):
        find_copies([tmp_path / "nowhere"])


def test_the_copy_fills_lost_frames_and_lands_by_least_delay(tmp_path):
    rows = sent_rows(60)
    base = TM0 - int(50_000 * 1e6)          # camera clock 50 s ahead
    lost = {rows[10][0], rows[11][0], rows[40][0]}
    jet = write_jetson(tmp_path / NAME, [(rows, base)], lost)
    write_cam_copy(tmp_path / "pi" / NAME, rows, name=NAME)

    with Session(tmp_path / NAME) as sess:
        copies = attach(sess, find_copies([tmp_path / "pi"]), ["cam1"])
        cc = copies["cam1"]
        frames = list(cc.frames())
        assert (cc.n_common, cc.n_jetson) == (57, 57)
        assert len(frames) == 60                            # lost ones back
        assert [frame_header(e.pre)["seq"] for e in frames] == [r[0] for r in rows]
        assert all(e.source == "cam1" for e in frames)
        # On the Jetson's clock: send time + the least delay (3 ms).
        assert cc.offsets == [base + 3_000_000]
        mapped = {frame_header(e.pre)["seq"]: e.tm for e in frames}
        assert all(mapped[s] <= tm for s, _, tm in jet)   # never after arrival
        assert mapped[jet[0][0]] == jet[0][2]               # least delay: equal
        assert [e.tm for e in frames] == sorted(e.tm for e in frames)
        # Wall time on the Jetson's clock, not the camera's.
        assert frames[0].rec["tw"] == pytest.approx(TW0 + (frames[0].tm - TM0) / 1e9)
        # The copy's bytes, CRC checked.
        assert cc.jpeg(frames[10]) == JPEG + bytes([rows[10][0] % 256])
        cc.close()


def test_a_camera_reboot_maps_each_run_on_its_own(tmp_path):
    run1 = sent_rows(30, t0_ms=50_000.0, seq0=100)
    run2 = sent_rows(30, t0_ms=1_000.0, seq0=0)            # clock restarted
    base1 = TM0 - int(50_000 * 1e6)
    base2 = TM0 + int(9 * SEC) - int(1_000 * 1e6)           # back 9 s in
    jet = write_jetson(tmp_path / NAME, [(run1, base1), (run2, base2)],
                       lost={run1[5][0], run2[7][0]})
    write_cam_copy(tmp_path / "copy", run1 + run2, name=NAME)

    with Session(tmp_path / NAME) as sess:
        cc = attach(sess, find_copies([tmp_path / "copy"]), ["cam1"])["cam1"]
        assert cc.offsets == [base1 + 3_000_000, base2 + 3_000_000]
        frames = list(cc.frames())
        assert len(frames) == 60
        assert [e.tm for e in frames] == sorted(e.tm for e in frames)
        by_key = {(frame_header(e.pre)["seq"], frame_header(e.pre)["t_send_ms"]): e.tm
                  for e in frames}
        assert all(by_key[(s, ts)] <= tm for s, ts, tm in jet)
        cc.close()


def test_frames_outside_the_jetson_session_are_left_out(tmp_path):
    rows = sent_rows(60)                    # 4 s of frames
    base = TM0 - int(50_000 * 1e6)
    write_jetson(tmp_path / NAME, [(rows[:30], base)], set(), end_s=2.0)
    write_cam_copy(tmp_path / "copy", rows, name=NAME)
    with Session(tmp_path / NAME) as sess:
        cc = attach(sess, find_copies([tmp_path / "copy"]), ["cam1"])["cam1"]
        frames = list(cc.frames())
        assert frames and frames[-1].tm <= TM0 + 2 * SEC
        assert len(frames) < 60
        cc.close()


def test_a_copy_with_nothing_in_common_is_not_used(tmp_path):
    base = TM0 - int(50_000 * 1e6)
    write_jetson(tmp_path / NAME, [(sent_rows(20), base)], set())
    write_cam_copy(tmp_path / "other", sent_rows(20, t0_ms=90_000.0), name=NAME)
    write_cam_copy(tmp_path / "late", sent_rows(20), name="20261110T002731Z")
    with Session(tmp_path / NAME) as sess:
        assert attach(sess, find_copies([tmp_path / "other"]), ["cam1"]) == {}
        assert attach(sess, find_copies([tmp_path / "late"]), ["cam1"]) == {}


def test_play_sends_the_copy_instead_of_the_jetsons_frames(tmp_path):
    rows = sent_rows(15)
    base = TM0 - int(50_000 * 1e6)
    write_jetson(tmp_path / NAME, [(rows, base)], {rows[3][0], rows[9][0]})
    write_cam_copy(tmp_path / "pi" / NAME, rows, name=NAME,
                   jpeg=lambda s: JPEG + b"copy" + bytes([s % 256]))
    vport = _free_port()
    got: list = []
    stop = threading.Event()
    th = threading.Thread(target=_receive, args=(f"tcp://127.0.0.1:{vport}", got, stop))
    th.start()
    args = parse_args([str(tmp_path / NAME), "--no-mqtt", "--video-bind",
                       f"cam1=tcp://127.0.0.1:{vport}", "--cam-copy",
                       str(tmp_path / "pi"), "--lead-in-s", LEAD_IN,
                       "--stats-interval", "0"])
    play_mod.STOP.clear()
    with Session(tmp_path / NAME) as sess:
        summary = play_mod.play(sess, args)
    time.sleep(0.2)
    stop.set()
    th.join()
    assert summary["sent"] == {"frame": 15}
    assert [p[-1] for _, p in got] == [JPEG + b"copy" + bytes([r[0] % 256]) for r in rows]
