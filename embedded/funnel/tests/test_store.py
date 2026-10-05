#!/usr/bin/env python3
"""Buffering: eviction, time windows, rate measurement, drop accounting."""

from __future__ import annotations

from funnel.models import Sample, StreamKey
from funnel.store import ClockEstimator, StreamStore, is_stale


def sample(t, seq=None, d=0, dev=None):
    return Sample(t_recv=t, t_src=None, seq=seq, values={"d_mm": d}, dev=dev)


def test_maxlen_evicts_oldest():
    """The hard cap: an unexpectedly fast publisher must not grow memory."""
    store = StreamStore(maxlen=5, window_s=1000)
    key = StreamKey("rower", 1, "seat")
    for i in range(20):
        store.push(key, sample(i * 0.01, d=i))
    buf = store.buffers[key]
    assert len(buf.samples) == 5
    assert buf.latest.values["d_mm"] == 19
    assert buf.n_recv == 20          # counters survive eviction


def test_window_trims_by_age():
    """The soft cap: history older than the window is not kept, so a fast
    publisher cannot push the window's worth out of maxlen's reach."""
    store = StreamStore(maxlen=10_000, window_s=1.0)
    key = StreamKey("boat", None, "imu")
    for i in range(300):
        store.push(key, sample(i * 0.01))        # 3 s at 100 Hz
    buf = store.buffers[key]
    assert len(buf.samples) <= 101
    assert buf.samples[0].t_recv >= 1.98


def test_window_returns_requested_span_oldest_first():
    store = StreamStore(maxlen=1000, window_s=10)
    key = StreamKey("rower", 2, "seat")
    for i in range(100):
        store.push(key, sample(i * 0.05, d=i))   # 5 s at 20 Hz
    buf = store.buffers[key]
    got = buf.window(1.0, now=4.95)
    assert len(got) == 21
    assert got[0].t_recv <= got[-1].t_recv
    assert got[-1].values["d_mm"] == 99


def test_window_is_empty_not_an_error_before_any_data():
    store = StreamStore()
    assert store.buffer(StreamKey("rower", 1, "seat")).window(1.0) == []


def test_rate_is_measured_not_assumed():
    store = StreamStore(maxlen=1000, window_s=10)
    key = StreamKey("rower", 1, "seat")
    for i in range(41):
        store.push(key, sample(i * 0.05))
    assert abs(store.buffers[key].rate_hz() - 20.0) < 0.01


def test_rate_is_none_with_one_sample():
    store = StreamStore()
    key = StreamKey("rower", 1, "seat")
    store.push(key, sample(1.0))
    assert store.buffers[key].rate_hz() is None


def test_seq_gaps_counted():
    store = StreamStore()
    key = StreamKey("rower", 1, "seat")
    for seq in (1, 2, 5, 6):          # 3 and 4 lost in flight
        store.push(key, sample(seq * 0.05, seq=seq))
    assert store.buffers[key].n_gap == 2


def test_sender_restart_is_not_four_billion_drops():
    """seq resets to 0 on reboot. Modulo arithmetic makes that look like a
    near-U32 gap; it must not be charged as lost samples."""
    store = StreamStore()
    key = StreamKey("rower", 1, "seat")
    store.push(key, sample(0.0, seq=5000))
    store.push(key, sample(0.1, seq=0))
    assert store.buffers[key].n_gap == 0


def test_seq_wrap_at_uint32_is_not_a_gap():
    store = StreamStore()
    key = StreamKey("rower", 1, "seat")
    store.push(key, sample(0.0, seq=2 ** 32 - 1))
    store.push(key, sample(0.1, seq=0))
    assert store.buffers[key].n_gap == 0


def test_duplicate_seq_is_not_a_gap():
    store = StreamStore()
    key = StreamKey("rower", 1, "seat")
    store.push(key, sample(0.0, seq=7))
    store.push(key, sample(0.1, seq=7))
    assert store.buffers[key].n_gap == 0


# ---- discovery --------------------------------------------------------- #

def test_seats_appear_from_data_alone():
    store = StreamStore()
    for seat in (1, 4, 2):
        store.push(StreamKey("rower", seat, "seat"), sample(0.0))
    store.push(StreamKey("boat", None, "imu"), sample(0.0))
    assert store.seats() == [1, 2, 4]


def test_status_only_seat_is_still_discovered():
    """A sensor that connected and then went quiet should be visible, not
    absent -- that is what distinguishes it from a seat with no sensor."""
    store = StreamStore()
    store.put_presence(5, {"up": True})
    assert store.seats() == [5]


def test_boat_status_is_not_a_seat():
    store = StreamStore()
    store.boat_status = {"up": True}
    assert store.seats() == []


# ---- staleness --------------------------------------------------------- #

def test_never_published_counts_as_stale():
    assert is_stale(None, 1000.0) is True


def test_zero_max_age_disables_the_check():
    assert is_stale(99_999.0, 0.0) is False


def test_stale_boundary():
    assert is_stale(999.0, 1000.0) is False
    assert is_stale(1001.0, 1000.0) is True


# ---- clock estimation -------------------------------------------------- #

def test_min_offset_beats_mean_under_latency_jitter():
    """The sender's clock runs 50 s behind the host's. Three samples arrive
    with 0 ms, 200 ms and 40 ms of transport delay; the estimate should be
    pinned by the fastest, not dragged by the slowest."""
    clock = ClockEstimator()
    clock.update(10.0, t_recv=1.0, wall=60.0)        # delay 0
    clock.update(11.0, t_recv=2.2, wall=61.2)        # delay 200 ms
    got = clock.update(12.0, t_recv=3.04, wall=62.04)  # delay 40 ms
    assert abs(got - 62.0) < 1e-6


def test_offsets_outside_the_window_are_forgotten():
    """A stale minimum must not pin the estimate forever, or clock drift could
    never be followed."""
    clock = ClockEstimator(window_s=1.0)
    clock.update(10.0, t_recv=0.0, wall=60.0)
    got = clock.update(20.0, t_recv=5.0, wall=71.0)
    assert abs(got - 71.0) < 1e-6


def test_uptime_wrap_is_unwrapped():
    clock = ClockEstimator()
    u32 = 2 ** 32
    assert clock.unwrap(float(u32 - 1000)) == (u32 - 1000) / 1000.0
    after = clock.unwrap(1000.0)
    assert after > (u32 - 1000) / 1000.0
