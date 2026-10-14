# Jetson connection and REST API

How the mobile app gets data from the boat's Jetson: what runs there, how the
phone reaches it, which endpoints to call, and what every field means.

Written for both people and coding agents. The source of truth is the Jetson
code itself, so if this file and the code disagree, the code wins and this file
should be updated:

| What | Where |
| --- | --- |
| REST service ("the funnel") | [`embedded/funnel/`](../embedded/funnel/README.md), schemas in `embedded/funnel/funnel/models.py` |
| Live schema on a running Jetson | `http://<jetson>:8000/docs` and `/openapi.json` |
| AprilTag record format | [`vision/april-detect/README.md`](../vision/april-detect/README.md), "Output contract" |
| App-side client | [`src/data/funnel/`](src/data/funnel/) (`types.ts`, `client.ts`, `store.ts`) |

Last checked against the funnel at commit `6f105d9` (2026-10-08).

---

## 1. The big picture

```
 ultrasonic seat sensor ──ESP──▶ MQTT :1883 ─┐
                                             │      ┌────────────── Jetson ──────────────┐
 camera (Pi Zero) ──JPEG──▶ april-detect ─ZMQ┼────▶ │ funnel                             │
                                             │      │  1. buffers every sensor stream    │
                                             ┘      │  2. recomputes everything at 10 Hz │
                                                    │  3. serves the latest result       │
                                                    └───────────────┬────────────────────┘
                                                                    │ HTTP GET, port 8000
                                                                    ▼
                                                               mobile app
```

Key facts:

- **The phone only speaks HTTP.** It never talks MQTT or ZMQ. Those carry data
  from the sensors to the Jetson.
- **One funnel per boat.** Seats are numbered `1..N`, **bow = 1**, and are
  discovered from the data. Nothing on the phone configures crew size.
- **Requests never trigger work.** The funnel computes once per tick (every
  100 ms) and every request returns that stored result. Polling faster than
  10 Hz returns the same data again; each response carries a `tick` counter so
  repeats can be skipped.
- **All processing happens on the Jetson.** The phone displays what it is
  given. It should not filter, normalise or re-derive sensor values.

---

## 2. Reaching the Jetson

| | |
| --- | --- |
| Jetson | `raytheon-rover` on the team's Tailscale network, `100.76.11.15` |
| Funnel (this API) | port **8000** |
| Auth | none; read-only, `GET` only |
| CORS | `*` by default |

**On the boat**, the phone and Jetson share the Jetson's Wi-Fi and use its
local IP (e.g. `http://10.88.164.133:8000`).

