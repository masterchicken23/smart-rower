"""recorder.export: frame times from the cameras' capture clocks, and the MP4
they make (that part only where an ffmpeg is found)."""

from __future__ import annotations

import random
import re
import subprocess
from pathlib import Path

import pytest

from recorder import export
from recorder.format import FrameMsg, Session, SessionWriter, write_meta

from .helpers import JPEG, SEC, cam_pre

TW0 = 1_760_000_000.0


def write_frames(path: Path, rows: list[tuple], cam: str = "cam1",
                 jpeg: bytes = JPEG) -> None:
    """rows: (tm ns, tw s, t_ms, t_send_ms) in arrival order; t_ms None
    writes a header with no clock, as a legacy sender's."""
    path.mkdir(parents=True, exist_ok=True)
    w = SessionWriter(path)
    for i, (tm, tw, t_ms, t_send) in enumerate(rows):
        pre = (cam_pre(i, t_ms, t_send, cam=cam) if t_ms is not None
               else [b'{"msg": {"cam": "%s", "seq": %d}}' % (cam.encode(), i)])
        w.write(FrameMsg(cam, tm, tw, pre, jpeg))
    w.close()


def jetson_rows(caps_ms: list[float], delays_ms: list[float],
                origin_ms: float) -> list[tuple]:
    """Frames captured at `caps_ms` (camera clock), arriving in order on the
    Jetson's clock after `delays_ms`, as Wi-Fi delivers them: a late frame
    holds up the ones behind it."""
    rows, prev = [], 0
    for c, d in zip(caps_ms, delays_ms, strict=True):
        tm = max(prev, 1000 * SEC + int((c - origin_ms + d) * 1e6))
        rows.append((tm, TW0 + tm / 1e9, c, c + 0.4))
        prev = tm
    return rows


def rel(tl: export.Timeline) -> list[float]:
    return [t - tl.frames[0][0] for t, _ in tl.frames]


def test_frames_are_timed_by_capture_not_arrival(tmp_path):
    lost = {20, 21, 22}                                   # lost on Wi-Fi
    caps = [50_000.0 + 68.0 * i + (i % 7 == 3) * -36.0   # some 32 ms steps
            for i in range(60) if i not in lost]
    rng = random.Random(1)
    delays = [rng.choice([4, 4, 30, 90, 160]) for _ in caps]
    write_frames(tmp_path, jetson_rows(caps, delays, 50_000.0))

    with Session(tmp_path) as sess:
        tl = export.timeline(sess, "cam1")
    assert len(tl.frames) == len(caps)
    assert tl.pieces == 1 and tl.untimed == tl.duplicate == 0
    assert rel(tl) == pytest.approx([(c - caps[0]) / 1000 for c in caps],
                                    abs=1e-9)
    steps = [b - a for a, b in zip(rel(tl), rel(tl)[1:], strict=False)]
    assert max(steps) == pytest.approx(4 * 0.068)        # the hole is kept


def test_a_spell_of_slow_wifi_does_not_change_the_frame_rate(tmp_path):
    # 60 s, the last 10 s of it with no frame through in under 240 ms: that
    # window's least delay is not the clock offset, and a mapping that follows
    # window minima would bend the frame rate (it stretched a real session's
    # last 9 s by 3 s). The camera's own spacing has to come through as is.
    caps = [50_000.0 + 68.0 * i for i in range(882)]
    rng = random.Random(3)
    delays = [rng.uniform(2, 80) if c - caps[0] < 50_100 else rng.uniform(240, 900)
              for c in caps]
    write_frames(tmp_path, jetson_rows(caps, delays, 50_000.0))

    with Session(tmp_path) as sess:
        tl = export.timeline(sess, "cam1")
    assert len(tl.frames) == len(caps)
    assert rel(tl) == pytest.approx([(c - caps[0]) / 1000 for c in caps],
                                    abs=1e-6)


def test_camera_reboot_keeps_order_on_the_jetson_clock(tmp_path):
    before = [900_000.0 + 68.0 * i for i in range(30)]
    after = [3_000.0 + 68.0 * j for j in range(30)]       # clock restarted
    rows = jetson_rows(before, [10.0] * 30, 900_000.0)
    reboot_ns = rows[-1][0] + 5 * SEC
    rows += [(reboot_ns + int(68e6 * j), TW0 + (reboot_ns + int(68e6 * j)) / 1e9,
              c, c + 0.4) for j, c in enumerate(after)]
    write_frames(tmp_path, rows)

    with Session(tmp_path) as sess:
        tl = export.timeline(sess, "cam1")
    assert tl.pieces == 2
    assert [ev.tm for _, ev in tl.frames] == [r[0] for r in rows]
    # Across the reboot the frames are as far apart as they arrived.
    assert tl.frames[30][0] - tl.frames[29][0] == pytest.approx(
        (rows[30][0] - rows[29][0]) / 1e9, abs=1e-3)


