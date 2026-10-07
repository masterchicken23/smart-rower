# april-detect

AprilTag detection for the boat's cameras, on the Jetson Orin Nano. One
container per camera.

```
camera ── imageZMQ JPEG ──▶ ┌──────────────────────────────────────────────┐
(PUB :5555, + timing hdr)   │ decode ▶ undistort ▶ detect ▶ pose (6-DoF)   │ ── ZMQ PUB :556N ──▶ funnel
                            └──────────────────────────────────────────────┘     apriltag / apriltag_meta
```

Per frame, it:

1. **Receives** a JPEG over imageZMQ and stamps the arrival time.
2. **Decodes** it to greyscale, and **corrects lens distortion** using the
   camera's calibration from configuration.
3. **Detects** AprilTags (tag36h11 by default; any family on the CPU detector).
4. **Solves each tag's pose**: position in metres and rotation in the camera
   frame.
5. **Publishes** one record to the funnel, carrying the frame's full timeline
   from capture on the camera to publication here.

It is built to the same pattern as `embedded/funnel`: env-var configuration,
stderr logging with `[subsystem]` prefixes, a slim non-root image, drop-rather-
than-queue everywhere, socket-free functions under test, and `tools/` for
hand-run scripts with argparse.

Two documents are contracts with other components:

- **[SENDER.md](SENDER.md)**: what a camera must send, including the per-frame
  timing header that imageZMQ does not carry.
