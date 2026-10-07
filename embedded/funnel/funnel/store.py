#!/usr/bin/env python3
"""
The buffers, and the slot the compute loop publishes into.

StreamStore holds one bounded ring buffer per stream. Senders publish at
whatever rate they like and the funnel never applies back-pressure: when a
buffer is full the oldest sample is evicted. Falling behind is worse than
losing history, which is the same trade the ZMQ code in
experiments/network-video-yolo makes with its drop-oldest queues.

StateStore holds the newest Snapshot. Publishing is a single attribute
assignment, so a reader either sees the previous tick's snapshot or this one,
never a half-written mixture. That is the whole reason the service runs on one
event loop: no locks, provided mutation never spans an await.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from typing import Any, Optional

from .models import Sample, Snapshot, StreamKey

U32 = 2 ** 32


class ClockEstimator:
    """Maps a sender's own clock onto the host's, without needing NTP.

    Sensors report `t_ms`, their uptime in milliseconds at the instant of
    measurement. That is the number we actually want -- the alternative is
    stamping on arrival, which bakes in radio and broker latency (exactly the
    defect in the current sensor-esp-pipeline, where receiver.ino restamps with
    its own millis() on packet arrival).

    Offset per sample is `host_wall - sender_seconds`, which equals the true
    offset plus that sample's transport delay. Delay is non-negative, so the
    *minimum* offset over a window is the best available estimate of the true
    one, and the sample that achieved it is the one that travelled fastest.
    Tracking the minimum rather than the mean therefore removes latency jitter
    instead of averaging it in.
    """

    def __init__(self, window_s: float = 30.0) -> None:
        self.window_s = window_s
        self._offsets: deque = deque()      # (t_recv_mono, offset)
        self._wraps = 0
        self._last_raw: Optional[float] = None

    def unwrap(self, t_ms: float) -> float:
        """uint32 millis wraps roughly every 49.7 days; keep it monotonic."""
        if self._last_raw is not None and t_ms < self._last_raw - (U32 / 2):
            self._wraps += 1
        self._last_raw = t_ms
        return (t_ms + self._wraps * U32) / 1000.0

    def update(self, t_sender_s: float, t_recv: float, wall: float) -> float:
        offset = wall - t_sender_s
        self._offsets.append((t_recv, offset))
        cutoff = t_recv - self.window_s
        while len(self._offsets) > 1 and self._offsets[0][0] < cutoff:
            self._offsets.popleft()
        best = min(o for _, o in self._offsets)
        return t_sender_s + best


class StreamBuffer:
    """One stream's recent samples, plus the counters that describe its health."""

    def __init__(self, key: StreamKey, maxlen: int, window_s: float) -> None:
        self.key = key
        self.window_s = window_s
        self.samples: deque = deque(maxlen=maxlen)
        self.clock = ClockEstimator()
        self.n_recv = 0
        self.n_bad = 0
        self.n_gap = 0
        self.last_seq: Optional[int] = None
        self.dev: Optional[str] = None
        self.t_first: Optional[float] = None
        self.state: dict[str, Any] = {}
        """Scratch space for compute stages that must carry state between
        ticks -- a filter's previous output, an observed calibration range.

        It lives here, beside the samples it derives from, so a per-stream
        transformation needs no registry of its own and disappears with the
        stream. Keyed by stage or pipeline name to keep stages from colliding.
        Written only by the compute loop, which is single-threaded with respect
        to everything else on the event loop."""

    # -- writing ------------------------------------------------------------ #
    def push(self, sample: Sample) -> None:
        """Append a sample and account for any the sender says we missed.

        Must stay synchronous: ingest, compute and request handling share one
        event loop, and the no-locks invariant holds only because no buffer
        mutation spans an await.
        """
        if sample.seq is not None:
            if self.last_seq is not None:
                gap = (sample.seq - self.last_seq) % U32
                if gap > 1 and gap < U32 // 2:
                    # A sender restart resets seq to 0, which modulo-wraps to a
                    # huge gap; treat only plausible gaps as real losses.
                    self.n_gap += gap - 1
            self.last_seq = sample.seq
        if sample.dev:
            self.dev = sample.dev
        if self.t_first is None:
            self.t_first = sample.t_recv
        self.samples.append(sample)
        self.n_recv += 1
        self._trim(sample.t_recv)

    def mark_bad(self) -> None:
        self.n_bad += 1

    def _trim(self, now: float) -> None:
        """Drop samples older than the window. maxlen is the hard cap; this is
        the soft one, so a fast publisher does not push the window's worth of
        history out of reach of the compute loop."""
        cutoff = now - self.window_s
        while self.samples and self.samples[0].t_recv < cutoff:
            self.samples.popleft()

    # -- reading ------------------------------------------------------------ #
    @property
    def latest(self) -> Optional[Sample]:
        return self.samples[-1] if self.samples else None

    def window(self, seconds: Optional[float] = None,
               now: Optional[float] = None) -> list:
        """Samples from the last `seconds`, oldest first.

        This is what the compute loop reads. Scans from the newest end, so the
        cost is the number of samples returned rather than the buffer size.
        """
        if not self.samples:
            return []
        now = time.monotonic() if now is None else now
        span = self.window_s if seconds is None else seconds
        cutoff = now - span
        out: list = []
        for s in reversed(self.samples):
            if s.t_recv < cutoff:
                break
            out.append(s)
        out.reverse()
        return out

    def age_ms(self, now: Optional[float] = None) -> Optional[float]:
        s = self.latest
        if s is None:
            return None
        now = time.monotonic() if now is None else now
        return (now - s.t_recv) * 1000.0

    def rate_hz(self) -> Optional[float]:
        """Measured arrival rate over what is buffered -- not a configured
        value, so a sensor running slow is visible rather than assumed."""
        n = len(self.samples)
        if n < 2:
            return None
        span = self.samples[-1].t_recv - self.samples[0].t_recv
        if span <= 0:
            return None
        return (n - 1) / span

    def stats(self, now: Optional[float] = None,
              max_age_ms: float = 0.0) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        age = self.age_ms(now)
        rate = self.rate_hz()
        return {
            "path": self.key.as_path(),
            "scope": self.key.scope,
            "ident": self.key.ident,
            "stream": self.key.stream,
            "age_ms": None if age is None else round(age, 1),
            "rate_hz": None if rate is None else round(rate, 2),
            "stale": is_stale(age, max_age_ms),
            "n_recv": self.n_recv,
            "n_bad": self.n_bad,
            "n_gap": self.n_gap,
            "buffered": len(self.samples),
            "last_seq": self.last_seq,
            "dev": self.dev,
        }


