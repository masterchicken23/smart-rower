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


class _FakeRequest:
    def __init__(self, ts: int, data: bytes) -> None:
        self.ts, self.data = ts, data

    def get_metadata(self):
        return {"SensorTimestamp": self.ts}

    def make_buffer(self, _name):
        import numpy as np
        return np.frombuffer(self.data, dtype=np.uint8)

    def release(self):
        pass


class _FakeUvc:
    """A 30 fps MJPEG camera, stamped from 10**12 ns."""

    def __init__(self, n: int) -> None:
        self.reqs = [_FakeRequest(10**12 + i * 33_333_333, b"\xff\xd8" + bytes([i]))
                     for i in range(n)]

    def start(self): pass
    def stop(self): pass
    def close(self): pass

    def capture_request(self):
        return self.reqs.pop(0)


def test_uvc_passes_jpeg_through_and_decimates(monkeypatch):
    pytest.importorskip("numpy")
    from cam_sender import backends

    cam = _FakeUvc(12)
    monkeypatch.setattr(backends, "_configure", lambda *_a, **_k: cam)
    monkeypatch.setattr(clk, "candidates",
                        lambda: [fixed("monotonic", 10**12)])
    args = SimpleNamespace(width=1280, height=720, fps=15.0, color=False)
    frames = backends.UvcBackend(args, threading.Event()).frames()
    got = [next(frames) for _ in range(5)]
    frames.close()
    assert [f.jpeg[2] for f in got] == [0, 2, 4, 6, 8]
    assert [f.skipped for f in got] == [0, 1, 1, 1, 1]
    assert all(b - a == pytest.approx(66.67, abs=0.1)
               for a, b in zip([f.t_ms for f in got], [f.t_ms for f in got][1:]))
