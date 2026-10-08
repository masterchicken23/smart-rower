"""Backends that can run without a camera: clock detection, the quality servo,
and the synthetic source end to end."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from cam_sender import clock as clk
from cam_sender.backends import QualityController, SyntheticBackend


def fixed(name: str, ns: int) -> clk.Clock:
    return clk.Clock(name, lambda: ns)


def test_detect_picks_the_clock_the_sensor_is_on():
    mono, boot = fixed("monotonic", 10**12), fixed("boottime", 10**12 + 5 * 10**9)
    assert clk.detect(10**12 - 30_000_000, [mono, boot]) is mono
    assert clk.detect(10**12 + 5 * 10**9 - 30_000_000, [mono, boot]) is boot


def test_detect_tie_goes_to_first_and_mismatch_is_none():
    a, b = fixed("monotonic", 10**12), fixed("boottime", 10**12)
    assert clk.detect(10**12, [a, b]) is a
    assert clk.detect(10**12 + 2 * clk.MATCH_NS, [a, b]) is None


def test_quality_servo_moves_toward_budget_within_bounds():
    qc = QualityController(60, 10_000, 15, 92)
    for _ in range(100):
        qc.update(50_000)
    assert qc.q == 15
    for _ in range(100):
        qc.update(1_000)
    assert qc.q == 92
    fixedq = QualityController(60, None, 15, 92)
    fixedq.update(10**9)
    assert fixedq.q == 60


@pytest.mark.parametrize("color", [False, True])
def test_synthetic_frames(color):
    pytest.importorskip("numpy")
    args = SimpleNamespace(width=320, height=240, fps=200.0, color=color,
                           quality=60, target_kb=0.0, qmin=15, qmax=92)
    try:
        frames = SyntheticBackend(args, threading.Event()).frames()
        got = [next(frames) for _ in range(3)]
    except SystemExit as e:                     # neither simplejpeg nor cv2
        pytest.skip(str(e))
    frames.close()
    assert all(f.jpeg.startswith(b"\xff\xd8") for f in got)
    assert got[0].t_ms < got[1].t_ms < got[2].t_ms
    assert all(f.skipped == 0 and f.enc_ms is not None for f in got)


class _FakeCapture:
    """A 30 fps MJPEG webcam stamped from 10**12 ns. The driver drops frame
    7 (a sequence gap) and flags frame 9 as corrupt."""

    card, width, height, fps = "fake", 1280, 720, 30.0

    def __init__(self, *_a, **_k) -> None:
        self.frames = [(b"\xff\xd8" + bytes([i]), 10**12 + i * 33_333_333, i,
                        i == 9) for i in range(14) if i != 7]
        self.ctrls: dict[int, int] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def ctrl_range(self, _cid):
        return (0, 100)

    def set_ctrl(self, cid, value):
        self.ctrls[cid] = value

    def start(self):
        pass

    def read(self, _timeout):
        return self.frames.pop(0)


def test_uvc_passes_jpeg_through_and_decimates(monkeypatch):
    from cam_sender import backends, v4l2

    cams: list[_FakeCapture] = []
    monkeypatch.setattr(v4l2, "Capture",
                        lambda *a, **k: cams.append(_FakeCapture()) or cams[-1])
    monkeypatch.setattr(clk, "candidates",
                        lambda: [fixed("monotonic", 10**12)])
    args = SimpleNamespace(device="/dev/video0", width=1280, height=720,
                           fps=15.0, color=False, hflip=False, vflip=False)
    frames = backends.UvcBackend(args, threading.Event()).frames()
    got = [next(frames) for _ in range(6)]
    frames.close()
    # Every other frame; 8 stands in for the dropped 7 one period late, and
    # the corrupt 9 is skipped, so 10 follows on time.
    assert [f.jpeg[2] for f in got] == [0, 2, 4, 6, 8, 10]
    assert [f.skipped for f in got] == [0, 1, 1, 1, 1, 1]
    assert got[0].t_ms == 10**12 / 1e6
    assert cams[0].ctrls == {v4l2.CID_SATURATION: 0}


def test_v4l2_layouts_match_the_kernel_ioctls():
    import ctypes

    from cam_sender import v4l2

    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("reference numbers below are for 64-bit")
    # From linux/videodev2.h as compiled on x86_64 / aarch64.
    assert v4l2.VIDIOC_QUERYCAP == 0x80685600
    assert v4l2.VIDIOC_S_FMT == 0xC0D05605
    assert v4l2.VIDIOC_REQBUFS == 0xC0145608
    assert v4l2.VIDIOC_QUERYBUF == 0xC0585609
    assert v4l2.VIDIOC_QBUF == 0xC058560F
    assert v4l2.VIDIOC_DQBUF == 0xC0585611
    assert v4l2.VIDIOC_STREAMON == 0x40045612
    assert v4l2.VIDIOC_S_PARM == 0xC0CC5616
    assert v4l2.VIDIOC_S_CTRL == 0xC008561C
    assert v4l2.VIDIOC_QUERYCTRL == 0xC0445624