def is_stale(age_ms: Optional[float], max_age_ms: float) -> bool:
    if age_ms is None:
        return True
    if not max_age_ms:
        return False                        # 0 disables the check
    return age_ms > max_age_ms


class StreamStore:
    """Every stream seen so far, created on first sight.

    Nothing here is configured per crew: publish to rower/9/seat and seat 9
    appears, stop publishing and it ages out of freshness on its own. That is
    what lets the rower count scale without a config change.
    """

    def __init__(self, maxlen: int = 2048, window_s: float = 10.0) -> None:
        self.maxlen = maxlen
        self.window_s = window_s
        self.buffers: dict[StreamKey, StreamBuffer] = {}
        self.presence: dict[int, dict[str, Any]] = {}
        """Per-seat, from the retained rower/<seat>/status topics."""
        self.boat_status: dict[str, Any] = {}
        """Boat-level equivalent, kept apart so it cannot be mistaken for a
        seat (a seat 0 does not exist -- bow is 1)."""
        self.n_unknown_topic = 0
        self.n_bad = 0

    def buffer(self, key: StreamKey) -> StreamBuffer:
        buf = self.buffers.get(key)
        if buf is None:
            buf = StreamBuffer(key, self.maxlen, self.window_s)
            self.buffers[key] = buf
        return buf

    def push(self, key: StreamKey, sample: Sample) -> None:
        self.buffer(key).push(sample)

    def mark_bad(self, key: Optional[StreamKey] = None) -> None:
        self.n_bad += 1
        if key is not None:
            self.buffer(key).mark_bad()

    def put_presence(self, seat: int, rec: dict[str, Any]) -> None:
        rec = dict(rec)
        rec["t_recv"] = time.monotonic()
        self.presence[seat] = rec

    def get(self, key: StreamKey) -> Optional[StreamBuffer]:
        return self.buffers.get(key)

    def keys(self) -> list[StreamKey]:
        return sorted(self.buffers, key=lambda k: (k.scope, k.ident or 0, k.stream))

    def seats(self) -> list[int]:
        seats = {k.ident for k in self.buffers
                 if k.scope == "rower" and k.ident is not None}
        seats |= set(self.presence)
        return sorted(seats)

    def streams_for(self, scope: str, ident: Optional[int]) -> list[StreamKey]:
        return [k for k in self.keys() if k.scope == scope and k.ident == ident]

    def stats(self, now: Optional[float] = None,
              max_age_ms: float = 0.0) -> list[dict[str, Any]]:
        now = time.monotonic() if now is None else now
        return [self.buffers[k].stats(now, max_age_ms) for k in self.keys()]


class StateStore:
    """The one slot the API reads.

    `publish` is a reference swap, which on CPython is atomic with respect to
    other tasks on the same loop. Readers therefore need no lock and can never
    observe a partially built tick.
    """

    def __init__(self) -> None:
        self._current: Snapshot = Snapshot.empty()
        self.started = time.time()
        self.started_mono = time.monotonic()

    @property
    def current(self) -> Snapshot:
        return self._current

    def publish(self, snap: Snapshot) -> None:
        self._current = snap

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_mono


def iter_window(buffers: Iterable[StreamBuffer], seconds: float) -> dict:
    """Convenience for metrics code: {path: [samples]} over a shared cutoff."""
    now = time.monotonic()
    return {b.key.as_path(): b.window(seconds, now) for b in buffers}
