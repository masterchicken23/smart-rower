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
