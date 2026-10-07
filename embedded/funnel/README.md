# funnel

Sits between the boat's sensors and the mobile app, on the Jetson Orin Nano.

```
seat sensors ──┐
boat IMU    ───┼── MQTT ──▶  ┌────────┐  ◀── ZMQ ── CV process (later)
               │             │ funnel │
               ┘             └───┬────┘
                                 │ REST :8000
                                 ▼
                            mobile app
```

Three jobs, and deliberately nothing else:

1. **Ingest** from many sources publishing at unrelated, arbitrary rates, and
   buffer them in bounded per-stream windows.
2. **Recompute** derived values on a fixed 10 Hz grid, so results have
   consistent timing regardless of when data arrived.
3. **Serve** the latest results read-only. Every request is a lookup of what
   the last tick produced — no request triggers ingest, computation or a fetch,
   and request rate is unrelated to data rate.

The funnel does no vision work. A separate process runs the model and will pipe
its results in over ZMQ; see `funnel/sources/zmq_pose.py`.

## Running it

```sh
./run.sh                                  # build + run funnel and its broker
docker compose up -d                      # same, detached
FUNNEL_RECORD_ENABLED=true ./run.sh       # debug run, recording raw samples
```

Sensors publish to the Jetson's address on `1883`; the app polls `8000`.
Interactive API docs are at `http://<jetson>:8000/docs`.

Copy `.env.example` to `.env` to override anything; every value in it is
already the default.

## MQTT contract

This is the part sensor firmware must match.

### Topics

```
rower/<seat>/<stream>        per-rower data; seat is 1-based, bow = 1
rower/<seat>/status          presence (retained + Last Will)
boat/<stream>                anything not tied to one rower
```

Seats and streams are discovered from the data. Publish to `rower/9/seat` and
seat 9 appears; there is no crew size to configure anywhere. A `seat` segment
that is not a positive integer is counted as an unknown topic and ignored.

### Samples

One JSON object per publish, **QoS 0, not retained**. Dropping beats delaying:
a sample that arrives late is worth less than the next one arriving on time.

```
topic:    rower/3/seat
payload:  {"seq": 412, "t_ms": 183450, "d_mm": 812, "v_mms": -350}
```

| field    | type   | required | meaning                                                      |
| -------- | ------ | -------- | ------------------------------------------------------------ |
| `seq`    | uint32 | yes      | gapless per-device counter, so dropped samples are countable |
| `t_ms`   | uint32 | yes      | device `millis()` **at the moment of measurement**           |
| `d_mm`   | int    | yes      | seat distance, millimetres                                   |
| `v_mms`  | int    | no       | device-computed velocity, mm/s                               |
| `dev`    | string | no       | device id (MAC suffix); diagnostics only                     |
| `t`      | float  | no       | unix epoch seconds, if the device is NTP-synced              |

`seq`, `t_ms`, `t` and `dev` are the envelope. **Every other key is passed
through to clients untouched**, so adding a measurement to a sensor needs no
change here — publish `{"seq":…, "t_ms":…, "force_n": 231}` and `force_n`
appears in the API.

Two of these deserve an explanation, because they are the fields the current
hardware does not send yet.

**Why `t_ms` must be the sampling instant.** In the existing pipeline
(`../experiments/sensor-esp-pipeline/`) the Pico's measurement time is dropped
at the ESP hop, and `receiver.ino` restamps with its own `millis()` when the
ESP-NOW packet lands. Those timestamps therefore carry radio latency and the
receiver's boot epoch, and the real sampling time is unrecoverable. Sending
uptime-at-measurement fixes it at the source. The funnel maps it onto host time
by tracking the *minimum* `host_wall − t_ms` over a sliding window: transport
delay is non-negative, so the minimum is the best estimate of the true offset
and the fastest-travelling sample sets it. No NTP needed. (If the device does
know epoch time, send `t` and it is believed directly.)

**Why `seq`.** Without it, a dropped sample is invisible — the data just looks
slightly slower. With it, `/streams` reports `n_gap` and you can tell a weak
radio link from a slow sensor. A sender restarting resets `seq` to 0, which is
recognised rather than charged as four billion lost samples.

### Status

```
topic:    rower/3/status     QoS 1, retained
payload:  {"up": true, "dev": "ce40d0", "fw": "1.0", "hz": 20}
```

Published on connect, with the sensor's MQTT **Last Will** set to
`{"up": false}` on the same topic. This distinguishes "the sensor is gone" from
"the sensor is connected but silent", which sample age alone cannot.

## REST API

All `GET`, all lookups of the last tick. `GET /` lists the endpoints.

