#!/usr/bin/env python3
"""
Putting the camera's clock on the Jetson's.

The frame header (SENDER.md) carries two instants on the sender's monotonic
clock: `t_ms`, when the frame was captured, and `t_send_ms`, just before it was
handed to ZMQ. Their difference is exact -- one clock -- and is the sender's
own encode-and-queue time. Mapping them onto the Jetson's wall clock is the
part that needs care, and it is the same problem embedded/funnel solves for
the seat sensors, so the same estimator is used: ported from
funnel/store.py's ClockEstimator.

    offset = host_wall_at_receipt - sender_send_instant
           = true_clock_offset + network_delay

Network delay is never negative, so the *minimum* offset over a sliding window
is the best estimate of the true offset, set by whichever frame travelled
fastest. Using `t_send_ms` here rather than `t_ms` matters: the gap from
capture to send is encode time, which varies frame to frame and would
otherwise be mistaken for network delay and inflate the estimate.

What that buys, and what it cannot:

  * Every frame's capture time on the Jetson's clock, to within the minimum
    network delay (sub-millisecond on wired Ethernet, a few ms on Wi-Fi), with
    no clock synchronisation configured anywhere.
  * Network latency per frame *relative to the fastest frame in the window*.
    The fastest frame reads as 0 ms, so `net` underestimates the true one-way
    delay by that minimum. If the absolute figure matters, run chrony on the
    cameras against the Jetson and send `t` (epoch seconds) as well; it is
    then believed directly and `clock` reports "ntp".

A sender reboot resets its monotonic clock, which makes the true offset jump
by the reboot's worth of uptime. The window minimum would hold the stale,
smaller value for a whole window, putting every capture time far in the past,
so the estimator resets itself when the sender's clock runs backwards or when
the offset jumps by more than RESET_S.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from .wire import FrameHeader

RESET_S = 2.0


class ClockEstimator:
    """Maps a sender's monotonic seconds onto host wall-clock seconds."""

    def __init__(self, window_s: float = 30.0) -> None:
        self.window_s = window_s
        self._offsets: deque = deque()      # (t_recv_mono, offset)
        self._last_sender: Optional[float] = None
        self.n_resets = 0

    def reset(self) -> None:
        self._offsets.clear()
        self._last_sender = None
        self.n_resets += 1

    def update(self, t_sender_s: float, t_recv: float, wall: float) -> float:
        """Record one observation; return the current best offset."""
        offset = wall - t_sender_s
        if self._offsets and (
                (self._last_sender is not None and t_sender_s < self._last_sender)
                or offset - self.best > RESET_S):
            self.reset()
        self._last_sender = t_sender_s
        self._offsets.append((t_recv, offset))
        cutoff = t_recv - self.window_s
        while len(self._offsets) > 1 and self._offsets[0][0] < cutoff:
            self._offsets.popleft()
        return self.best

    @property
    def best(self) -> float:
        return min(o for _, o in self._offsets)


@dataclass(slots=True)
class Stamps:
    """One frame's timeline on the host wall clock (seconds).

    clock   "ntp"  the sender sent epoch time; believed directly.
            "est"  sender monotonic time, mapped by ClockEstimator.
            "recv" the sender sent no timing; only receipt onward is known.
    """

    clock: str
    t_cap: Optional[float]
    t_send: Optional[float]


def stamp(h: FrameHeader, est: ClockEstimator, t_recv_mono: float,
          t_recv_wall: float) -> Stamps:
    enc_s = None
    if h.t_ms is not None and h.t_send_ms is not None:
        enc_s = (h.t_send_ms - h.t_ms) / 1000.0

    if h.t is not None:
        t_send = h.t + enc_s if enc_s is not None else None
        return Stamps("ntp", h.t, t_send)

    anchor_ms = h.t_send_ms if h.t_send_ms is not None else h.t_ms
    if anchor_ms is None:
        return Stamps("recv", None, None)

    offset = est.update(anchor_ms / 1000.0, t_recv_mono, t_recv_wall)
    t_send = h.t_send_ms / 1000.0 + offset if h.t_send_ms is not None else None
    t_cap = h.t_ms / 1000.0 + offset if h.t_ms is not None else None
    return Stamps("est", t_cap, t_send)