**Anywhere else**, use [Tailscale](https://tailscale.com): the private
`10.88.x.x` address only works on that Wi-Fi. Both the computer and the
**phone** need the Tailscale app, signed into the team tailnet. Then use the
Jetson's Tailscale IP:

```bash
tailscale ping raytheon-rover
```

```bash
curl http://raytheon-rover:8000/
```

Use the `100.x.x.x` IP or the short name (`raytheon-rover`) in the app, not
the long `*.ts.net` name: iOS App Transport Security blocks plain `http://` to
fully qualified domain names in a real build. `app.json` already allows local
networking (`NSAllowsLocalNetworking`) and Android cleartext traffic.

### Configuring the app

In `mobile/.env` (read at bundle time, so restart with `npx expo start -c`
after changing it):

```bash
EXPO_PUBLIC_FUNNEL_URL=http://100.76.11.15:8000
```

The Developer screen can also change the URL at runtime: tap the status pill.
That override is not persisted.

---

## 3. Endpoints

All `GET`. `GET /` lists them along with `compute_hz` and `max_age_ms`.

| Endpoint | Use it for | Status codes |
| --- | --- | --- |
| **`/snapshot`** | **Everything, in one response. The app's main poll.** | always `200` while running |
| **`/rower/{seat}/position`** | Derived seat position for one seat | `200` / `404` / `503` |
| `/rower/{seat}/apriltag` | Newest raw AprilTag record from that seat's camera | `200` / `404` / `503` |
| `/rower/{seat}/apriltag/meta` | That camera's model and conventions | `200` / `404` (never `503`) |
| `/health` | Data health, pipeline stage status, per-stream counters | `200`, or `503` **with a full body** when nothing is fresh |
| `/rowers` | Seats discovered, with age and sensor presence | `200` |
| `/rower/{seat}` | Every block for one seat, raw and derived | `200` / `404` / `503` |
| `/rower/{seat}/{stream}` | One raw stream, e.g. `/rower/1/seat` | `200` / `404` / `503` |
| `/rower/{seat}/raw?stream=&window_s=` | Recent raw samples (debug; window capped at 60 s) | `200` / `404` |
| `/boat`, `/boat/{stream}` | Boat-level streams (none in use yet) | `200` / `404` / `503` |
| `/streams` | Every stream's rate, age and drop counters | `200` |
| `/livez` | Process liveness only | `200` |
| `/docs`, `/openapi.json` | Interactive docs and machine-readable schema | `200` |

### Status codes: what they mean

| Code | Meaning | What the app should do |
| --- | --- | --- |
| `200` | Fresh data | Display it |
| `404` | That seat or stream has **never** published | Show "not present", not an error |
| `503` | Known, but older than `max_age_ms` (1 s), or no data yet. **Empty body** | Show "stale" / "no signal". Never show the last value as current |
| network error / timeout | Phone can't reach the funnel | Show "unreachable" and retry |

`/snapshot` is the exception: it never `503`s for staleness. It answers `200`
and sets `"stale": true` inside the affected block, so one dead sensor greys out
one tile instead of failing the whole poll.

### Diagnostic headers

| Header | Values |
| --- | --- |
| `X-Funnel-Status` | `ok`, `stale`, `no-data`, `starting`; on `/snapshot` also `stale-tick` (the compute loop has stalled, a serious fault) |
| `X-Funnel-Age-Ms` | age of the data served |
| `X-Funnel-Tick` | tick that produced it |

A rising tick with empty seats means the sensors are quiet. A frozen tick means
the service is broken.

---

## 4. `/snapshot`, the endpoint the app polls

```jsonc
{
  "tick": 136840,               // increments every 100 ms; skip repeats
  "t": 1791483256.89,           // tick wall-clock time, unix seconds
  "age_ms": 12.3,               // how old this snapshot was when served
  "rowers": {
    "1": {                      // seat number, as a STRING key (JSON)
      "position": { ... },      // derived: section 5
      "seat":     { ... },      // raw ultrasonic stream: section 6
      "apriltag": { ... }       // raw AprilTag stream: section 7
    }
  },
  "presence": {
    "1": { "up": true, "dev": "2eb968", "fw": "1.0", "hz": 20, "age_ms": 512.0 }
  },
  "streams": { "rower/1/seat": { "rate_hz": 20.0, "n_gap": 0, ... } },
  "boat": {}
}
```

- **Seat keys are strings** (`"1"`, `"2"`). Convert them to numbers.
- A seat appears in `rowers` as soon as **anything** publishes for it. A seat
  can have a camera but no ultrasonic sensor, or the reverse, so treat every
  block as optional.
- `presence` comes from each sensor's retained MQTT status topic. `up: false`
  is the sensor's Last Will: it disconnected. That's different from "connected
  but silent", which shows up only as `stale`.

---

## 5. Seat position: the value to display

`rowers[seat].position` in the snapshot, or `GET /rower/{seat}/position`.
**Pinned schema**: the fields never change, only the numbers.

```json
{"position":0.6302,"travel_mm":180.4,"velocity_mms":-215.1,"t":1791475899.4529,
 "age_ms":1.5,"stale":false,"n_in":205,"calibrated":true,"ready":true,"pending":[]}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `position` | number \| null | **0.0 at the catch, 1.0 at the finish**, clamped. Null until the first full stroke has been seen (`calibrated: false`) or when no recent reading passed the filters |
| `travel_mm` | number \| null | Filtered mm along the slide from the catch, positive towards the finish. While `calibrated` is false it is the filtered raw distance instead, not referenced to the catch and not comparable between seats |
| `velocity_mms` | number \| null | Seat speed in mm/s. **Positive on the drive** (towards the finish), negative on the recovery |
| `t` | number \| null | Capture time of the newest input sample, unix seconds |
| `age_ms` | number \| null | Since the newest sample arrived |
| `stale` | boolean | Older than 1 s. Don't display the numbers as current |
| `n_in` | integer | Samples the pipeline saw this tick |
| `calibrated` | boolean | A full stroke has been seen, so catch and finish are known |
| `ready` | boolean | Every pipeline stage is implemented (true since 2026-10-08) |
| `pending` | string[] | Stages not yet implemented; empty when `ready` |

How it's computed (all on the Jetson; details in the funnel README):

- Pipeline: gate, despike, smooth (2 Hz low-pass), track catch/finish, normalise, velocity.
- **There is no calibration step.** Catch and finish are learned from the
  rower's own strokes: the first full stroke sets them, then they adapt slowly
  (over about 8 strokes) to a change of rower or reach.
- `position` lags the real seat by about **0.15 s**.
- Sitting still doesn't move the endpoints. A sensor dropout restarts the
  filters but keeps the endpoints.

`/health` reports each stage's status under `pipelines.seat_position`
(`"active"` or `"identity"`, where identity means not implemented).

---

## 6. Raw ultrasonic stream (debug only)

`rowers[seat].seat`, or `GET /rower/{seat}/seat`. Exactly what the sensor
published, plus the funnel's bookkeeping. **Don't display it as seat
position**: its zero is wherever the sensor is bolted, and it contains spikes.

```json
{"age_ms":37.7,"rate_hz":19.64,"stale":false,"n":57,"n_gap":1,"seq":58,
 "values":{"d_mm":699,"echo_us":6539},"t":1791361001.1945,"dev":"2eb968"}
```

| Field | Meaning |
| --- | --- |
| `values` | The sensor's payload, untouched. `d_mm` is distance in mm. New firmware fields appear here automatically |
| `rate_hz` | Measured arrival rate (the sensor runs at about 20 Hz) |
| `n_gap` | Samples lost in transit, counted from the sensor's `seq` |
| `seq` | Sensor's last sequence number |
| `dev` | Sensor device id |
| `n` | Samples currently buffered |

Any raw stream block (`seat`, `apriltag`, future ones) has this same envelope.

---

## 7. AprilTag (raw; oar position not derived yet)

Each camera watches one seat. The mapping lives in the funnel's config
(`FUNNEL_APRILTAG_CAMERAS=cam1:1,cam2:2,...`). An unmapped camera is ignored,
so `/rower/{seat}/apriltag` returns `404` until it's mapped.

`GET /rower/{seat}/apriltag` wraps the detector's newest record:

```jsonc
{
  "seat": 1, "cam": "cam1", "age_ms": 20.1, "rate_hz": 15.0, "stale": false,
  "n_gap": 0, "tick": 1842, "t": 1760000000.0005,
  "record": {
    "type": "apriltag", "v": 1, "cam": "cam1", "tick": 1842,
    "w": 1280, "h": 720,
    "ms": { "e2e": 24.6, ... },          // capture → publish latency
    "n": 1,                               // tags in view (0 is normal)
    "tags": [{
      "id": 0, "size_m": 0.1,
      "pos_m": [0.1117, 0.0201, 1.0661],  // tag centre in camera frame, metres
      "quat": [0.988, 0.094, 0.119, 0.024],
      "euler_deg": [11.31, 13.31, 4.07],  // [rx, ry, rz], display only
      "dist_m": 1.0721,
      "center_px": [734.29, 376.17],
      "ambiguity": 0.03                   // near 1 → rotation unreliable
    }]
  }
}
```

In `/snapshot` the same data is `rowers[seat].apriltag`, a raw stream block
whose `values` is the whole `record`.

- The detector sends a record for **every frame**, even with no tag in view
  (`n: 0`). So `503` or `stale` means the camera or detector is down, not that
  the tag is hidden.
- Camera frame: +x right, +y down, +z out of the lens. An upright tag squarely
  facing the camera has identity rotation.
- Use `quat` for maths. `euler_deg` is for display and is singular at
  `ry = ±90°`. Treat rotation as unreliable when `ambiguity` is near 1.
- `/rower/{seat}/apriltag/meta` carries the camera intrinsics (`K_rect`), tag
  sizes and conventions, resent every 2 s.

The full field list is in `vision/april-detect/README.md`, "Output contract".

---

## 8. How the Coxswain view uses the funnel

Both screens share one `FunnelStore` (`src/data/funnel/store.ts`), which polls
only while a screen holds it, so the URL set on the Developer screen applies to
the Coxswain view too. The Coxswain store (`src/data/store.ts`) listens to each
new tick:

- **Crew size** is the highest seat number reported, minimum 2. Funnel seat
  `N` is stroke and seat 1 is bow; the view lists stroke first and labels
  rowers from stroke ("Rower 1" = stroke = funnel seat `N`).
- **Dots** show `position` as served. A seat that is stale, missing, or has a
  null `position` shows NO SIGNAL (or CALIBRATING before its first full
  stroke), never its last value. An unreachable funnel shows NO CONNECTION.
- **Strokes, rate and sync are timed on the phone** until the funnel computes
  them (section 11). Each seat's catch and finish are the moments `position`
  crosses 0.25 and 0.75, interpolated on the snapshot's tick clock `t`, which
  every seat shares. Rate is from stroke's catch-to-catch period; sync is each
  seat's offset from stroke, median of the last 3 strokes. Every measured
  offset is written to the Developer log (`s<seat>` lines).
- **HANDS** shows NO DATA: there is no hand or oar-height feed yet.

## 9. Rules for app code (humans and agents)

1. **Poll `/snapshot`** about every 100 ms, one request at a time. Skip a
   response whose `tick` you've already processed. Poll `/health` about once a
   second. Don't hit per-seat endpoints in a loop.
2. **Display derived blocks** (`position`). Raw blocks (`seat`, `apriltag`)
   are for debugging and the Developer screen only.
3. **Null is not zero.** Show nulls as "—" and leave a gap in traces. Never
   substitute 0 or the last value.
4. **Respect `stale`.** A stale or `503` value must not look current. Grey it
   out or show a "stale" or "no signal" state.
5. **Don't re-filter or re-normalise** on the phone. If the numbers need
   changing, that's a change to the funnel (`embedded/funnel/funnel/compute/`).
6. **Discover seats; don't hard-code them.** Handle any number of seats, and
   any seat with only some blocks present.
7. **Parse `/health` on `503` too.** It carries the full body.
8. **Keep `src/data/funnel/types.ts` in step with `funnel/models.py`.** Add
   fields only when the funnel sends them. Check against `/openapi.json` on a
   running Jetson.

---

## 10. Testing without the boat

Run the real funnel on a laptop with simulated seat sensors, which send real
MQTT traffic through the same code:

```bash
cd embedded/funnel && ./run.sh
```

```bash
python tools/fake_sensor.py --rowers 4 --hz 20 --drop 0.02
```

Then point the app at `http://<laptop-ip>:8000`. `--drop` loses 2% of samples
so `n_gap` has something to count. Without Docker, run `python -m funnel` with
`FUNNEL_MQTT_HOST=127.0.0.1` and any MQTT broker (e.g. `pip install amqtt`,
then `amqtt`).

Useful checks against a live Jetson:

```bash
curl -s http://raytheon-rover:8000/health | python3 -m json.tool
```

```bash
curl -s http://raytheon-rover:8000/rower/1/position
```

---

## 11. Not built yet

| Item | Owner | Status |
| --- | --- | --- |
| AprilTag → oar position conversion | Jetson (funnel `compute/`) | to do |
| Oar height endpoint | Jetson | to do |
| Stroke rate, phase, catch timing | Jetson (planned in `compute/metrics.py`) | to do |
| Pose keypoints into the funnel (ZMQ) | Jetson | stub in `funnel/sources/zmq_pose.py` |
| Coxswain view on funnel data | App | done (section 8); strokes and sync timed on the phone until the funnel computes them |
| Hands feedback on the Coxswain view | App + Jetson | waiting on the oar height endpoint |
| AprilTag view on the Developer screen | App | done: Developer → AprilTag (`src/app/apriltag.tsx`) plots each tag's raw `center_px` on the image plane |