- **[Output contract](#output-contract)** (below): what the funnel receives.

## Running it

```sh
cp .env.example .env              # camera addresses, calibration paths
./run.sh                          # build + run all four
./run.sh -d april-cam1            # one camera, detached
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up --build   # GPU detector
```

Each `april-camN` connects to `CAMN_SOURCE` and publishes on port `556N` of the
Jetson (host networking; see the comment in `docker-compose.yml`). Logs
include a stats line every 5 s:

```
[stats] in  15.0/s out  15.0/s busy  19%  skipped 0  bad 0  err 0  tags/frame 1.00 | ms: decode  2.7 undistort  3.6 detect  5.8 pose  0.3 proc 12.5 (proc max 16.4) | e2e p50  22.1 p95  24.9  net p50   0.3  recv->pub p95  15.9
```

`busy` is the fraction of wall time spent processing. Past ~80% the stream is
about to start skipping frames.

### Calibration

Put a calibration per camera in `config/` and point `CAMN_CALIB` at it
(`/config/cam1.yaml`). The format is ROS `camera_info` YAML, which is what
standard calibration tools write. JSON in the same layout and the `.npz` from
`experiments/april-tag` also work; see `config/example-camera.yaml`. Pinhole
(`plumb_bob`, `rational_polynomial`) and fisheye (`equidistant`) models are
supported. Calibrating at 1080p and streaming at 720p is fine, because K is
scaled. A different aspect ratio is refused, because it means a different
crop of the sensor.

The calibration can also be set inline (`APRIL_CAMERA_MATRIX="fx fy cx cy"`,
`APRIL_DIST_COEFFS`, `APRIL_DIST_MODEL`, `APRIL_CALIB_SIZE`). With no
calibration at all, it assumes a 70° HFOV and logs a warning. Positions are
then approximate, and meta reports `"calibrated": false`.

`APRIL_UNDISTORT` chooses how the correction is applied:

| mode | what | when |
| --- | --- | --- |
| `image` (default) | remap every frame, then detect | wide lenses. AprilTag fits straight edges to find quads, and barrel distortion bends them, so detection near the frame edge suffers if the image is not corrected first |
| `points` | detect on the raw frame, undistort the 4 corners | mild distortion. Same pose accuracy, saves the remap (~25% of per-frame CPU) |
| `none` | ideal pinhole | already-rectified sources |

Every setting is in `april_detect/config.py` with its rationale.

## Output contract

This is what the funnel consumes. **The funnel's ZMQ source is not
implemented yet** (`funnel/sources/zmq_pose.py` is a documented stub), so
this section is the spec it should be written against. It follows that
stub's stated contract wherever the stub has one.

### Transport

| | |
|---|---|
| socket | ZMQ **PUB**, `SNDHWM` 16, drops rather than buffers (a slow funnel costs records, never detector latency) |
| framing | two-frame multipart `[topic, compact_json]`, as `zmq_pose.py` specifies |
| topology | each detector **binds** `tcp://*:556N`; the funnel **connects**, as `zmq_pose.py` specifies (`FUNNEL_ZMQ_SUBSCRIBE`, default port 5557). `APRIL_OUT_BIND=false` reverses it for a funnel that binds one SUB for every detector |
| topics | `apriltag`: one record per processed frame. `apriltag_meta`: camera model and conventions, every 2 s |

Subscribing to the prefix `apriltag` receives both topics. Dispatch on the
exact topic. One SUB socket can `connect()` to all four detectors, and that is
the recommended shape for the funnel.

### `apriltag` record

```json
{
  "type": "apriltag", "v": 1,
  "cam": "cam1",
  "tick": 1842,
  "seq": 18342,
  "skipped": 0,
  "t": 1760000000.0005,
  "clock": "est",
  "w": 1280, "h": 720,
  "ts": {"cap": 1760000000.0005, "send": 1760000000.0093, "recv": 1760000000.0096,
         "start": 1760000000.0098, "pub": 1760000000.0251},
  "ms": {"enc": 8.8, "net": 0.3, "queue": 0.2, "decode": 2.5, "undistort": 4.4,
         "detect": 7.7, "pose": 0.5, "proc": 15.3, "total": 15.5, "e2e": 24.6},
  "n": 1,
  "tags": [{
    "id": 0, "fam": "tag36h11", "size_m": 0.1,
    "pos_m": [0.1117, 0.0201, 1.0661],
    "quat": [0.98821, 0.09373, 0.11874, 0.0237],
    "euler_deg": [11.31, 13.31, 4.07],
    "dist_m": 1.0721,
    "center_px": [734.29, 376.17],
    "corners_px": [[778.56, 337.55], [693.8, 332.24], [690.78, 413.92], [774.02, 420.98]],
    "hamming": 0, "margin": 119.03, "err_px": 0.02, "ambiguity": 0.03
  }]
}
```

Envelope:

| field | meaning |
| --- | --- |
| `cam` | camera id (`APRIL_CAMERA_ID`, else the sender's `cam`) |
| `tick` | **gapless** per-detector counter, the drop detector between here and the funnel. Same role as `tick` in the pose records and `seq` on MQTT. Restarts at 1 when the container restarts |
| `seq` | the camera's own frame counter, passed through (`null` from a legacy sender). Gaps here mean frames lost *before* this detector |
| `skipped` | frames that reached this detector but were superseded before processing, since the previous record. Non-zero means this detector is not keeping up |
| `t` | **the frame's capture time on the Jetson's wall clock**, the timestamp to use. Falls back to receipt time when the sender sent no timing |
| `clock` | how `t` was obtained: `est`, `ntp` or `recv` (SENDER.md, "What the detector reports") |
| `ts.*` | the frame's timeline, unix seconds, host wall clock |
| `ms.*` | stage durations. `e2e` is capture → publish. `total` is receipt → publish. `null` where unknown |
| `n`, `tags` | detections. A record with `n: 0` is still sent, so "no tag in view" is distinguishable from "detector down" (`APRIL_EMIT_EMPTY`) |

Per tag:

| field | meaning |
| --- | --- |
| `pos_m` | tag centre in the camera frame, metres |
| `quat` | rotation `[w, x, y, z]`, tag frame → camera frame. **Use this for computation** |
| `euler_deg` | the same rotation as `[rx, ry, rz]`, `R = Rz·Ry·Rx`. For display only; singular at `ry = ±90°` |
| `dist_m` | ‖pos‖ |
| `center_px`, `corners_px` | in the *corrected* image, whose intrinsics are `K_rect` in meta. Corner order is AprilTag's |
| `hamming` | bits corrected in decoding. Detections above `APRIL_MAX_HAMMING` (1) are dropped |
| `margin` | decode decision margin (CPU detector only). Low values are weak decodes |
| `err_px` | RMS reprojection error of the chosen pose |
| `ambiguity` | best ÷ second-best pose error. **Near 1, the rotation is unreliable**: two tilts explain the corners equally well. See [Accuracy](#accuracy) |

Conventions, as also sent in every meta record:

- **Camera frame:** OpenCV, +x right, +y down, +z out of the lens.
- **Tag frame:** origin at the tag centre, +x toward the tag's right edge, +y
  toward its bottom edge, +z into the tag, with the tag read upright. **An
  upright tag squarely facing the camera has identity rotation.** That is
  `quat = [1,0,0,0]` and `euler_deg = [0,0,0]`.
- The CPU AprilTag library's native frame is rotated 180° about z from this
  one. `experiments/april-tag` found that by hand with real tags, and
  `tests/test_pipeline.py` pins it against rendered ground truth. Every
  backend reports the frame above.

### `apriltag_meta`

Every `APRIL_META_PERIOD_S` (2 s), because a subscriber that joins late would
otherwise never learn how to interpret corners. It carries `cam`, `detector`,
`families`, `tag_size_m`/`tag_sizes`, `undistort`, `pose_source`, `frame`
`[w, h]`, `K_rect` (row-major 3×3), the full `calib` (`K`, `D`, `model`,
`size`, `source`, `calibrated`), `clock_resets`, `version`, and the
`conventions` above as strings.

### Suggested mapping into the funnel

The record is shaped so the funnel's existing model needs no new concepts:

- `Sample.t_src = rec["t"]`: already on the host wall clock, so no
  `ClockEstimator` is needed (same machine).
- `Sample.seq = rec["tick"]`: gapless, so `n_gap` works unchanged.
- `Sample.dev = rec["cam"]`.
- **Key**: `StreamKey("boat", None, f"apriltag_{cam}")` for the frame-level
  record. A tag id is a stable physical identity, so mapping tag → seat is a
  static table and could live in either process. The funnel is the natural
  owner, because it already owns seats.

Three small changes on the funnel side: accept a comma-separated
`FUNNEL_ZMQ_SUBSCRIBE` (one SUB, `connect()` to each), subscribe to prefix
`apriltag`, and add `pyzmq` to its `requirements.txt`. That last one is
already flagged there.

## Timing

Every frame is tracked from capture to publish. See the timeline diagram in
[SENDER.md](SENDER.md#what-the-detector-reports-from-it). In short:

- **Camera side** (`ms.enc`, `ms.net`): only knowable if the camera stamps
  its frames. imageZMQ doesn't, so SENDER.md defines a header that fits inside
  imageZMQ's own `msg` field without changing the library. The camera's clock
  is mapped onto the Jetson's with no clock sync, using the funnel's
  minimum-offset estimator.
- **Detector side** (`queue` → `pub`): one wall-clock anchor per frame, taken
  in a receive thread that does nothing else. Every later stamp is placed by
  the monotonic clock from that anchor, so a wall-clock step cannot produce a
  negative duration.
- **Funnel side**: `funnel_receipt − ts.pub` is the delivery delay, measured
  on one clock. `tools/tap.py` reports it as `deliv`, at ~1.5 ms p95 in local
  testing.

Measured end to end on a laptop (fake camera with a simulated 8 ms encoder →
detector → tap, over real ZMQ sockets, 720p with barrel distortion): **e2e
p50 22 ms, p95 25 ms**, of which 8.6 ms is the simulated encoder.

## Throughput

**Requirement:** 4 streams × 15 FPS × 1280×720 on one Orin Nano, which is 60
frames/s total, or a 66.7 ms budget per frame per stream.

**Verdict:** attainable on the CPU detector alone, with the GPU left to the
pose model. **This was not measured on a Jetson here**, because none was
available. The figures below are laptop measurements scaled by a stated
factor. `tools/bench.py` is the one-command check on the device; see
[Verifying it on the Jetson](#verifying-it-on-the-jetson).

### Where the time goes

Single-threaded, so wall time ≈ CPU time, on an Intel Core Ultra 7 155H. The
input is 720p, JPEG q85, one tag, barrel distortion corrected in `image` mode,
with default settings (`decimate 2`, `sigma 0.8`):

| sensor noise σ | decode | undistort | detect | pose | **total** |
| --- | --- | --- | --- | --- | --- |
| 1 (bright) | 1.7 | 3.0 | 4.5 | 0.3 | **9.5 ms** |
| 2 (small sensor in shade) | 2.9 | 4.3 | 6.6 | 0.3 | **14.1 ms** |
| 3 (poor light) | 4.0 | 5.3 | 10.8 | 0.4 | **20.6 ms** |

The surprise in that table is that **detection cost is driven by sensor
noise, not resolution**. Noise fragments AprilTag's thresholded image into
thousands of tiny segments to cluster. With the default `quad_sigma 0.8` off,
a σ=2 frame took 20 ms to detect instead of 4.9 ms (measured), which is why
the blur is on by default. It costs ~1 ms on a clean frame and buys a cost
that stays flat as light falls.

### Projected onto the Orin Nano

The Orin Nano has 6× Cortex-A78AE at 1.5 GHz (1.7 GHz in Super mode). Taking a
**4×** single-thread slowdown against the laptop's P-cores is deliberately
pessimistic. Using the σ=2 row:

- **CPU:** 14.1 ms × 4 ≈ **56 ms of CPU per frame** × 60 frames/s ≈ **3.4 of
  6 cores**. At σ=3 it is 4.9 cores, which is tight. At σ=1 it is 2.3.
- **Latency per stream:** decode and remap are serial and detection is split
  over `APRIL_THREADS=2`, so processing takes roughly 30 + 13 ≈ **43 ms**
  against the 66.7 ms period. Each stream keeps up, with ~35% slack.

That is a pass with margin at typical noise, and close to the limit in poor
light. These levers are ordered by cost to accuracy:

| lever | saves | cost |
| --- | --- | --- |
| greyscale JPEG from the camera (SENDER.md) | ~20% of link bandwidth (measured); no decode time, since decode is already luminance-only | none |
| `APRIL_UNDISTORT=points` | the remap, ~25–30% of per-frame CPU (measured: 14.1 → 10.6 ms) | detections lost near the edge of a strongly distorted frame |
| `docker-compose.cuda.yml` (cuAprilTags) | detection, ~45% of CPU, moves to the GPU | 4 CUDA contexts share the GPU with the pose model; tag36h11 only |
| `APRIL_DECODE_SCALE=2` | ~⅔ of per-frame CPU (measured: 5.1 → 1.8 ms, undistorted) | half the range: the smallest detectable tag doubles in pixels |

About the GPU path: NVIDIA's Isaac ROS benchmarks list its AprilTag node, the
same cuAprilTags library, at **~120 FPS for 720p on an Orin Nano Super**.
That is twice the 60 FPS needed, but it would be sharing the GPU with the pose
model. Hence the CPU is the default and the GPU is the reserve.

### What limits it other than compute

The most likely thing to break 4 × 15 FPS is **the camera link, not the
Jetson**. At 720p, real-scene JPEGs run 60–150 KB, which is 7–18 Mbit/s per
camera and up to ~70 Mbit/s for four. Pi Zero W / Zero 2 W cameras are
2.4 GHz-only, and four of them on one channel will not sustain that. Cap the
frame size (`pi_sender.py --target-kb 35` gives ~17 Mbit/s total) or wire the
cameras. Tag pose was insensitive to JPEG quality down to q20 in synthetic
tests (SENDER.md). The camera also has to *produce* 720p15 JPEG. On a Zero W
that means its GPU encoder (`pi_sender.py`'s `picamera` backend), not
software encoding.

### Verifying it on the Jetson

```sh
docker compose run --rm april-cam1 python tools/bench.py             # 4 x 15 fps, 720p
docker compose run --rm april-cam1 python tools/bench.py --noise 3   # poor light
APRIL_UNDISTORT=points docker compose run --rm -e APRIL_UNDISTORT april-cam1 python tools/bench.py
```

`bench.py` runs N worker processes, each standing in for one container, at
the camera rate with latest-wins semantics, through the real
`Pipeline.process()` and configured from the same `APRIL_*` variables. It
prints per-stream FPS, skips, latency percentiles, CPU per frame, and
**PASS/FAIL**. On the laptop it passes at 60.0 FPS total with 0 skipped. For
the full path including the network, run four `tools/fake_camera.py`
instances on another machine and watch `tools/tap.py`.

## Accuracy

- **Angle tags away from the camera axis.** Face-on, a planar tag has two
  near-equally-good tilts (the AprilTag "flip"). On clean synthetic frames a
  face-on tag's tilt was off by 2–5° with `ambiguity` 0.2–0.8, while a tag at
  25° was within ~1° with `ambiguity` ≤ 0.08. Position is unaffected. Mount
  tags 20–30° off-axis if rotation matters, and treat `ambiguity > ~0.5` as
  "rotation unknown".
- **Range** at `decimate 2`: on synthetic frames (σ=2 noise, 20° yaw) a tag
  was found 10/10 times down to 18 px across, i.e. a 10 cm tag at 5 m with
  f≈900 px. `decimate 1` did not extend that, at 2–3× the detection cost. Real
  motion blur and focus will shorten the range. Measure with the real optics
  before changing it.
- **Ground truth:** through the full pipeline with barrel distortion, the
  synthetic moving tag measured **pos p95 ≤ 3 mm, rot p95 ≤ 0.6° at ~1 m**.
- `APRIL_TAG_SIZE_M` is the edge of the black square, not the printed paper.
  Pose scales linearly with it.

## Design notes

**Latest-wins, never a queue.** The receive thread overwrites a one-slot
mailbox. This is the trade `pose_stream.py`'s `LatestSlot` and the funnel's
buffers make: a stale frame is worth less than the next, and a queue would
turn one slow frame into permanent lag. Overwritten frames are counted
(`skipped`), so falling behind is visible.

**Event-driven, not on a tick.** `pose_stream.py` runs on a fixed grid
because it low-pass filters keypoints, which needs uniform spacing. This
does no temporal filtering, so it processes each frame on arrival and adds no
scheduling latency. Every record carries its capture time, and the funnel's
compute loop resamples onto its own grid anyway.

**Pose from corners (IPPE), not the detector.** There is one solver and one
convention whichever backend found the corners, per-id tag sizes work, and
it gives the second solution's error, which is what `ambiguity` is.
`APRIL_POSE_SOURCE=detector` uses the library's estimate instead, and a test
pins that both agree. The CUDA backend uses cuAprilTags' own pose, because
its corner order has not been checked against ground truth. That needs the
library and a GPU, and `tools/tap.py --truth` against `fake_camera.py` is the
check on the Jetson.

**One container per camera.** A crash or a misbehaving camera costs one
stream. Each can be restarted or recalibrated on its own, and there is no
shared state to coordinate.

**Liveness, not data, in the healthcheck.** The main loop touches a heartbeat
file every second whether or not frames arrive, so a camera that is switched
off does not restart a healthy detector. The funnel's `/livez` vs `/health`
makes the same split.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest            # no camera, GPU, Docker or funnel needed
.venv/bin/ruff check .
```

The suite (50 tests) renders tags at known poses through known lenses, with
OpenCV's aruco module, which carries the AprilTag dictionaries bit-for-bit.
It checks:

- the tag-frame convention and rotations against ground truth;
- that undistortion corrects pinhole and fisheye lenses (in both `image` and
  `points` modes), and, as a control, that *skipping* it is measurably wrong;
- calibration scaling across resolutions, reduced decode, per-id sizes and
  allow-lists;
- the record and meta shapes and every timing field's ordering;
- the clock estimator, including a sender reboot;
- wire compatibility, by sending through imageZMQ's real `send_jpg`.

The pipeline runs on Windows for development:

```sh
python tools/fake_camera.py --bind tcp://127.0.0.1:5555 --name cam1 --distort=-0.3,0.09,0,0,0 --encode-ms 8
# matching calibration: python tools/fake_camera.py --print-env --distort=-0.3,0.09,0,0,0
APRIL_SOURCE=tcp://127.0.0.1:5555 APRIL_OUT_ADDRESS=tcp://127.0.0.1:5561 \
  APRIL_CAMERA_MATRIX="900 900 639.5 359.5" APRIL_DIST_COEFFS="-0.3 0.09 0 0 0" \
  APRIL_CALIB_SIZE=1280x720 APRIL_HEARTBEAT_PATH= python -m april_detect
python tools/tap.py tcp://127.0.0.1:5561 --truth
```

## Conventions, and departures from the funnel

Kept: `log()` to stderr with `[subsystem]` prefixes, env-var settings with
unit-suffixed names, deferred heavy imports, drop-oldest everywhere, the
funnel's clock estimator and lint config, `python:3.11-slim`, non-root, deps
before source, and `run.sh`.

Changed, deliberately:

1. **A builder stage in the Dockerfile.** pupil-apriltags publishes no
   aarch64 wheel, so it is compiled on the Jetson. The toolchain stays out of
   the runtime image.
2. **No HTTP.** This is a ZMQ publisher with nothing to serve, so liveness is
   a heartbeat file rather than a `/livez` endpoint.
3. **`requires-python >= 3.10`**, not 3.11, because the CUDA image is built
   on L4T's Ubuntu 22.04.
4. **Host networking**, where the funnel publishes ports. The funnel is a
   separate compose project and expects to reach the vision feed on
   `127.0.0.1`.