@pytest.mark.parametrize(("wall_gap_s", "expect_s"), [
    (40.0, 40.0),          # the camera's wall clock is right after its reboot
    (-3600.0, 0.068),      # no RTC: it came back an hour early -- one frame
])
def test_cameras_own_recording_joins_a_restarted_clock_by_wall_time(
        tmp_path, wall_gap_s, expect_s):
    # cam-sender's own session: tm is t_send_ms on the camera's clock, which
    # starts again when it reboots and resumes the session.
    rows = []
    for i in range(30):
        c = 2_000_000.0 + 68.0 * i
        rows.append((int((c + 0.4) * 1e6), TW0 + i * 0.068, c, c + 0.4))
    tw_end = rows[-1][1]
    for j in range(30):
        c = 25_000.0 + 68.0 * j
        rows.append((int((c + 0.4) * 1e6), tw_end + wall_gap_s + j * 0.068,
                     c, c + 0.4))
    write_frames(tmp_path, rows, cam="cam-pi")

    with Session(tmp_path) as sess:
        tl = export.timeline(sess, "cam-pi")
    assert tl.pieces == 2 and len(tl.frames) == 60
    assert [ev.tm for _, ev in tl.frames] == [r[0] for r in rows]
    assert tl.frames[30][0] - tl.frames[29][0] == pytest.approx(expect_s,
                                                                abs=1e-6)


def test_a_sender_with_no_clock_is_timed_by_arrival(tmp_path):
    tms = [1000 * SEC + int(i * 71e6) for i in range(10)]
    write_frames(tmp_path, [(tm, TW0, None, None) for tm in tms])
    with Session(tmp_path) as sess:
        tl = export.timeline(sess, "cam1")
    assert [t for t, _ in tl.frames] == [tm / 1e9 for tm in tms]


def test_a_damaged_frame_is_left_out(tmp_path):
    caps = [50_000.0 + 68.0 * i for i in range(10)]
    write_frames(tmp_path, jetson_rows(caps, [5.0] * 10, 50_000.0))
    with Session(tmp_path) as sess:
        third = list(sess.frames("cam1"))[3]
    with open(tmp_path / "cam1.mjpeg", "r+b") as f:
        f.seek(third.rec["off"] + 4)
        f.write(b"\x00")

    out = tmp_path / "jpegs"
    out.mkdir()
    with Session(tmp_path) as sess:
        tl = export.timeline(sess, "cam1")
        kept, corrupt = export.write_frames(sess, tl, out)
    assert corrupt == 1 and len(kept) == 9
    assert all(p.read_bytes() == JPEG for _, p in kept)
    assert tl.frames[3][0] not in [t for t, _ in kept]


def test_concat_durations_never_accumulate_rounding():
    rng = random.Random(2)
    t, times = 1234.567891234, []
    for _ in range(5000):
        times.append(t)
        t += rng.choice([0.0679876, 0.0320123, 0.1360456])
    frames = [(x, Path(f"/f/{i:07d}.jpg")) for i, x in enumerate(times)]
    script = export.concat_list(frames, 0.068)

    assert script.count(f"option framerate {export.TIMEBASE}") == len(frames)
    durs = [float(x) for x in re.findall(r"^duration (\S+)$", script, re.M)]
    at = 0
    for i, d in enumerate(durs):
        assert at == round((times[i] - times[0]) * 1e6)
        at += round(d * 1e6)
    assert durs[-1] == 0.068


def test_quotes_in_a_path_are_escaped():
    script = export.concat_list([(0.0, Path("/tmp/it's.jpg"))], 0.068)
    assert "file '/tmp/it'\\''s.jpg'" in script


def test_output_is_named_after_the_session(tmp_path):
    one, two = tmp_path / "20261110T000213Z", tmp_path / "20261110T002731Z"
    rows = jetson_rows([0.0, 68.0], [5.0, 5.0], 0.0)
    write_frames(one, rows, cam="cam-pi")
    write_frames(two, rows, cam="cam1")
    write_frames(two, rows, cam="cam2")
    with Session(one) as a, Session(two) as b:
        assert export.out_path(a, "cam-pi") == one / "20261110T000213Z.mp4"
        assert export.out_path(b, "cam2") == two / "20261110T002731Z_cam2.mp4"


# ---- with ffmpeg -------------------------------------------------------- #

FFMPEG = export.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg found")


@needs_ffmpeg
def test_mp4_shows_each_frame_at_its_capture_time(tmp_path, capsys):
    jpeg = subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x48",
         "-frames:v", "1", "-c:v", "mjpeg", "-f", "image2pipe", "-"],
        capture_output=True, check=True).stdout
    caps = [0.0, 68.0, 136.0, 168.0, 236.0, 508.0, 576.0, 644.0, 676.0]
    # Arriving with no delay: sync.py reads a device's least delay as zero
    # anyway, so this keeps the expected start offset exact.
    rows = [(1000 * SEC + int(c * 1e6), TW0, 70_000.0 + c, 70_000.0 + c)
            for c in caps]
    session = tmp_path / "20261110T000213Z"
    write_frames(session, rows, jpeg=jpeg)
    # The session started 40 ms before the first frame was captured.
    write_meta(session, {"start": {"tm": 1000 * SEC - 40_000_000,
                                   "tw": TW0}})

    assert export.main([str(session), "--preset", "ultrafast"]) == 0
    out = session / "20261110T000213Z.mp4"
    assert out.is_file()
    assert not list(session.glob("*.part.mp4"))
    said = capsys.readouterr().out
    assert "9 frames" in said and "+0.040 s from session start" in said

    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out), "-vf",
                            "showinfo", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    assert re.search(r"Video: h264 .*yuv420p\(tv", probe)
    pts = [float(x) * 1000 for x in re.findall(r"pts_time:([0-9.]+)", probe)]
    assert pts == pytest.approx(caps, abs=1.0)
