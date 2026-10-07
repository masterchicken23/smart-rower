"""The input and output wire contracts, with no camera and no funnel."""

from __future__ import annotations

import json

import pytest

from april_detect import wire

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def test_imagezmq_dict_msg_round_trips_through_the_real_library():
    """The timing header rides in imageZMQ's `msg` unmodified: send with the
    library's own send_jpg and decode what actually crosses the socket."""
    imagezmq = pytest.importorskip("imagezmq")
    import zmq

    ctx = imagezmq.SerializingContext()
    a, b = ctx.socket(zmq.PAIR), ctx.socket(zmq.PAIR)
    a.bind("inproc://t")
    b.connect("inproc://t")
    try:
        hdr = {"v": 1, "cam": "cam1", "seq": 7, "t_ms": 1000.5,
               "t_send_ms": 1012.25, "exposure_us": 8000}
        a.send_jpg(hdr, JPEG)
        parts = b.recv_multipart()
        h, jpeg = wire.decode_frame(parts)
    finally:
        a.close(0)
        b.close(0)
    assert jpeg == JPEG
    assert (h.cam, h.seq, h.t_ms, h.t_send_ms, h.t) == ("cam1", 7, 1000.5, 1012.25, None)
    assert h.extra == {"exposure_us": 8000}


def test_legacy_string_name_is_camera_only():
    parts = [json.dumps({"msg": "jetson-pi"}).encode(), JPEG]
    h, _ = wire.decode_frame(parts)
    assert h.cam == "jetson-pi" and h.seq is None and h.t_ms is None


def test_string_msg_holding_json_is_parsed():
    msg = json.dumps({"cam": "c", "seq": 3, "t_ms": 5})
    h, _ = wire.decode_frame([json.dumps({"msg": msg}).encode(), JPEG])
    assert (h.cam, h.seq, h.t_ms) == ("c", 3, 5.0)


def test_topic_then_jpeg_like_pi_mjpeg_pub():
    h, jpeg = wire.decode_frame([b"cam", JPEG])
    assert h.cam == "cam" and jpeg == JPEG


def test_single_part_with_prefix():
    h, jpeg = wire.decode_frame([b"cam2 " + JPEG])
    assert h.cam == "cam2" and jpeg == JPEG


def test_malformed_header_keeps_the_frame():
    h, jpeg = wire.decode_frame([b"{not json", JPEG])
    assert jpeg == JPEG and not h.ok


def test_no_jpeg_is_rejected():
    assert wire.decode_frame([b"{}", b"PNG...."]) is None
    assert wire.decode_frame([]) is None


@pytest.mark.parametrize("bad", [True, "12", float("nan"), None, [1]])
def test_non_numeric_timing_is_ignored(bad):
    h = wire.parse_header({"t_ms": bad, "seq": bad, "t": bad})
    assert h.t_ms is None and h.seq is None and h.t is None


def test_seq_wraps_to_uint32():
    assert wire.parse_header({"seq": 2 ** 32 + 5}).seq == 5


def test_encode_is_two_frames_of_compact_json():
    topic, body = wire.encode(wire.TOPIC_TAGS, {"a": 1, "b": [1, 2]})
    assert topic == b"apriltag" and body == b'{"a":1,"b":[1,2]}'


def test_encode_refuses_nan():
    """NaN is not JSON; a record carrying one would break strict parsers in
    the funnel, so it must fail here instead."""
    with pytest.raises(ValueError):
        wire.encode(wire.TOPIC_TAGS, {"x": float("nan")})
