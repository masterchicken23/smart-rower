#!/usr/bin/env python3
"""Topic and payload decoding. No broker involved -- these are the contract."""

from __future__ import annotations

from funnel.models import StreamKey
from funnel.sources.mqtt import decode_payload, parse_topic, to_sample
from funnel.store import ClockEstimator

# ---- topics ------------------------------------------------------------ #

def test_rower_topic():
    assert parse_topic("rower/3/seat") == StreamKey("rower", 3, "seat")


def test_boat_topic():
    assert parse_topic("boat/imu") == StreamKey("boat", None, "imu")


def test_seats_are_one_based():
    """Bow is 1. A seat 0 is a firmware bug, not a seat, and must not create a
    buffer that then shows up in /rowers."""
    assert parse_topic("rower/0/seat") is None
    assert parse_topic("rower/-1/seat") is None


def test_unrecognised_topics_are_rejected_not_guessed():
    for topic in ("rower/x/seat", "rower/3", "rower/3/seat/extra",
                  "boat", "boat/a/b", "", "/", "something/else"):
        assert parse_topic(topic) is None, topic


def test_leading_and_trailing_slashes_tolerated():
    assert parse_topic("/rower/3/seat/") == StreamKey("rower", 3, "seat")


def test_high_seat_numbers_need_no_configuration():
    """An eight, or a sixteen; the funnel does not know the crew size."""
    assert parse_topic("rower/16/seat") == StreamKey("rower", 16, "seat")


# ---- payloads ---------------------------------------------------------- #

def test_decode_object():
    assert decode_payload(b'{"seq":1,"d_mm":812}') == {"seq": 1, "d_mm": 812}


def test_decode_rejects_non_objects():
    for raw in (b"", b"not json", b"[1,2]", b"812", b"null",
                b'{"seq":1,'):
        assert decode_payload(raw) is None, raw


def test_envelope_split_from_measurements():
    s = to_sample({"seq": 412, "t_ms": 1000, "dev": "ce40d0",
                   "d_mm": 812, "v_mms": -350}, t_recv=5.0, wall=100.0)
    assert s.seq == 412
    assert s.dev == "ce40d0"
    assert s.values == {"d_mm": 812, "v_mms": -350}
    assert s.t_recv == 5.0


def test_epoch_timestamp_used_directly_when_present():
    """A sender with NTP knows real time; believe it rather than estimating."""
    s = to_sample({"t": 1_760_000_000.5, "t_ms": 1000, "d_mm": 1},
                  t_recv=5.0, wall=100.0, clock=ClockEstimator())
    assert s.t_src == 1_760_000_000.5


def test_uptime_mapped_onto_host_clock():
    clock = ClockEstimator()
    s = to_sample({"t_ms": 2000, "d_mm": 1}, t_recv=5.0, wall=100.0,
                  clock=clock)
    # offset = 100.0 - 2.0 = 98.0, so the sample is placed at 100.0
    assert s.t_src == 100.0


def test_no_timestamp_at_all_is_not_an_error():
    s = to_sample({"d_mm": 812}, t_recv=5.0, wall=100.0,
                  clock=ClockEstimator())
    assert s.t_src is None
    assert s.values == {"d_mm": 812}


def test_missing_seq_is_allowed():
    """seq is how drops are detected, but a sensor without one still works --
    it just gets no gap accounting."""
    s = to_sample({"d_mm": 812}, t_recv=1.0, wall=1.0)
    assert s.seq is None


def test_seq_is_masked_to_uint32():
    s = to_sample({"seq": 2 ** 32 + 5, "d_mm": 1}, t_recv=1.0, wall=1.0)
    assert s.seq == 5
