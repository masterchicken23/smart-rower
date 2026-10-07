#!/usr/bin/env python3
"""
Replay a recorded session back into the buffers, at the rate it was captured.

The point is to be able to develop and test the computation with no boat, no
sensors and no broker: record one outing with FUNNEL_RECORD_ENABLED=true, then
iterate on compute/metrics.py against that file for as long as it takes.

Timing is reconstructed from each sample's `t_recv`, so the relative spacing
and the interleaving between streams are preserved -- a 20 Hz seat sensor and a
100 Hz IMU come back at 20 and 100 Hz, not in lockstep. Sample timestamps are
re-stamped onto the current monotonic clock, since ages and rates are measured
against arrival time and a recording's original clock is meaningless now.

Enable with FUNNEL_REPLAY_PATH=<session dir>, which takes the place of MQTT
ingest rather than running alongside it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

from .. import log
from ..models import Sample, StreamKey
from ..recorder import Recorder
from ..store import StreamStore
from .base import BaseSource


def key_from_filename(name: str) -> Optional[StreamKey]:
    """`rower_3_seat.jsonl` -> ("rower", 3, "seat");
    `boat_imu.jsonl` -> ("boat", None, "imu")."""
    stem = name[:-len(".jsonl")] if name.endswith(".jsonl") else name
    parts = stem.split("_")
    if len(parts) == 3 and parts[0] == "rower":
        try:
            return StreamKey("rower", int(parts[1]), parts[2])
        except ValueError:
            return None
    if len(parts) == 2 and parts[0] == "boat":
        return StreamKey("boat", None, parts[1])
    return None


def load_session(directory: str) -> list[tuple[float, StreamKey, dict]]:
    """Read every stream file into one list ordered by capture time."""
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"no such session directory: {directory}")
    events: list[tuple[float, StreamKey, dict]] = []
    for path in sorted(root.glob("*.jsonl")):
        key = key_from_filename(path.name)
        if key is None:
            log.warn(f"[replay] skipping unrecognised file {path.name}")
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue           # tolerate junk; see sample_pose_data.json
                try:
                    rec = json.loads(line)
                except Exception:          # noqa: BLE001
                    continue
                t = rec.get("t_recv")
                if not isinstance(t, (int, float)):
                    continue
                events.append((float(t), key, rec))
    events.sort(key=lambda e: e[0])
    return events


class ReplaySource(BaseSource):
    name = "replay"

    def __init__(self, store: StreamStore, path: str, speed: float = 1.0,
                 loop_forever: bool = True,
                 recorder: Optional[Recorder] = None) -> None:
        super().__init__(store, recorder)
        self.path = path
        self.speed = speed if speed > 0 else 1.0
        self.loop_forever = loop_forever
        self.n_passes = 0

    def stats(self) -> dict[str, Any]:
        s = super().stats()
        s.update({"path": self.path, "speed": self.speed,
                  "passes": self.n_passes})
        return s

    async def run(self) -> None:
        events = load_session(self.path)
        if not events:
            log.warn(f"[replay] {self.path} holds no samples; nothing to do")
            return
        span = events[-1][0] - events[0][0]
        log.info(f"[replay] {len(events)} samples over {span:.1f}s "
                 f"from {self.path} at {self.speed}x")

        while True:
            t_zero = events[0][0]
            start = time.monotonic()
            for t_cap, key, rec in events:
                target = start + (t_cap - t_zero) / self.speed
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.emit(key, Sample(
                    t_recv=time.monotonic(),
                    t_src=rec.get("t"),
                    seq=rec.get("seq"),
                    values=dict(rec.get("values") or {}),
                    dev=rec.get("dev"),
                ))
            self.n_passes += 1
            if not self.loop_forever:
                log.info("[replay] finished")
                return
            # Looping keeps the API populated indefinitely while iterating on
            # metrics. Buffers are time-windowed, so the seam between passes
            # ages out on its own.
