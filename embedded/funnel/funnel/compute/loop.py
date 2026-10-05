#!/usr/bin/env python3
"""
The fixed-rate computation loop.

Ported from the scheduler in experiments/network-video-yolo/pose_stream.py,
which gets two things right that a naive `while True: work(); sleep(period)`
does not:

  * The deadline is absolute, on a fixed grid. Sleeping for `period` after
    doing the work makes the real period `period + work`, so the loop drifts
    and the timestamps it stamps are unevenly spaced.

  * On overrun it *skips* whole grid points instead of trying to catch up. A
    loop that fell behind by two periods and then runs three ticks back to back
    produces a burst of near-identical results and stays late forever. Dropping
    the missed grid points puts it back on schedule immediately.

The tick fires whether or not any data arrived, so a consumer can tell "no data
is coming in" from "the service is dead" -- the snapshot's tick keeps
advancing in the first case and freezes in the second.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Optional

from .. import log
from ..config import Settings
from ..models import Snapshot
from ..store import StateStore, StreamStore

ComputeFn = Callable[[StreamStore, Snapshot, Settings, float], tuple]


class ComputeLoop:
    """Runs `compute_fn` on an even grid and publishes each result."""

    def __init__(self, store: StreamStore, state: StateStore,
                 settings: Settings, compute_fn: ComputeFn) -> None:
        self.store = store
        self.state = state
        self.settings = settings
        self.compute_fn = compute_fn
        self.period = settings.period_s
        self.n_ticks = 0
        self.n_late = 0
        self.n_errors = 0
        self.jitter_ms = 0.0
        self.jitter_ms_max = 0.0
        self.compute_ms = 0.0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict[str, Any]:
        return {
            "tick": self.n_ticks,
            "hz": round(1.0 / self.period, 3) if self.period else 0.0,
            "jitter_ms": round(self.jitter_ms, 3),
            "jitter_ms_max": round(self.jitter_ms_max, 3),
            "n_late": self.n_late,
            "n_errors": self.n_errors,
            "compute_ms": round(self.compute_ms, 3),
        }

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        period = self.period
        deadline = loop.time() + period
        log.info(f"[tick] loop at {1.0 / period:.1f} Hz ({period * 1000:.0f} ms)")

        while not self._stop.is_set():
            now = loop.time()
            if now < deadline:
                try:
                    await asyncio.wait_for(self._stop.wait(), deadline - now)
                    break               # stop was requested while waiting
                except TimeoutError:
                    pass                # the normal path: the deadline arrived
                t0 = loop.time()
            else:
                t0 = now                # the previous tick overran

            lateness = t0 - deadline
            self.jitter_ms = lateness * 1000.0
            self.jitter_ms_max = max(self.jitter_ms_max, abs(self.jitter_ms))
            if lateness >= period:
                skipped = int(lateness // period)
                self.n_late += skipped
                deadline += skipped * period
            deadline += period
            self.n_ticks += 1

            self._tick(self.n_ticks, time.time(), time.monotonic())

        log.info(f"[tick] stopped after {self.n_ticks} ticks "
                 f"({self.n_late} grid points skipped, {self.n_errors} errors)")

    def tick_once(self, tick: Optional[int] = None) -> None:
        """Run one grid point right now, off-schedule.

        The running loop paces itself; this is for tests, which want the
        compute-then-publish path without a clock, and for poking the service
        by hand during bring-up.
        """
        if tick is None:
            self.n_ticks += 1
            tick = self.n_ticks
        self._tick(tick, time.time(), time.monotonic())

    def _tick(self, tick: int, t_wall: float, t_mono: float) -> None:
        """One grid point. Synchronous by design: it reads the buffers and
        swaps the published snapshot without an await in between, which is what
        keeps the whole service lock-free."""
        c0 = time.perf_counter()
        prev = self.state.current
        try:
            rowers, boat, streams, presence = self.compute_fn(
                self.store, prev, self.settings, t_mono)
        except Exception as e:              # noqa: BLE001
            # A bad metric must not stop the clock. Keep serving the previous
            # snapshot and count it; /health surfaces n_errors.
            self.n_errors += 1
            if self.n_errors <= 5 or self.n_errors % 100 == 0:
                log.error(f"[tick] compute failed at tick {tick}: {e!r}")
            return
        self.compute_ms = (time.perf_counter() - c0) * 1000.0

        self.state.publish(Snapshot(
            tick=tick,
            t=t_wall,
            t_recv=t_mono,
            jitter_ms=round(self.jitter_ms, 3),
            compute_ms=round(self.compute_ms, 3),
            rowers=rowers,
            boat=boat,
            streams=streams,
            presence=presence,
        ))


def make_loop(store: StreamStore, state: StateStore, settings: Settings,
              compute_fn: Optional[ComputeFn] = None) -> ComputeLoop:
    if compute_fn is None:
        from .metrics import compute as default_compute
        compute_fn = default_compute
    return ComputeLoop(store, state, settings, compute_fn)
