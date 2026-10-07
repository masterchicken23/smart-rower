"""Mapping the camera's clock onto the host's."""

from __future__ import annotations

from april_detect.clock import ClockEstimator, stamp
from april_detect.wire import FrameHeader

OFFSET = 1_760_000_000.0        # host wall - sender monotonic, the true value


def feed(est, sender_s, delay_s, recv_mono):
    return est.update(sender_s, recv_mono, sender_s + OFFSET + delay_s)


def test_minimum_offset_removes_network_jitter():
    est = ClockEstimator()
    for i, delay in enumerate([0.030, 0.004, 0.050, 0.012]):
        best = feed(est, 10.0 + i * 0.066, delay, i * 0.066)
    assert abs(best - (OFFSET + 0.004)) < 1e-9


def test_window_forgets_old_minimum():
    est = ClockEstimator(window_s=1.0)
    feed(est, 0.0, 0.001, 0.0)
    for i in range(1, 40):
        best = feed(est, i * 0.1, 0.010, i * 0.1)
    assert abs(best - (OFFSET + 0.010)) < 1e-9


def test_sender_reboot_resets_instead_of_holding_stale_offset():
    est = ClockEstimator()
    feed(est, 5000.0, 0.002, 0.0)
    # Reboot: sender clock restarts near zero, so the true offset jumps up.
    best = est.update(3.0, 0.1, 5000.1 + OFFSET)
    assert est.n_resets == 1
    assert abs(best - (5000.1 + OFFSET - 3.0)) < 1e-9


def test_stamp_estimated_uses_send_instant_for_offset():
    est = ClockEstimator()
    h = FrameHeader(t_ms=1000.0, t_send_ms=1020.0)   # 20 ms encode
    wall = 1.020 + OFFSET + 0.003                    # 3 ms network
    st = stamp(h, est, 0.0, wall)
    assert st.clock == "est"
    # first frame is the fastest seen, so net reads 0 and cap = recv - 20 ms
    # float64 resolves epoch seconds to ~0.24 us, hence 1e-6
    assert abs((st.t_send - st.t_cap) - 0.020) < 1e-6
    assert abs(wall - st.t_send) < 1e-6


def test_stamp_ntp_is_believed():
    st = stamp(FrameHeader(t=OFFSET, t_ms=0.0, t_send_ms=15.0),
               ClockEstimator(), 0.0, OFFSET + 0.05)
    assert st.clock == "ntp" and st.t_cap == OFFSET
    assert abs(st.t_send - (OFFSET + 0.015)) < 1e-6


def test_stamp_without_timing():
    st = stamp(FrameHeader(cam="x"), ClockEstimator(), 0.0, OFFSET)
    assert (st.clock, st.t_cap, st.t_send) == ("recv", None, None)
