"""The on-disk format: what the writer writes, the reader reads back --
including the awkward payloads and the damage a pulled battery leaves."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from recorder.format import (
    INDEX_EXT,
    MQTT,
    VIDEO_EXT,
    CorruptFrameError,
    FrameMsg,
    MqttMsg,
    Session,
    SessionWriter,
    clean_label,
    create_session_dir,
    frame_header,
    session_name,
    split_tee,
)

from .helpers import JPEG, SEC, write_session


def test_round_trip_keeps_payloads_byte_exact(tmp_path):
    w = SessionWriter(tmp_path)
    payloads = [b'{"seq":1,"t_ms":5}', b"", b"\xff\xfe not utf-8 \x00",
                "naïve ünïcode".encode()]
    for i, p in enumerate(payloads):
        w.write(MqttMsg(i * SEC, 1.5 + i, f"t/{i}", i % 2, bool(i % 2), p))
    w.close()
    with Session(tmp_path) as s:
        got = list(s.mqtt())
    assert [e.payload for e in got] == payloads
    assert [e.source for e in got] == [f"t/{i}" for i in range(4)]
    assert [e.rec["retain"] for e in got] == [False, True, False, True]
    assert [e.rec["qos"] for e in got] == [0, 1, 0, 1]


def test_frames_round_trip_with_header_and_tee_stamp(tmp_path):
    w = SessionWriter(tmp_path)
    pre = [b'{"msg": {"cam": "cam1", "seq": 7}}']
    w.write(FrameMsg("cam1", 5 * SEC, 9.0, pre, JPEG, up=(4 * SEC, 8.5)))
    w.write(FrameMsg("cam1", 6 * SEC, 10.0, pre, JPEG[::-1]))
    w.close()
    with Session(tmp_path) as s:
        assert s.cameras() == ["cam1"]
        fr = list(s.frames("cam1"))
        assert [s.jpeg(e) for e in fr] == [JPEG, JPEG[::-1]]
        assert fr[0].pre == pre
        assert fr[0].tm_arrival == 4 * SEC       # the tee's stamp wins
        assert fr[1].tm_arrival == 6 * SEC       # no tee: the recorder's


def test_events_merge_every_file_in_arrival_order(tmp_path):
    write_session(tmp_path, seconds=1.0)
    with Session(tmp_path) as s:
        evs = list(s.events())
        kinds = {e.kind for e in evs}
    tms = [e.tm for e in evs]
    assert tms == sorted(tms)
    assert kinds == {"mqtt", "frame", "apriltag"}


def test_truncated_video_tail_and_torn_line_are_tolerated(tmp_path):
    write_session(tmp_path, seconds=1.0)
    vid = tmp_path / ("cam1" + VIDEO_EXT)
    with Session(tmp_path) as s:
        n_frames = len(list(s.frames("cam1")))
        n_mqtt = len(list(s.mqtt()))
    # The battery goes: the last frame's bytes are half there, its index line
    # is not, and the MQTT log ends mid-line.
    with open(vid, "r+b") as f:
        f.truncate(vid.stat().st_size - 5)
    with open(tmp_path / MQTT, "a", encoding="utf-8") as f:
        f.write('{"tm": 99, "tw": 1.0, "topic": "rower/1/se')
    with Session(tmp_path) as s:
        frames = list(s.frames("cam1"))
        assert len(frames) == n_frames - 1
        assert all(len(s.jpeg(e)) == e.rec["len"] for e in frames)
        assert len(list(s.mqtt())) == n_mqtt


def test_index_lines_point_inside_the_video_file(tmp_path):
    write_session(tmp_path, seconds=0.5)
    size = (tmp_path / ("cam1" + VIDEO_EXT)).stat().st_size
    offs = [json.loads(line) for line in
            (tmp_path / ("cam1" + INDEX_EXT)).read_text().splitlines()]
    assert offs[0]["off"] == 0
    for a, b in zip(offs, offs[1:], strict=False):
        assert b["off"] == a["off"] + a["len"]
    assert offs[-1]["off"] + offs[-1]["len"] == size


def test_session_names_are_datetimes_and_never_reused(tmp_path):
    now = datetime(2026, 11, 7, 14, 30, 0, tzinfo=UTC)
    assert session_name(now) == "20261107T143000Z"
    assert session_name(now, "pause drill/1") == "20261107T143000Z_pause-drill-1"
    a = create_session_dir(tmp_path, now=now)
    b = create_session_dir(tmp_path, now=now)
    c = create_session_dir(tmp_path, "x", now=now)
    assert (a.name, b.name, c.name) == ("20261107T143000Z", "20261107T143000Z-2",
                                        "20261107T143000Z_x")
    assert clean_label("  ../../etc  ") == "etc"


def test_frame_header_reads_every_sender_shape():
    full = {"v": 1, "cam": "cam1", "seq": 3, "t_ms": 1.5}
    assert frame_header([json.dumps({"msg": full}).encode()]) == full
    assert frame_header([json.dumps(full).encode()]) == full
    assert frame_header([json.dumps({"msg": json.dumps(full)}).encode()]) == full
    assert frame_header([json.dumps({"msg": "picam"}).encode()]) == {"cam": "picam"}
    assert frame_header([b"topic-cam"]) == {"cam": "topic-cam"}
    assert frame_header([]) == {}


def test_split_tee():
    own = [b'{"msg": {}}', JPEG]
    parts, up = split_tee([b"tee1", b'{"t_mono": 12.5, "t_wall": 1700.25}', *own])
    assert parts == own and up == (12_500_000_000, 1700.25)
    parts, up = split_tee(own)
    assert parts == own and up is None


# ---- damage: what one bad frame or line may cost (only itself) ---------- #

class _FailsHalfway:
    """A video file whose next write lands half its bytes, then errors -- an
    SD card hiccup, or the disk filling."""

    def __init__(self, f):
        self.f = f
        self.fail_next = False

    def write(self, b):
        if self.fail_next:
            self.fail_next = False
            self.f.write(bytes(b[: len(b) // 2]))
            raise OSError(5, "Input/output error")
        return self.f.write(b)

    def fileno(self):
        return self.f.fileno()

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.close()


def _frame(i: int) -> FrameMsg:
    return FrameMsg("cam1", i * SEC, float(i), [b'{"msg":{"seq":%d}}' % i],
                    JPEG + bytes([i]) * (100 + i))


def test_a_failed_write_does_not_shift_later_frames(tmp_path):
    w = SessionWriter(tmp_path)
    w.write(_frame(0))
    flaky = w._video["cam1"] = _FailsHalfway(w._video["cam1"])
    flaky.fail_next = True
    with pytest.raises(OSError):
        w.write(_frame(1))              # half of it is now in the file
    for i in (2, 3):
        w.write(_frame(i))
    w.close()
    with Session(tmp_path) as s:
        got = [(e.rec["tm"] // SEC, s.jpeg(e)) for e in s.frames("cam1")]
    # Frame 1 is gone; every other frame reads back exactly, CRC and all.
    assert got == [(i, _frame(i).jpeg) for i in (0, 2, 3)]


def _rewrite_index(path, fn):
    idx = path / ("cam1" + INDEX_EXT)
    lines = [json.loads(x) for x in idx.read_text().splitlines()]
    idx.write_text("".join(json.dumps(fn(i, rec)) + "\n"
                           for i, rec in enumerate(lines)))


def test_crc_catches_a_frame_damaged_on_disk(tmp_path):
    w = SessionWriter(tmp_path)
    for i in range(5):
        w.write(_frame(i))
    w.close()
    with Session(tmp_path) as s:
        bad = list(s.frames("cam1"))[2]
    vid = tmp_path / ("cam1" + VIDEO_EXT)
    with open(vid, "r+b") as f:             # one flipped byte inside frame 2
        f.seek(bad.rec["off"] + 50)
        b = f.read(1)
        f.seek(bad.rec["off"] + 50)
        f.write(bytes([b[0] ^ 0xFF]))
    with Session(tmp_path) as s:
        frames = list(s.frames("cam1"))
        assert len(frames) == 5
        for i, e in enumerate(frames):
            if i == 2:
                with pytest.raises(CorruptFrameError):
                    s.jpeg(e)
                assert len(s.jpeg(e, verify=False)) == e.rec["len"]
            else:
                assert s.jpeg(e) == _frame(i).jpeg


def test_sessions_without_crc_still_read(tmp_path):
    w = SessionWriter(tmp_path)
    for i in range(3):
        w.write(_frame(i))
    w.close()
    _rewrite_index(tmp_path, lambda i, rec: {k: v for k, v in rec.items()
                                             if k != "crc"})
    with Session(tmp_path) as s:
        assert [s.jpeg(e) for e in s.frames("cam1")] == [_frame(i).jpeg
                                                          for i in range(3)]


def test_a_bad_index_line_mid_file_costs_only_its_frame(tmp_path):
    w = SessionWriter(tmp_path)
    for i in range(6):
        w.write(_frame(i))
    w.close()
    # Line 1 points past the end of the video; line 3 is not JSON at all.
    _rewrite_index(tmp_path, lambda i, rec: dict(rec, off=10**12) if i == 1 else rec)
    idx = tmp_path / ("cam1" + INDEX_EXT)
    lines = idx.read_text().splitlines(keepends=True)
    lines[3] = lines[3][:20] + "#garbage\n"
    idx.write_text("".join(lines))
    with Session(tmp_path) as s:
        got = [s.jpeg(e) for e in s.frames("cam1")]
    assert got == [_frame(i).jpeg for i in (0, 2, 4, 5)]
