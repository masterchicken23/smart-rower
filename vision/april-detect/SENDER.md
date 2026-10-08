# Camera sender interface

What a camera must publish for `april-detect` to consume it, and — the reason
this document exists — how to attach **frame timing** to each frame.

imageZMQ carries no timing: `send_jpg(msg, jpeg)` sends a name and a JPEG, and
nothing on the receiving side can recover when the frame was captured. Stamping
on arrival bakes in encode time and network latency, which is the same defect
the funnel's README describes for the seat sensors (`receiver.ino` restamping
on packet arrival). The fix is the same too: the sender stamps the instant of
measurement, on its own clock, and the receiver maps that clock onto its own.

The good news is that no change to imageZMQ is needed on either end.

## Transport

| | |
|---|---|
| library | imageZMQ (`imagezmq.ImageSender`), PUB/SUB mode |
| topology | the **sender binds**, the detector connects (`APRIL_SOURCE`) |
| port | `5555` by convention, one camera per address |
| send call | `sender.send_jpg(header, jpeg_bytes)` |
| `SNDHWM` | small (2). PUB drops at the high-water mark instead of blocking, so a slow or absent detector costs the camera frames, never latency. |

On the wire that is a two-frame ZMQ message, which is all the detector
actually depends on — a sender without imageZMQ can produce it with plain
pyzmq or any other ZMQ binding:

```
frame 0:  JSON  {"msg": <header>}
frame 1:  JPEG bytes
```

## The header

imageZMQ JSON-encodes whatever `msg` it is given, so pass a **dict** instead of
a name string and the header rides along unmodified:

```json
{"v": 1, "cam": "cam1", "seq": 1234, "t_ms": 5021733.104, "t_send_ms": 5021747.882}
```

| field       | type   | required    | meaning |
| ----------- | ------ | ----------- | ------- |
| `v`         | int    | yes         | header version, `1` |
| `cam`       | string | yes         | camera id; the detector reports it unless `APRIL_CAMERA_ID` overrides |
| `seq`       | uint32 | yes         | gapless per-camera frame counter, incremented for **every** frame the camera produces — including ones it then fails to send — so drops anywhere downstream are countable |
| `t_ms`      | float  | yes         | sender **monotonic** clock, milliseconds, at the **capture** instant |
| `t_send_ms` | float  | yes         | the **same** clock, milliseconds, taken as the last thing before `send_jpg` |
| `t`         | float  | no          | unix epoch seconds at capture — only if the camera's clock is NTP/chrony-synced to the Jetson |
| *other*     | any    | no          | carried through untouched, not interpreted (e.g. `exposure_us`, `gain`) |

Field names follow the funnel's MQTT envelope (`seq`, `t_ms`, `t`), so the
whole system has one vocabulary for timing.

### Getting the two instants right

**`t_ms` is the capture instant, not the send instant.** Best is the
sensor's own timestamp for the start of exposure, if the camera stack reports
one (picamera2: `request.get_metadata()["SensorTimestamp"]`, nanoseconds).
Failing that, take the clock the moment the frame is dequeued from the driver —
before encoding, before anything else. Every millisecond between the true
capture and `t_ms` is invisible latency.

**Both fields must be on the same clock.** Their difference is the camera's
own encode-and-queue time, reported per frame as `ms.enc`, and is exact only
if they share a clock. If `t_ms` comes from the sensor, find out which clock
the sensor stamps on — on Linux it is `CLOCK_MONOTONIC` or `CLOCK_BOOTTIME`;
compare it against both once at start-up and use the one it matches for
`t_send_ms` too.

**The clock must be monotonic.** Not wall time: NTP slewing or stepping the
wall clock mid-stream would show up as latency. Any monotonic clock with
sub-millisecond resolution works (`time.monotonic()` / `time.perf_counter()` on
Linux; not `time.monotonic()` on Windows, which ticks at 15.6 ms). It need not
be related to the Jetson's clock in any way.

