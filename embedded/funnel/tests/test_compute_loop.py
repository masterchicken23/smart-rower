#!/usr/bin/env python3
"""
The scheduler. These are the tests that justify the absolute-deadline grid over
`sleep(period)`, so they check the timing properties directly rather than
asserting that the loop merely runs.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from funnel.compute.loop import ComputeLoop
from funnel.compute.metrics import compute as real_compute
from funnel.config import Settings
from funnel.models import Sample, StreamKey
from funnel.store import StateStore, StreamStore


def settings(**kw) -> Settings:
    base = {"compute_hz": 100.0, "max_age_ms": 1000.0, "mqtt_host": "none"}
    base.update(kw)
    return Settings(**base)


def empty_compute(store, prev, s, now):
    return {}, {}, {}, {}


async def test_ticks_fire_at_the_configured_rate():
    loop = ComputeLoop(StreamStore(), StateStore(), settings(compute_hz=100.0),
                       empty_compute)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.25)
    loop.stop()
    await task
    # 100 Hz for 250 ms: allow generous slack for a loaded CI machine, but a
    # loop that drifted or stalled fails this.
    assert 15 <= loop.n_ticks <= 40


async def test_tick_fires_with_no_data_at_all():
    """A frozen tick counter means the service is dead; a rising one with empty
    blocks means the sensors are quiet. Clients need to tell those apart."""
    state = StateStore()
    loop = ComputeLoop(StreamStore(), state, settings(compute_hz=50.0),
                       real_compute)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.1)
    loop.stop()
    await task
    assert loop.n_ticks >= 2
    assert state.current.tick >= 2
    assert state.current.rowers == {}


async def test_overrun_skips_grid_points_instead_of_drifting():
    """A tick that takes three periods must give up the two grid points it
    missed. Catching up would emit a burst of near-identical snapshots and
    leave the loop permanently late."""
    period = 0.02
    calls = {"n": 0}

    def slow_once(store, prev, s, now):
        calls["n"] += 1
        if calls["n"] == 2:
            time.sleep(period * 3)      # blocking, as a real overrun would be
        return {}, {}, {}, {}

    loop = ComputeLoop(StreamStore(), StateStore(),
                       settings(compute_hz=1.0 / period), slow_once)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.3)
    loop.stop()
    await task
    assert loop.n_late >= 1
    # Having skipped, the loop must be back on schedule rather than still late.
    assert abs(loop.jitter_ms) < period * 1000 * 2


async def test_jitter_is_recorded_for_health():
    loop = ComputeLoop(StreamStore(), StateStore(), settings(compute_hz=100.0),
                       empty_compute)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.1)
    loop.stop()
    await task
    assert loop.jitter_ms_max >= 0.0
    assert loop.stats()["hz"] == 100.0


async def test_compute_error_does_not_stop_the_clock():
    """A bad metric is a bug to fix, not a reason to stop serving. The previous
    snapshot keeps being served and /health reports n_errors."""
    state = StateStore()

    def boom(store, prev, s, now):
        raise ValueError("bad metric")

    loop = ComputeLoop(StreamStore(), state, settings(compute_hz=100.0), boom)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.1)
    loop.stop()
    await task
    assert loop.n_errors >= 2
    assert loop.n_ticks >= 2
    assert state.current.tick == 0        # nothing was ever published


async def test_published_snapshot_is_a_complete_replacement():
    """Readers must never see a half-built tick: publish swaps one reference."""
    store = StreamStore()
    state = StateStore()
    store.push(StreamKey("rower", 1, "seat"),
               Sample(t_recv=time.monotonic(), t_src=None, seq=1,
                      values={"d_mm": 812}))
    loop = ComputeLoop(store, state, settings(compute_hz=100.0), real_compute)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)
    first = state.current
    await asyncio.sleep(0.05)
    loop.stop()
    await task
    assert state.current is not first          # replaced, not mutated
    assert first.rowers[1]["seat"]["values"] == {"d_mm": 812}


async def test_stop_is_prompt():
    loop = ComputeLoop(StreamStore(), StateStore(), settings(compute_hz=2.0),
                       empty_compute)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    loop.stop()
    await asyncio.wait_for(task, timeout=1.0)
    # Must not have to wait out the remaining 500 ms period.
    assert time.monotonic() - t0 < 0.3


@pytest.mark.parametrize("hz", [1.0, 10.0, 50.0])
async def test_period_follows_configuration(hz):
    loop = ComputeLoop(StreamStore(), StateStore(), settings(compute_hz=hz),
                       empty_compute)
    assert abs(loop.period - 1.0 / hz) < 1e-9
