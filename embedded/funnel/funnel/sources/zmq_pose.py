#!/usr/bin/env python3
"""
Computer-vision ingest over ZMQ. NOT WIRED UP -- this file documents the
planned contract so the CV side can be written against it, and so the shape of
the eventual source is visible next to the MQTT one.

The funnel does no vision work itself. A separate process runs the model and
publishes its results; this would subscribe to them. The prior art is already
in the repo and the contract below matches it:

    experiments/network-video-yolo/pose_stream.py   publisher (PUB, binds)
    experiments/network-video-yolo/pose_api.py      an existing subscriber

Transport
    ZMQ PUB/SUB, two-frame multipart: [topic_bytes, compact_json].
    Topics `pose` (one record per inference tick) and `meta` (resent every
    couple of seconds, because a SUB that joins late would otherwise never
    learn what the keypoint indices mean).

    SNDHWM on the publisher drops rather than buffers, so a slow subscriber
    costs the publisher nothing and the subscriber sees gaps instead of lag.
    `tick` in each record is a gapless counter, so those gaps are detectable.

Ports
    5555 video frames (camera -> CV), 5556 pose records in the existing
    experiment. This source defaults to 5557 so it can run alongside both
    during bring-up without a port clash.

Mapping onto the funnel's model
    A pose record carries a crew-wide frame, not one rower, so the natural
    mapping is: per-person blocks keyed by whatever seat the CV process
    assigns, pushed to StreamKey("rower", seat, "pose"); frame-level fields
    (tick, jitter, inference time, person count) to
    StreamKey("boat", None, "vision").

    Seat assignment is the open question and it belongs upstream, in the CV
    process, which is the only thing that knows where in the frame each person
    is. The funnel should not be guessing seats from bounding boxes.

    One caveat worth carrying over from pose_api.py: its /keypoints divides x
    and y by frame width and height separately, which is right for drawing on
    the frame and wrong for deriving angles, because the axes scale differently
    unless the frame is square. Pull the pixel values and the upstream `rowing`
    block for anything geometric.

Implementing it
    Subclass BaseSource, defer `import zmq` into run(), set RCVTIMEO so
    cancellation is noticed promptly, and keep decode-to-emit synchronous as
    MqttSource does. Then add it to the task list in api/app.py behind
    FUNNEL_ZMQ_ENABLED.
"""

from __future__ import annotations

from typing import Any, Optional

from ..config import Settings
from ..recorder import Recorder
from ..store import StreamStore
from .base import BaseSource

TOPIC_POSE = b"pose"
TOPIC_META = b"meta"


class ZmqPoseSource(BaseSource):
    """Placeholder. Enabling FUNNEL_ZMQ_ENABLED before this is implemented
    should fail loudly at startup rather than silently ingest nothing."""

    name = "zmq"

    def __init__(self, store: StreamStore, settings: Settings,
                 recorder: Optional[Recorder] = None) -> None:
        super().__init__(store, recorder)
        self.settings = settings

    async def run(self) -> None:
        raise NotImplementedError(
            "ZMQ pose ingest is not implemented yet. See the module docstring "
            "in funnel/sources/zmq_pose.py for the intended contract, and set "
            "FUNNEL_ZMQ_ENABLED=false until it exists."
        )

    def stats(self) -> dict[str, Any]:
        s = super().stats()
        s.update({"subscribe": self.settings.zmq_subscribe,
                  "implemented": False})
        return s
