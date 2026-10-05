#!/usr/bin/env python3
"""
What each tick computes.

Two kinds of output, and the difference matters:

  * **Raw stream blocks**, one per MQTT stream, under the stream's own name.
    Newest values exactly as published, plus timing and health. These exist so
    a stream is visible the moment a sensor starts publishing, and for
    debugging. They are not what a client should display.

  * **Derived blocks**, produced by an explicit transformation of the buffered
    samples, under their own name. `position` is the first: see
    compute/seat_position.py. These have pinned shapes and are what the mobile
    app consumes.

So a new sensor is immediately visible with no code change, but a value only
becomes app-facing once something deliberately derives it.

Crew-wide derived values (a synchrony score across seats, say) belong in
`boat`, since they are not any single rower's.

Contract for anything added here
--------------------------------
`compute` is called once per grid point, on the event loop, with no await
anywhere inside it. That buys the whole service its lock-free design, and it
costs this one rule:

  * Do not await, sleep, or do I/O. A slow tick delays the next one; a blocking
    tick stalls ingest and the API too.
  * Read the buffers through `buf.window(seconds)`. Do not mutate them.
  * Return fresh dicts. The Snapshot they go into is frozen and handed
    unguarded to request handlers, so nothing it holds may be edited later.
  * Never raise on missing data. A sensor that has not published yet is the
    normal startup state, not an error.

Where to add things
-------------------
Transforming one stream into a cleaner version of itself -- the seat sensor's
millimetres into a usable seat position -- belongs in its own module, as
compute/seat_position.py does. Keep this file as the place that assembles the
tick: it decides what each seat and the boat expose, and delegates the actual
mathematics.

Stroke metrics (phase, rate, length) should be built on the *derived* position
rather than the raw stream, which is the reason the pipeline exists. Their
starting point is experiments/sensor-esp-pipeline/plotdistance.py, with the
rate-dependence caveat described in seat_position.py.
"""

from __future__ import annotations

from typing import Any, Optional

from ..config import Settings
from ..models import Snapshot, StreamKey
from ..store import StreamBuffer, StreamStore, is_stale
from . import seat_position


def block_for(buf: StreamBuffer, now: float, max_age_ms: float) -> dict[str, Any]:
    """One stream's passthrough block.

    `values` is whatever the sender published, untouched. The rest is the
    funnel's own account of the stream: when the reading arrived, how fast they
    are arriving, and whether any went missing. A client that wants to render a
    number needs the first; a client deciding whether to render it at all needs
    the rest.
    """
    s = buf.latest
    age = buf.age_ms(now)
    rate = buf.rate_hz()
    block: dict[str, Any] = {
        "age_ms": None if age is None else round(age, 1),
        "rate_hz": None if rate is None else round(rate, 2),
        "stale": is_stale(age, max_age_ms),
        "n": len(buf.samples),
        "n_gap": buf.n_gap,
        "seq": buf.last_seq,
    }
    if s is None:
        block["values"] = {}
        block["t"] = None
        return block
    block["values"] = dict(s.values)
    block["t"] = None if s.t_src is None else round(s.t_src, 4)
    if buf.dev:
        block["dev"] = buf.dev
    return block


def compute(store: StreamStore, prev: Snapshot, settings: Settings,
            now: float) -> tuple[dict, dict, dict, dict]:
    """Build one tick's results.

    Returns (rowers, boat, streams, presence):

      rowers    {seat: {name: block}}        per-rower, 1-based seats; `name`
                                             is a raw stream name or a derived
                                             block name such as "position"
      boat      {name: block}                not tied to any rower
      streams   {path: stats}                health of every stream seen
      presence  {seat: {...}}                from the retained status topics

    Seats and streams are whatever has published, so the crew size follows the
    data rather than any configuration.
    """
    max_age = settings.max_age_ms

    rowers: dict[int, dict[str, Any]] = {}
    boat: dict[str, Any] = {}
    streams: dict[str, dict[str, Any]] = {}

    for key in store.keys():   # noqa: SIM118 -- StreamStore, not a dict
        buf = store.buffers[key]
        streams[key.as_path()] = buf.stats(now, max_age)
        block = block_for(buf, now, max_age)
        if key.scope == "rower" and key.ident is not None:
            rowers.setdefault(key.ident, {})[key.stream] = block
        else:
            boat[key.stream] = block

    # ---- derived blocks ---------------------------------------------------- #
    # Seat position is derived from the raw distance stream rather than passed
    # through; the transformation lives in compute/seat_position.py. Add
    # further per-rower derivations the same way, and crew-wide ones to `boat`.
    for seat, blocks in rowers.items():
        buf = store.get(StreamKey("rower", seat, seat_position.RAW_STREAM))
        if buf is not None:
            blocks[seat_position.OUTPUT] = seat_position.transform(
                buf, settings, now)

    presence = {seat: _presence_block(rec, now)
                for seat, rec in store.presence.items()}
    if store.boat_status:
        boat["status"] = _presence_block(store.boat_status, now)

    # Seats seen only via their status topic still belong in the listing, so a
    # sensor that connected and then went quiet is visible rather than absent.
    for seat in presence:
        rowers.setdefault(seat, {})

    return rowers, boat, streams, presence


def _presence_block(rec: dict[str, Any], now: float) -> dict[str, Any]:
    out = {k: v for k, v in rec.items() if k != "t_recv"}
    t_recv: Optional[float] = rec.get("t_recv")
    out["age_ms"] = None if t_recv is None else round((now - t_recv) * 1000.0, 1)
    return out