**Why both, not just one.** The detector puts the camera's clock on its own by
tracking the minimum of `receipt_time − t_send_ms` over a sliding window
(`april_detect/clock.py`, ported from the funnel's `ClockEstimator`). Network
delay is never negative, so the minimum is the best estimate of the clock
offset. Anchoring on `t_send_ms` rather than `t_ms` keeps the encoder's
frame-to-frame variation out of that estimate. A camera reboot resets its
monotonic clock; the detector notices the clock running backwards and
re-estimates.

## What the detector reports from it

Per frame, all on the Jetson's wall clock (`ts.*`, unix seconds) and as
durations (`ms.*`):

```
 capture ──enc──▶ send ──net──▶ recv ──queue──▶ start ─decode─undistort─detect─pose─▶ pub
 ts.cap            ts.send       ts.recv         ts.start                              ts.pub
 └──────────────────────────────────── ms.e2e ────────────────────────────────────────┘
```

| clock (`clock` field) | when | what is exact |
| --- | --- | --- |
| `est`  | `t_ms`/`t_send_ms` sent | everything except the network's *minimum* one-way delay: `ms.net` is relative to the fastest frame in the last 30 s (reads ~0 on a quiet link), so `ts.cap` and `ms.e2e` read low by that minimum — sub-ms on Ethernet, a few ms on Wi-Fi |
| `ntp`  | `t` sent as well | absolute, to the accuracy of the camera's clock sync |
| `recv` | no timing sent (legacy sender) | receipt onward only; `ts.cap`, `ms.enc`, `ms.net`, `ms.e2e` are `null` |

If the absolute network delay matters, run chrony on each camera with the
Jetson as its server (the boat has no internet route, so the Jetson is the
natural time source) and send `t`.

## Reference implementation

`tools/fake_camera.py`, `send_loop()`, is the complete contract in about 20
lines. For a real camera, `vision/cam-sender` implements it on a Raspberry Pi
Zero: GPU JPEG at 1280×720, sensor capture timestamps, greyscale by default.
The essential part:

```python
import time, imagezmq, zmq

sender = imagezmq.ImageSender(connect_to="tcp://*:5555", REQ_REP=False)
sender.zmq_socket.setsockopt(zmq.SNDHWM, 2)
sender.zmq_socket.setsockopt(zmq.LINGER, 0)

seq = 0
for frame in camera:                                  # whatever yields frames
    t_ms = time.monotonic() * 1000.0                  # capture: before encoding
    jpeg = encode(frame)                              # or the camera's own MJPEG
    header = {"v": 1, "cam": "cam1", "seq": seq, "t_ms": t_ms,
              "t_send_ms": time.monotonic() * 1000.0} # last thing before send
    sender.send_jpg(header, jpeg)
    seq = (seq + 1) % 2**32
```

For `experiments/network-video-yolo/cam-sender/pi_sender.py` the change is
mostly in its send loop -- replace `sender.send_jpg(args.name, data)` with the
dict above -- plus one thing per backend: each must yield its capture instant
alongside the JPEG. The `t0` the backends already take is **not** that
instant in every case: in the picamera fast path it is taken after
`capture_continuous` has handed over a frame that is already captured and
encoded, so it would hide the whole encode. Take the sensor timestamp where
the backend exposes one (picamera2), otherwise the clock immediately after the
driver returns the frame and before encoding.

## Compatibility

Every sender in the repo works unmodified; all but `cam-sender` without timing:

| sender | wire | detector sees |
| --- | --- | --- |
| `vision/cam-sender` (pyzmq, imageZMQ format, full header) | `[{"msg": {header}}, jpeg]` | `cam`, `seq`, `clock: "est"` |
| `pi_sender.py`, `file_sender.py` (imageZMQ, `msg` = name) | `[{"msg": "name"}, jpeg]` | `cam` = name, `clock: "recv"` |
| `pi_mjpeg_pub.py` (raw pyzmq) | `[topic, jpeg]` | `cam` = topic, `clock: "recv"` |
| a sender that can only send a string `msg` | `[{"msg": "{\"cam\":...}"}, jpeg]` | parsed as the header dict |

And the change is backward compatible in the other direction: an imageZMQ
receiver that treats `msg` as a name just receives a dict instead —
`pose_stream.py` passes it through to its `src` field and keeps working.

## Frames

- **Baseline JPEG.** Any resolution; 1280×720 is the design point. A
  resolution change mid-stream is handled (new undistortion maps are built,
  tens of ms, once).
- **Greyscale JPEG is fine, and smaller.** The detector only uses luminance,
  so the chroma planes are wasted bandwidth: ~20% smaller frames in a test
  here, and more on colourful scenes. (It saves no decode time. The
  detector already decodes luminance only.)
- **Size.** At 720p and 15 fps, every 10 KB per frame is 1.2 Mbit/s per
  camera. Four cameras sharing one 2.4 GHz Wi-Fi channel is the tightest link
  in the whole system (see README, "Throughput"); `pi_sender.py --target-kb 35`
  keeps four cameras near 17 Mbit/s combined. Tags tolerate heavy compression:
  on the synthetic frames in this repo, pose error was unchanged from JPEG
  quality 85 down to 20 (p95 ~2-3 mm, ~0.6 deg at 1 m). Confirm on real
  frames with `tools/tap.py`.
- **Do not resize or crop on the camera** without recalibrating, or tell the
  detector: the calibration's resolution is scaled automatically only when the
  aspect ratio is unchanged.
