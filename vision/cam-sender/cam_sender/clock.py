#!/usr/bin/env python3
"""
The one monotonic clock both header timestamps are taken on (SENDER.md,
"Getting the two instants right").

`t_ms` should be the sensor's own capture timestamp, and `t_send_ms` must be
on the same clock or their difference -- the detector's `ms.enc` -- means
nothing. libcamera's `SensorTimestamp` is nanoseconds on a kernel clock that
is CLOCK_MONOTONIC on current Pi kernels, but SENDER.md says to check rather
than assume: `detect()` compares one sensor timestamp against each candidate
clock and keeps the one it lands on.

Off Linux there is no clock_gettime, and only the fallback exists:
perf_counter (CLOCK_MONOTONIC on Linux; the high-resolution counter on
Windows, where time.monotonic() ticks at 15.6 ms).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

MATCH_NS = 1_000_000_000
"""A sensor timestamp within this of a clock's "now" is on that clock. Real
capture-to-dequeue delay is tens of ms; unrelated clocks differ by uptime."""


@dataclass(frozen=True)
class Clock:
    name: str
    now_ns: Callable[[], int]

    def now_ms(self) -> float:
        return self.now_ns() / 1e6


PERF = Clock("perf_counter", time.perf_counter_ns)


def candidates() -> list[Clock]:
    """Kernel clocks a sensor timestamp may be on, most likely first."""
    out = []
    for name in ("CLOCK_MONOTONIC", "CLOCK_BOOTTIME"):
        cid = getattr(time, name, None)
        if cid is not None and hasattr(time, "clock_gettime_ns"):
            out.append(Clock(name[6:].lower(),
                             lambda cid=cid: time.clock_gettime_ns(cid)))
    return out


def detect(sensor_ns: int,
           clocks: Optional[list[Clock]] = None) -> Optional[Clock]:
    """The clock `sensor_ns` was stamped on, or None if it matches none.

    Called right after the frame is dequeued, so the true answer is "now,
    minus a few tens of ms". Ties -- monotonic and boottime agree on a Pi that
    has never suspended -- go to the first candidate."""
    best: Optional[Clock] = None
    best_d = MATCH_NS
    for c in candidates() if clocks is None else clocks:
        d = abs(c.now_ns() - sensor_ns)
        if d < best_d:
            best, best_d = c, d
    return best