| endpoint                   | purpose                                               |
| -------------------------- | ----------------------------------------------------- |
| `/snapshot`                | **everything, in one response** — the app's endpoint   |
| `/rower/{seat}/position`   | **derived seat position** — the value to display       |
| `/rowers`                  | seats discovered, with age and presence               |
| `/rower/{seat}`            | every block for one seat, raw and derived             |
| `/rower/{seat}/{stream}`   | one raw stream for one seat                           |
| `/rower/{seat}/raw`        | recent raw samples (debug; `?stream=`, `?window_s=`)  |
| `/boat`                    | all boat-level blocks                                 |
| `/boat/{stream}`           | one boat-level stream                                 |
| `/streams`                 | every stream's rate, age and drop counters            |
| `/health`                  | data health; `503` when nothing fresh is arriving     |
| `/livez`                   | process liveness only                                 |

### Raw streams vs derived values

The API exposes two different kinds of thing, and the distinction is the point:

- **Raw stream blocks** (`/rower/3/seat`, `/boat/imu`) are whatever a sensor
  published, passed through untouched. A new sensor shows up here the moment it
  starts publishing, with no code change. They are for debugging and for
  bring-up — **not for display**.
- **Derived blocks** (`/rower/3/position`) are the output of an explicit
  transformation, with a pinned schema. These are what a client consumes.

A raw HC-SR04 distance is not a seat position: its zero is wherever the sensor
happens to be bolted, it contains isolated wild readings, and it is in
millimetres of distance rather than fraction of slide travel. So the app reads
`/rower/{seat}/position`, never `/rower/{seat}/seat`.

### Staleness, and why the codes differ per endpoint

A reading that stopped updating looks identical to a reading that is not
changing — and the second is legitimate. So past `FUNNEL_MAX_AGE_MS` (1 s):

- **Per-resource endpoints answer `503`** with an empty body, rather than
  serving an old number as current. Following `pose_api.py`, which made the
  same call.
- **`/snapshot` answers `200`** with `"stale": true` inside the affected
  stream's block. A dashboard should grey out one tile when one seat sensor
  dies, not fail its whole poll.
- **`/rower/{seat}` is `404`** for a seat that has never published, which is a
  different thing from a known seat gone quiet (`503`).

Diagnostics ride in headers so bodies stay clean: `X-Funnel-Status`
(`ok`/`stale`/`no-data`/`starting`), `X-Funnel-Age-Ms`, `X-Funnel-Tick`.

`/health` vs `/livez` matters for operations: `/health` is about the *data* and
goes `503` whenever the sensors are off, so the container healthcheck probes
`/livez` instead. Probing `/health` would restart a perfectly healthy funnel
every time the boat was idle. A rising `tick` with empty blocks means the
sensors are quiet; a frozen `tick` means the service is dead.

## Where the computation goes

### Seat position — `funnel/compute/seat_position.py`

**This is the seam, and every stage in it is currently an identity no-op.**
Implement them there; nothing outside that file needs to change.

```
calibrate  ->  despike  ->  smooth  ->  normalize
                                    \->  velocity
```

| stage       | what it must do                                                       |
| ----------- | --------------------------------------------------------------------- |
| `calibrate` | raw distance → travel from the catch; drop readings on the sensor clamp |
| `despike`   | reject isolated wild values (median over a short time span)            |
| `smooth`    | low-pass with a cutoff in Hz, state carried across ticks               |
| `normalize` | travel → 0.0 at the catch, 1.0 at the finish                           |
| `velocity`  | differentiate the *filtered* series, not the sensor's own figure       |

Order matters: despike before smooth, because one spike through a low-pass
contaminates many outputs; velocity after smooth, because differentiation
amplifies noise.

The response shape is pinned now, so the mobile side can be written against it
today. Until the stages are implemented, `/rower/{seat}/position` reports
`ready: false` and lists the identity stages in `pending`, `position` is `null`
(a fraction from a wrong span is worse than an honest gap), and `travel_mm`
carries the unreferenced raw reading with `calibrated: false` beside it.
`/health` shows every stage's status under `pipelines`.

Per-stream state that must survive between ticks — a filter's previous output,
an observed calibration range — goes in `buf.state`, keyed by stage name. It
lives beside the samples it derives from, so a transformation needs no registry
and disappears with its stream.

### Everything else — `funnel/compute/metrics.py`

That file assembles the tick: it decides what each seat and the boat expose and
delegates the mathematics. Rules for anything added: no `await`, no I/O, read
buffers through `buf.window(seconds)`, return fresh dicts, never raise on
missing data. It runs on the event loop, which is what lets the whole service
work without locks.

