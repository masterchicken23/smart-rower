#!/usr/bin/env python3
"""
Publish a synthetic rower's seat sensor *and* oar camera from one stroke
model, so the form plot (/rower/<seat>/form) can be exercised with no boat.

    python tools/fake_form.py                           # seat 1, cam1 on :5561
    python tools/fake_form.py --rowers 4 --outliers 0.05
    python tools/fake_form.py --estimate                # + the proposed `oar`

Then run the funnel against it:

    FUNNEL_MQTT_HOST=127.0.0.1 FUNNEL_APRILTAG_ENABLED=true \\
    FUNNEL_APRILTAG_CAMERAS=cam1:1 FUNNEL_APRILTAG_CONNECT=tcp://127.0.0.1:5561 \\
    python -m funnel

and open tools/form_viewer.html.

Seat N publishes MQTT `rower/N/seat` exactly as fake_sensor.py does, and binds
a ZMQ PUB on 556N carrying `apriltag` records from camera `camN` in the shape
of vision/april-detect's output contract. The oar tag rides low through the
drive (blade buried) and lifts on the recovery, so the form plot is a loop.
Each stroke varies a little in rate, reach and blade depth; --outliers makes
the occasional stroke clearly wrong (a skied catch, a washed-out finish, a
high recovery), which the plot's per-stroke deviation should pick out.

With --estimate every record also carries the `oar` object the detector is
proposed to publish (compute/form.py, "Detector contract"), with the height in
metres; the funnel uses it when FUNNEL_FORM_USE_HEIGHT_ESTIMATE=true.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from dataclasses import dataclass

W, H = 1280, 720
PX_PER_M = 600.0
REST_PX = 360.0
"""Blade at the waterline, in frame pixels from the bottom."""


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


@dataclass
class Stroke:
    t0: float
    period_s: float
    drive_frac: float
    catch_mm: float
    span_mm: float
    depth_px: float
    lift_px: float
    fault: str = ""


class StrokeModel:
    """One rower: a sequence of slightly different strokes, generated on
    demand. Queried from two publishers at different rates, always near now."""

    def __init__(self, seat: int, args, t0: float) -> None:
        self.rng = random.Random(seat)
        self.args = args
        self.strokes: list[Stroke] = []
        self._new(t0 - (seat - 1) * 0.1)

    def _new(self, t0: float) -> None:
        a, r = self.args, self.rng
        fault = ""
        if a.outliers and r.random() < a.outliers:
            fault = r.choice(["sky", "wash", "high"])
        self.strokes.append(Stroke(
            t0=t0,
            period_s=60.0 / a.spm * r.uniform(0.95, 1.05),
            drive_frac=r.uniform(0.38, 0.44),
            catch_mm=r.gauss(820.0, 6.0),
            span_mm=r.gauss(560.0, 12.0),
            depth_px=r.gauss(55.0, 3.0),
            lift_px=r.gauss(45.0, 3.0),
            fault=fault))
        del self.strokes[:-4]

    def _stroke(self, t: float) -> Stroke:
        while t >= self.strokes[-1].t0 + self.strokes[-1].period_s:
            last = self.strokes[-1]
            self._new(last.t0 + last.period_s)
        for s in reversed(self.strokes):
            if t >= s.t0:
                return s
        return self.strokes[0]

    def at(self, t: float) -> tuple[float, float]:
        """(seat distance mm, oar height px above the frame bottom) at t."""
        s = self._stroke(t)
        u = (t - s.t0) / s.period_s
        df = s.drive_frac
        if u < df:                              # drive: quick, blade buried
            p = u / df
            x = (1.0 - math.cos(math.pi * p)) / 2.0
            y = REST_PX - s.depth_px * math.sin(math.pi * p) ** 0.6
            if s.fault == "sky" and p < 0.3:
                y += 70.0 * math.sin(math.pi * p / 0.3)
            if s.fault == "wash" and p > 0.6:
                y += 45.0 * (p - 0.6) / 0.4
        else:                                   # recovery: slow, blade lifted
            p = (u - df) / (1.0 - df)
            x = (1.0 + math.cos(math.pi * p)) / 2.0
            y = REST_PX + s.lift_px * math.sin(math.pi * p) ** 0.8
            if s.fault == "high":
                y += 60.0 * math.sin(math.pi * p)
        d = s.catch_mm - x * s.span_mm
        return d, y


async def publish_seat(client, seat: int, model: StrokeModel, args,
                       t0: float) -> None:
    period = 1.0 / args.hz
    seq = 0
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
            deadline = time.monotonic() + period
        t = time.monotonic()
        seq = (seq + 1) % (2 ** 32)
        d, _ = model.at(t)
        d += model.rng.gauss(0.0, args.noise_mm)
        await client.publish(f"rower/{seat}/seat", json.dumps({
            "seq": seq, "t_ms": int((t - t0) * 1000.0), "d_mm": int(d),
            "dev": f"fake{seat:02d}"}).encode(), qos=0)


async def publish_camera(sock, seat: int, model: StrokeModel, args) -> None:
    period = 1.0 / args.fps
    cam = f"cam{seat}"
    tick = 0
    last_meta = 0.0
    deadline = time.monotonic() + period
    while True:
        now = time.monotonic()
        if now < deadline:
            await asyncio.sleep(deadline - now)
        deadline += period
        if time.monotonic() - deadline > period:
            deadline = time.monotonic() + period

        if now - last_meta > 2.0:
            last_meta = now
            await sock.send_multipart([b"apriltag_meta", json.dumps({
                "type": "apriltag_meta", "v": 1, "cam": cam,
                "detector": "fake", "frame": [W, H],
                "K_rect": [900, 0, (W - 1) / 2, 0, 900, (H - 1) / 2, 0, 0, 1],
            }).encode()])

        tick += 1
        t_mono = time.monotonic()
        _, y = model.at(t_mono)
        y += model.rng.gauss(0.0, args.noise_px)
        wall = time.time()
        tags = []
        if not (args.miss and model.rng.random() < args.miss):
            cy = H - y
            tags.append({
                "id": args.tag_id, "fam": "tag36h11", "size_m": 0.05,
                "center_px": [round(W / 2, 2), round(cy, 2)],
                "corners_px": [[W / 2 + 20, cy - 20], [W / 2 - 20, cy - 20],
                               [W / 2 - 20, cy + 20], [W / 2 + 20, cy + 20]],
                "hamming": 0, "err_px": 0.05, "ambiguity": 0.1,
            })
        rec = {"type": "apriltag", "v": 1, "cam": cam, "tick": tick,
               "seq": tick, "skipped": 0, "t": wall, "clock": "est",
               "w": W, "h": H, "n": len(tags), "tags": tags}
        if args.estimate:
            rec["oar"] = ({"height_m": round((y - REST_PX) / PX_PER_M, 4),
                           "oar_id": args.tag_id, "ref_id": 7,
                           "err_m": 0.002} if tags else None)
        await sock.send_multipart([b"apriltag", json.dumps(
            rec, separators=(",", ":")).encode()])


async def run(args) -> int:
    import aiomqtt
    import zmq
    import zmq.asyncio

    ctx = zmq.asyncio.Context.instance()
    t0 = time.monotonic()
    socks = []
    async with aiomqtt.Client(hostname=args.host, port=args.port,
                              identifier="fake-form") as client:
        tasks = []
        for seat in range(1, args.rowers + 1):
            model = StrokeModel(seat, args, t0)
            sock = ctx.socket(zmq.PUB)
            sock.setsockopt(zmq.SNDHWM, 16)
            port = args.base_port + seat
            sock.bind(f"tcp://*:{port}")
            socks.append(sock)
            tasks.append(asyncio.create_task(
                publish_seat(client, seat, model, args, t0)))
            tasks.append(asyncio.create_task(
                publish_camera(sock, seat, model, args)))
            log(f"[fake] seat {seat}: rower/{seat}/seat at {args.hz} Hz, "
                f"cam{seat} on tcp://*:{port} at {args.fps} fps")
        try:
            await asyncio.gather(*tasks)
        finally:
            for s in socks:
                s.close(linger=0)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Publish a synthetic rower's seat sensor and oar camera.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--host", default="localhost", help="MQTT broker host")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--rowers", type=int, default=1)
    p.add_argument("--base-port", type=int, default=5560,
                   help="seat N's camera binds base-port + N")
    p.add_argument("--hz", type=float, default=20.0, help="seat sensor rate")
    p.add_argument("--fps", type=float, default=30.0, help="camera rate")
    p.add_argument("--spm", type=float, default=28.0)
    p.add_argument("--outliers", type=float, default=0.05,
                   help="fraction of strokes with a deliberate fault")
    p.add_argument("--noise-mm", type=float, default=3.0)
    p.add_argument("--noise-px", type=float, default=2.0)
    p.add_argument("--miss", type=float, default=0.02,
                   help="fraction of frames with no tag detected")
    p.add_argument("--tag-id", type=int, default=0)
    p.add_argument("--estimate", action="store_true",
                   help="also send the proposed `oar` height estimate")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if sys.platform == "win32":
        # Same reason as fake_sensor.py: paho needs add_reader.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log("[fake] stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
