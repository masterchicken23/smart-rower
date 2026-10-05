#!/usr/bin/env python3
"""
Publish synthetic seat-sensor traces, so the funnel can be exercised with no
boat and no hardware.

    python tools/fake_sensor.py --rowers 8 --hz 20
    python tools/fake_sensor.py --rowers 4 --hz 20 --drop 0.02   # lossy radio
    python tools/fake_sensor.py --boat-imu                       # + boat/imu

It emits exactly the contract in the README: one JSON object per publish, with
`seq` as a gapless counter and `t_ms` as the device uptime at the sampling
instant. --drop skips publishes *without* skipping seq, which is what a real
dropped packet looks like, so /streams should show the loss in `n_gap`.

Argparse rather than environment variables, because this one is a hand-run CLI
like the other scripts in this repo -- it is the funnel itself, being a
container, that is configured by environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def seat_distance_mm(t: float, seat: int, spm: float) -> int:
    """A plausible seat trace: the slide swept roughly sinusoidally at `spm`
    strokes per minute, with each seat slightly out of phase with the last so
    the data is not eight identical copies.

    Real seat motion is not a sine -- the drive is faster than the recovery --
    but this is for exercising the pipeline, not for validating a metric.
    """
    phase = (seat - 1) * 0.12
    cycles = t * (spm / 60.0)
    mid, amp = 510.0, 240.0
    return int(mid + amp * math.sin(2 * math.pi * (cycles - phase)))


async def publish_seat(client, seat: int, args, t0: float) -> None:
    period = 1.0 / args.hz
    seq = 0
    topic = f"rower/{seat}/seat"

    await client.publish(f"rower/{seat}/status", json.dumps(
        {"up": True, "dev": f"fake{seat:02d}", "fw": "sim-0.1",
         "hz": args.hz}).encode(), qos=1, retain=True)

    deadline = time.monotonic() + period
    while True:
        now = time.monotonic()
        if now < deadline:
            await asyncio.sleep(deadline - now)
        deadline += period
        if time.monotonic() - deadline > period:
            deadline = time.monotonic() + period      # fell behind; resync

        t = time.monotonic() - t0
        seq = (seq + 1) % (2 ** 32)
        d = seat_distance_mm(t, seat, args.spm)
        if args.drop and random.random() < args.drop:
            continue        # dropped in flight: seq still advanced, as it would
        payload = {
            "seq": seq,
            "t_ms": int(t * 1000.0),
            "d_mm": d,
            "v_mms": int((seat_distance_mm(t + 0.01, seat, args.spm) - d) * 100),
            "dev": f"fake{seat:02d}",
        }
        await client.publish(topic, json.dumps(payload).encode(), qos=0)


async def publish_boat_imu(client, args, t0: float) -> None:
    """A boat-level stream, to check that data not tied to a rower lands under
    /boat rather than under a seat."""
    period = 1.0 / args.imu_hz
    seq = 0
    while True:
        await asyncio.sleep(period)
        t = time.monotonic() - t0
        seq = (seq + 1) % (2 ** 32)
        await client.publish("boat/imu", json.dumps({
            "seq": seq,
            "t_ms": int(t * 1000.0),
            "pitch_deg": round(2.5 * math.sin(t * 0.7), 3),
            "roll_deg": round(1.2 * math.cos(t * 0.5), 3),
            "yaw_rate_dps": round(0.4 * math.sin(t * 0.3), 3),
        }).encode(), qos=0)


async def run(args) -> int:
    import aiomqtt

    t0 = time.monotonic()
    async with aiomqtt.Client(hostname=args.host, port=args.port,
                              identifier="fake-sensor") as client:
        log(f"[fake] publishing {args.rowers} seat(s) at {args.hz} Hz "
            f"to {args.host}:{args.port}"
            + (f", boat/imu at {args.imu_hz} Hz" if args.boat_imu else ""))
        tasks = [asyncio.create_task(publish_seat(client, seat, args, t0))
                 for seat in range(1, args.rowers + 1)]
        if args.boat_imu:
            tasks.append(asyncio.create_task(publish_boat_imu(client, args, t0)))
        await asyncio.gather(*tasks)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Publish synthetic smart-rower sensor data over MQTT.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--host", default="localhost", help="MQTT broker host")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--rowers", type=int, default=8,
                   help="how many seats to publish, numbered from 1 (bow)")
    p.add_argument("--hz", type=float, default=20.0,
                   help="per-seat sample rate; 20 Hz matches the real sensor")
    p.add_argument("--spm", type=float, default=28.0,
                   help="strokes per minute in the synthetic trace")
    p.add_argument("--drop", type=float, default=0.0,
                   help="fraction of publishes to skip without skipping seq, "
                        "so the funnel's n_gap counter can be checked")
    p.add_argument("--boat-imu", action="store_true",
                   help="also publish a boat-level stream on boat/imu")
    p.add_argument("--imu-hz", type=float, default=50.0,
                   help="rate for boat/imu, deliberately unequal to --hz")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if sys.platform == "win32":
        # Windows defaults to the Proactor loop, which has no add_reader --
        # the mechanism paho uses to watch its socket under aiomqtt. Without
        # this the connection times out with a NotImplementedError traceback.
        # Same reason as the note in funnel/__main__.py.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log("[fake] stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