Stroke metrics (phase, rate, length) should be built on the **derived** position
rather than the raw stream — that is why the pipeline exists.
`../experiments/sensor-esp-pipeline/plotdistance.py` is the starting point (a
5 mm deadband for DRIVE/RECOVERY, strokes-per-minute over a 10-stroke window),
but it is not portable as written: its deadband is a per-sample delta, so it
silently assumes the sensor's 20 Hz rate. Express thresholds as mm/s and
cutoffs in Hz and that assumption goes away.

## Design notes

**One process, one event loop.** Ingest, the compute loop, the recorder and the
request handlers all share it. That is why there are no locks anywhere: buffer
mutation never spans an `await`, and the compute loop publishes results by
swapping a single `Snapshot` reference, so a reader sees the previous tick or
this one and never a half-built mixture. Two consequences: nothing in a tick or
a handler may block, and `workers=1` is not a limitation to be tuned away — a
second worker would serve its own empty copy of the in-memory buffers.

**The tick is deadline-driven.** `while True: work(); sleep(period)` makes the
real period `period + work`, so it drifts. The loop in `compute/loop.py` targets
absolute deadlines on a fixed grid, and when a tick overruns it *skips* the
grid points it missed instead of running them back-to-back to catch up.
Ported from `pose_stream.py`. `/health` reports `jitter_ms` and `n_late`.

**Nothing applies back-pressure to ingest.** Full buffers evict the oldest
sample; the recorder queue drops the oldest entry. Falling behind is worse than
losing history.

**A bad metric does not stop the clock.** A raising `compute` is counted in
`/health`'s `n_errors` and the previous snapshot keeps being served.

## Recording and replay (debug only)

Off by default. When off the recorder is never constructed and ingest's only
cost is one `is not None` test per sample.

```sh
FUNNEL_RECORD_ENABLED=true docker compose up     # writes ./data/<utc>/
```

One JSONL file per stream plus a `meta.json` holding the settings used — the
same role as `../experiments/imu-recording/recordings/`, in a format that does
not assume a fixed column set. Then replay it, which takes the place of MQTT
ingest entirely:

```sh
FUNNEL_REPLAY_PATH=/data/20261005T194600Z docker compose up
```

Relative sample spacing and the interleaving between streams are preserved, so
a 20 Hz seat sensor and a 50 Hz IMU come back at 20 and 50 Hz. This is how to
work on the metrics with no boat and no sensors.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                      # needs no broker and no sensors
```

The suite covers payload/topic decoding, buffer eviction and time-windowing,
clock-offset estimation, the scheduler's grid behaviour under a simulated
overrun, the seat-position pipeline's output contract, and every endpoint's
shape and status code over in-process ASGI.

`tests/test_seat_position.py` is deliberately the harness for the unimplemented
stages: it pins what must hold regardless of what the transformation ends up
doing — the output shape with and without data, the honesty of the
`ready`/`pending` flags, rejection of non-numeric readings, and state surviving
between ticks.

To exercise the real MQTT path without Docker, run any broker on 1883 and:

```sh
python -m funnel &
python tools/fake_sensor.py --rowers 8 --hz 20 --drop 0.02 --boat-imu
```

`--drop` skips publishes *without* skipping `seq`, which is what a lost packet
looks like, so `/streams` should show the loss in `n_gap`. If you have no
broker to hand, `pip install amqtt` provides a pure-Python one.

Note `python -m funnel` is supported on Windows for development: it selects the
selector event loop explicitly, because Windows defaults to the Proactor loop,
which lacks the `add_reader` that paho uses to watch its socket — the symptom
is a broker connection that times out while the service happily serves 503s.

## Conventions, and three departures from the rest of the repo

Kept: `log()` to stderr with `[subsystem]` prefixes, full type hints, deferred
heavy imports, unit-suffixed names, drop-oldest queues, 503-on-stale.

Changed, deliberately:

1. **FastAPI, where `pose_api.py` says "stdlib only apart from pyzmq — no web
   framework to install on the Jetson".** Containerising removes that
   constraint: dependencies are baked into the image and never installed
   against JetPack's system Python. In exchange, `/docs` becomes the contract
   the mobile side is written against.
2. **Environment variables rather than argparse.** Compose is the configuration
   surface for a container. Hand-run tools under `tools/` still use argparse,
   like the rest of the repo.
3. **First `Dockerfile`, `pyproject.toml` and tests in the repo.** `run.sh`
   keeps the shape of the other experiments' launch scripts.
