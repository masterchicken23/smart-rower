# recorder

Records everything the Jetson ingests, raw and on one clock, so a session on
the water can be played back into the funnel later with no boat, no sensors
and no cameras.

```
cams ──Wi-Fi──▶ april-camN ──raw frame tee 127.0.0.1:557N──┐
                    └──detections :556N───────────────────┤
sensors ──▶ mosquitto :1883 (the funnel's) ───────────────┼──▶ recorder ──▶ data/<UTC>/
                                                          │    HTTP :8010/recorder/*
later:  python -m recorder.play data/<UTC> ──MQTT──▶ broker ──▶ funnel
                                           └──ZMQ :558N video──▶ april-camN ──▶ funnel
        python -m recorder.mix data/<A> data/<B> --offset S   (two sessions, one crew)
```

It records three things:

- **Every MQTT message**: seat sensors, boat IMU, status, beacons. Payloads
  are kept byte for byte, so the sensor's `t_ms` and `seq` survive.
- **Every camera frame**: the JPEG exactly as the camera sent it, with its
  timing header. Frames are not re-encoded: the Orin Nano has no hardware
  encoder, and re-encoding would change what the detector sees on replay.
- **Every AprilTag record**, so a replay can skip the detectors, and a
  re-detection can be compared against the original.

The funnel's own recorder (`FUNNEL_RECORD_ENABLED`) is a different thing. It
logs samples *after* decoding, with `t_ms` already turned into an estimate,
and it has no video. This recorder captures the inputs themselves, which is
what a replay needs.

## Running it

On the Jetson, next to the funnel and april-detect:

```sh
./run.sh -d                                   # build + run, detached
curl -X POST localhost:8010/recorder/start    # start a session
curl -X POST 'localhost:8010/recorder/start?label=pause-drill'   # with a label
curl localhost:8010/recorder/status           # live sources, session progress
curl -X POST localhost:8010/recorder/stop     # finish it; prints the summary
```

| endpoint | |
| --- | --- |
| `POST /recorder/start?label=` | start a session; `409` if one is running, `507` if the disk is short |
| `POST /recorder/stop` | drain, fsync and close it; returns what was received, written and dropped |
| `GET /recorder/status` | every source's rate, age and sequence gaps, recording or not |
| `GET /recorder/sessions` | recorded sessions, newest first |
| `GET /recorder/livez` | process liveness (the healthcheck) |
| `GET /recorder/docs` | interactive docs |

**Why it cannot clash with the funnel's API.** The recorder listens on its
own port (`REC_HTTP_PORT`, 8010; the funnel uses 8000). Every route, the docs
included, is also under `/recorder`, so the two would not collide even behind
one proxy.

Check `/recorder/status` *before* starting. The sources are connected all the
time, so a camera at 0 fps or a seat with no messages shows up there first.

Each session is a new directory named after its UTC start time, such as
`20261107T143000Z` or `20261107T143000Z_pause-drill`. A name is never reused:
a second start in the same second gets `-2`. `docker compose stop` finishes a
running session cleanly (the compose file allows 30 s for it). The container
never restarts on its own, so a reboot cannot start quietly filling the disk.

### What has to be configured

| | |
| --- | --- |
| april-detect | **the raw frame tee**. On by default in `vision/april-detect/docker-compose.yml`: camera N on `127.0.0.1:557N`. Each detector passes on every frame it receives, unchanged, with its own arrival stamp. The recorder does not subscribe to the cameras itself, because each camera would then send a second copy of its stream over Wi-Fi, the system's tightest link. |
| the funnel | nothing. The recorder is a second subscriber on its broker (`127.0.0.1:1883`, published by the funnel's compose). |
| the cameras | nothing, unless they should also record locally: then `CAM_RECORD=true` and `CAM_REC_CONTROL=tcp://<jetson-ip>:5580` on each (see "Recording on the cameras too"). `REC_CAM_SYNC=false` turns them all off from here. |
| `REC_DATA` | where sessions go. **Use the NVMe, formatted ext4**: four cameras at the default 35 KB/frame come to ~2 MB/s, about 7.6 GB an hour. The recorder checks the filesystem at startup and warns in its log, in `/recorder/status` and in each session's `meta.json` if it is FAT32 or exFAT (see "When a recording is damaged"). |

`REC_CAMERAS` can point a camera at its own address (`cam1=tcp://cam1.local:5555`)
when april-detect is not running, at the cost of that second Wi-Fi copy.
`.env.example` lists every setting; `recorder/config.py` explains each one.

## What's in a session

```
data/20261107T143000Z/
  meta.json         clock anchors at start/end, config, sources, and at stop a
                    per-device summary: received, written, sequence gaps, drops
  mqtt.jsonl        {"tm","tw","topic","qos","retain","p"}   one per message
  apriltag.jsonl    {"tm","tw","topic","p"}                  one per record
  cam1.mjpeg        the JPEGs, concatenated, byte for byte
  cam1.idx.jsonl    {"tm","tw","off","len","crc","pre","up"} one per frame
```

- `p` is the payload: a string, or `{"b64": ...}` if it is not UTF-8.
- `crc` is the frame's CRC-32.
- `pre` is the frame's header part(s) as the camera sent them.
- `up` is the tee's arrival stamp.

Quick look at a camera: `ffplay -f mjpeg data/<s>/cam1.mjpeg`. ffplay does not
know the recorded timing, so it plays at its own pace. To watch one in real
time, or send it to someone, export it as MP4 (see "Exporting video").

`python -m recorder.report data/<session>` summarises a session:
per-topic and per-camera rates, sequence gaps (losses between the sender and
the Jetson), the recorder's own drops, wall-clock steps, and the clock mapping
for every device. Add `--json` for the same as JSON, and `--verify` to read
every frame and check its CRC (it reads the whole video, so it is not the
default).

### When a recording is damaged

One video file per camera per session is safe because nothing in it depends on
anything after it. There is no index at the end to lose, as MP4 has, and each
frame is a JPEG of its own. Damage costs what it touches, not the session:

| what happens | what it costs |
| --- | --- |
| power cut, crash, `kill -9` | the last few seconds (up to `REC_FSYNC_S`, 5 s). `meta.json` says `complete: false`; the session still reports and plays. |
| a torn or garbled index line | that frame. Lines are independent; reading resumes at the next one. |
| an index entry pointing past the end of the video | that frame (counted as `missing` in the report). |
| a write that fails halfway (SD card error, disk full) | that frame. Each frame's offset is read from the file, not counted, so the bytes that did land are never pointed at, and later frames are unaffected. |
| frame bytes damaged on the disk | that frame. Its CRC fails: the player skips it and counts it as `corrupt`, and `report --verify` counts it as `crc bad`. |

The filesystem matters more than the format:

- **FAT32** caps a file at 4 GB, so a camera stops recording after about 2 h
  at 35 KB/frame, or about 30 min at webcam frame sizes.
- **FAT32 and exFAT** have no journal, so a power cut can damage the
  filesystem itself rather than only lose the tail.

Format the data drive ext4. The recorder warns at startup and at each session
start if it is not.

## Recording on the cameras too

The recorder only has the frames that crossed Wi-Fi. A camera run with
`CAM_RECORD=true` ([vision/cam-sender](../../vision/cam-sender), "Local
recording") also keeps every frame on its own SD card. This recorder starts
and stops those local recordings with its own sessions, under the same name.

- **How cameras hear it.** The recorder publishes its state on
  `REC_CAM_CONTROL_BIND` (`tcp://*:5580`) on every change and every second:
  the session's name, or null. The cameras connect to it
  (`CAM_REC_CONTROL=tcp://<jetson-ip>:5580`).
- **State, not commands.** A camera that was unreachable at start or stop, or
  that has restarted since, is put right by the next message. A camera that
  hears nothing keeps recording, so an outage never stops one. It stops when
  it next hears that no session is running.
- **Turning it off.** `REC_CAM_SYNC=false` turns the cameras' local recording
  off from here. The recorder still publishes, but always "no session", so
  every camera stays or goes idle. An empty `REC_CAM_CONTROL_BIND` opens no
  socket at all.
- **Confirmation.** Each frame a camera keeps carries `"rec": "<session>"` in
  its header. `/recorder/status` shows it per camera
  (`sources.video.camN.rec`), and the stats line shows `camN 15.0fps+rec`.
  At stop, the summary counts the confirmed frames per camera (`cam_local`).
  You know which cameras have a local copy before you go and fetch it.

To use a camera's copy, rsync its session into a subdirectory of this one
(`data/<session>/cam1/`), then read it with `recorder.report`,
`recorder.play` or `recorder.export`. Keep it there rather than merging its files, because its
`tm` is on the camera's clock. The frames match this session's by `seq`.
To replay the session with the camera's copy in place of the Jetson's, give
`--cam-copy data/<session>` to `recorder.play` or `recorder.mix` (see
"Playing a session back").

## Time synchronisation

Aligning the streams is the point of recording them together. These are the
precautions, in order:

1. **One clock, stamped on arrival.** Each source has its own receive thread:
   paho's network thread, one ZMQ thread per camera, one for the detectors.
   Each thread only receives, stamps and queues; a separate writer thread
   does the disk I/O. A slow SD card can delay a write but never a stamp.
   - `tm` is `perf_counter_ns`. On Linux that is the kernel's
     CLOCK_MONOTONIC, shared by every container: the same clock as the
     funnel's `t_recv` and the detectors' stamps. It is immune to the wall
     clock being stepped.
   - `tw` (wall time) is kept beside it, and steps in it are reported.
2. **The tee's stamp for video.** A teed frame carries april-detect's own
   arrival stamp, taken in the thread that does nothing but receive. The
   loopback hop and the recorder's scheduling under CPU load therefore drop
   out. Both stamps are kept; the report shows the hop.
3. **The senders' own clocks, untouched.** Arrival times carry Wi-Fi, broker
   and encoder jitter: tens of ms, against a 50 ms seat period. The recording
   keeps what removes it: the sensor's `t_ms` (millis at the echo), and the
   camera's `t_ms` (sensor timestamp) and `t_send_ms`.
4. **Mapping those clocks onto the Jetson's** (`recorder/sync.py`) uses the
   rule the funnel and april-detect apply live: the minimum of
   `arrival − sender clock` is the clock offset, because delay is never
   negative. Offline it does better in three ways:
   - it takes the minimum in every 10 s window and joins the minima with
     straight lines, so crystal drift is followed (and reported in ppm);
   - it maps each sample from the windows on both sides;
   - it splits at reboots.

   The report prints, per device, the drift, the segments, and the delay
   above the fastest message.
5. **Every loss is counted**: sequence gaps per device, queue drops, write
   errors. A hole in the recording is never mistaken for the rower holding
   still.

**What is left.** Each device's *minimum* one-way delay cannot be seen from
one side, so devices align to within the *difference* of their minimum
delays. That is a few ms on Wi-Fi, against sample periods of 50 ms (seat) and
67 ms (camera). To remove even that, run chrony on the senders with the
Jetson as server and have them send `t`.

## Playing a session back

```sh
python -m recorder.play data/20261107T143000Z
```

The player republishes the session over the transports it arrived on:

- MQTT to a broker (default `127.0.0.1:1883`);
- each camera's frames on a ZMQ PUB in the camera's own format (cam1 on
  `tcp://*:5581`, cam2 on 5582, ...).

The funnel and april-detect then run **unchanged**. Point the detectors at the
player (`CAMn_SOURCE=tcp://127.0.0.1:558N`), or skip them and replay the
recorded detections straight into the funnel:

```sh
python -m recorder.play data/<s> --apriltag --no-video     # no detectors needed
python -m recorder.play data/<s> --loop --start 30 --end 90
```

On the Jetson:
`docker compose run --rm recorder python -m recorder.play /data/<session>`.
It uses host networking, so `127.0.0.1:1883` is the funnel's broker. **Stop
the real sensors first**, or both will publish to the same topics.

**How replay keeps the alignment.**

- One thread sends everything in recorded arrival order, each message at an
  absolute deadline, so lateness never accumulates. It prepares each message
  before its deadline and reports lateness (p50/p95/max) at the end.
- Messages go out on the recorded *arrival* schedule, jitter included. The
  funnel's and detectors' clock estimators then reach the same capture times
  they reached live.
- Every sender clock in the payloads (`t_ms`, `t_send_ms`) is moved by the
  same amount as the schedule. Each device keeps its recorded offset to
  arrival, and clocks only move forward, across `--loop` passes and across
  repeated runs. The funnel never sees a sensor clock run backwards between
  runs.
- Wall-clock fields (`t`, `ts.*`) are moved onto the present, so every stream
  agrees on "now".
- `seq` and `tick` carry on across loop passes.
- JPEG bytes are never changed.

`--verbatim` sends every byte exactly as recorded, once. Restart the funnel
between verbatim runs.

**A camera's own copy instead of the Jetson's.** The Jetson's copy holds only
the frames that crossed Wi-Fi: 3 to 5 % fewer than the camera sent. The camera's
own copy (see "Recording on the cameras too") holds them all:

```sh
python -m recorder.play data/<s> --cam-copy data/<s>        # copies in data/<s>/camN/
python -m recorder.play data/<s> --cam-copy ../pi           # or a directory of them
```

`--cam-copy` takes a camera recording, or a directory holding them, and can
be repeated. A copy is used for a session when its `meta.json` has the
session's name. It is matched to the Jetson's camera it shares the most
frames with, by `seq` and `t_send_ms`. Its clock is the camera's own, so it is
placed on the Jetson's by the least delay over the shared frames (the rule in
"Time synchronisation"). Each frame then goes out when the camera sent it,
plus that least delay. The frames Wi-Fi lost are filled in, and the Wi-Fi
jitter is gone. A camera that rebooted mid-session is placed one stretch of
its clock at a time. A stretch that shares no frame with the Jetson's copy
cannot be placed, and is left out with a warning.

## Playing two sessions as one crew

`recorder.mix` plays several sessions at once, each on seats and cameras of
its own. Two single-rower sessions recorded minutes apart become a two-seat
boat for the funnel and april-detect:

```sh
python -m recorder.mix data/A data/B --offset auto --dry-run   # look first
python -m recorder.mix data/A data/B --offset -10.5 --cam-copy ../pi
python -m recorder.mix data/A data/B --offset -10.5 --apriltag --no-video --loop
```

- **Seats and cameras.** The first session plays as recorded. Each later one
  moves past the highest seat and camera number in use: two sessions of seat
  1 and `cam1` play as `rower/1`, `cam1` and `rower/2`, `cam2` (video on 5582).
  A moved seat's status payload names its new seat. A moved camera's detector
  records carry its new name. Other topics (`boat/*`) come from the first
  session only: there is one boat. Detector records of cameras a session has
  no video or detections for (idle detectors' `apriltag_meta`) are left out.
  To swap the seats, swap the sessions.
- **The offset.** `--offset S` starts the second session S seconds after the
  first one's start, or before it if negative. Give one per session after the
  first. A start is `meta.json`'s `start`, so an offset does not move with
  `--no-video` or `--start`. To line up a catch at 10.0 s in A with one at
  13.2 s in B, B starts 3.2 s earlier: `--offset -3.2`.
- **`--offset auto`** finds the offset that lines the strokes up. It
  correlates the two seat-distance traces, on their capture times, within
  `--search` seconds (20 by default). Strokes repeat, so there is a peak at
  every stroke. It takes the best and prints the next ones, which can be
  given by hand instead.
- **`--start`/`--end`** count on the shared timeline. With `--loop` the crew
  loops as a unit: the shorter session goes quiet until the longer ends.
- **Timing** is `recorder.play`'s engine, one track per session, so
  everything under "How replay keeps the alignment" holds within each
  session. Each moved seat is its own stream to the funnel, with its own
  clock estimator.

At start it prints the settings the consumers need, for example
`FUNNEL_APRILTAG_CAMERAS=cam1:1,cam2:2`, and `CAM2_SOURCE=tcp://127.0.0.1:5582`
for april-detect (whose compose sets `APRIL_CAMERA_ID` per detector). With
`--apriltag` it prints `FUNNEL_APRILTAG_CONNECT=tcp://127.0.0.1:5561`.
`--dry-run` prints all of it, and the offsets, and sends nothing.

To watch the crew, open the funnel's
`tools/crew_viewer.html?api=http://127.0.0.1:8000` (see the funnel's README).
It overlays every rower's seat position over time, which is the quickest way
to check that an offset lines the strokes up. It also plots each rower's oar
height against seat position.

## Checking that it all lines up

Record the replay and compare it with the original:

```sh
curl -X POST 'localhost:8010/recorder/start?label=replay'
python -m recorder.play data/<original>
curl -X POST localhost:8010/recorder/stop
python -m recorder.report data/<replay> --compare data/<original>
```

`--compare` matches every device's messages by sequence number and measures
how far each device moved *relative to the others*. A replay shifts everything
by one constant, so whatever remains is alignment error. It should be well
under 2 ms at p95.

On the boat, the physical check: wave a board through a seat sensor's beam in
view of that seat's camera. The distance dip and the frame showing the board
should agree to within a frame on the report's capture-time axis.

## Exporting video

```sh
python -m recorder.export data/20261107T143000Z              # every camera
python -m recorder.export data/<s> data/<s>/cam1 --cam cam1  # and a camera's own copy
python -m recorder.export data/<s> --crf 23                  # smaller files
```

Each camera becomes an H.264 MP4 in the session's directory, named after the
directory: `<session>.mp4`, or `<session>_cam2.mp4` when the session holds
more than one camera. The MJPEG and its index are left as they are.

Every frame is shown at the moment it was captured, so the video plays in
real time:

- A camera does not run at a steady 15 fps. It captures at ~30 fps and sends
  about every other frame, so frames are mostly 68 ms apart, with some 32 ms
  steps.
- Frames lost on Wi-Fi leave holes in the Jetson's copy. Played at a fixed
  rate, a copy that lost 3 % of its frames runs 3 % fast, nearly 2 s a
  minute. Exported, a lost frame shows as the one before it held for the gap.
- Arrival times would be no better: Wi-Fi delivers frames in bursts, 10 to
  300 ms apart.

The capture time is the header's `t_ms`, so the spacing is the camera's own.
It is placed on the recording's clock by the least delay seen (see "Time
synchronisation"), and split where the camera rebooted, so a reboot
mid-session stays in order. Export does not use the report's windowed fit:
that follows clock drift, but a whole window of slow Wi-Fi bends it, and a
bent fit would change the frame rate. A camera's own copy, whose clock
restarts with the camera, is joined across the reboot by its wall clock.
Frames that fail their CRC are left out
and counted. The video starts at its first frame; the exporter prints how
long after the session's start that frame was captured, and keeps it in the
MP4's comment.

It needs ffmpeg 5.1 or newer with libx264: on `PATH`, given with `--ffmpeg`,
or from `pip install imageio-ffmpeg`, which bundles one. The recorder's image
has no ffmpeg, so export on a laptop.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest            # no broker, camera or Docker needed
.venv/bin/ruff check .
```

The suite covers:

- the format round trip, including empty, binary and retained payloads, and
  a torn tail;
- session naming, the lifecycle, and auto-stop on a full disk;
- the API: 409/507, and that every route sits under `/recorder`;
- clock mapping against a synthetic sender with drift, jitter, stalls and a
  reboot;
- the player's schedule and clock rewriting;
- byte-exact replay over real loopback sockets;
- a camera's own copy on the Jetson's clock: the three places a copy can be
  found, frames lost on Wi-Fi filled in, none placed after its Jetson
  arrival, a camera reboot, a copy that shares no frame, and `--cam-copy`
  over a loopback socket;
- the mix: seats and cameras moved, records of idle detectors left out, one
  boat; the offset on the schedule and the wall clock (positive, negative,
  zero), one loop period for all, each session's clocks across loops,
  `--start`/`--end` on the shared timeline; `--offset auto` recovering a
  known lag from irregular strokes with spikes and unrelated sensor clocks; a
  dry run sending nothing; and two rowers coming out on their own topics and
  camera ports at the offset;
- camera sync over a loopback socket: idle, session, idle; sync off; the
  frames' `rec` confirmation;
- export's frame times across lost frames, Wi-Fi bursts, a spell of slow
  Wi-Fi, a camera reboot and a camera's restarted clock; and, where an ffmpeg is found (`PATH` or
  imageio-ffmpeg), the MP4's decoded frame times.

End to end on one machine without Docker (Windows works), with
`pip install amqtt` for a broker:

```sh
amqtt                                                               # broker :1883
python ../funnel/tools/fake_sensor.py --rowers 2 --drop 0.02        # sensors
python ../../vision/april-detect/tools/fake_camera.py --bind tcp://127.0.0.1:5555 --name cam1
APRIL_SOURCE=tcp://127.0.0.1:5555 APRIL_OUT_ADDRESS=tcp://127.0.0.1:5561 \
  APRIL_TEE_ADDRESS=tcp://127.0.0.1:5571 APRIL_CAMERA_ID=cam1 APRIL_HEARTBEAT_PATH= \
  APRIL_CAMERA_MATRIX="900 900 639.5 359.5" APRIL_CALIB_SIZE=1280x720 python -m april_detect
REC_DIR=./data REC_CAMERAS=cam1=tcp://127.0.0.1:5571 \
  REC_APRILTAG_CONNECT=tcp://127.0.0.1:5561 python -m recorder
```

Then start and stop a session as above. To replay it:

1. Stop the sensors and the camera.
2. Restart april-detect with `APRIL_SOURCE=tcp://127.0.0.1:5581`.
3. Run `python -m funnel` with `FUNNEL_MQTT_HOST=127.0.0.1
   FUNNEL_APRILTAG_ENABLED=true FUNNEL_APRILTAG_CAMERAS=cam1:1
   FUNNEL_APRILTAG_CONNECT=tcp://127.0.0.1:5561`.
4. Run `python -m recorder.play data/<session>`.

## Conventions

These follow the funnel and april-detect:

- `REC_*` environment variables for the service, and argparse for the tools;
- `log()` to stderr with `[subsystem]` prefixes, a stats line every 5 s, and
  deferred heavy imports;
- drop-oldest queues, with every drop counted;
- `python:3.11-slim`, non-root, dependencies before source, and a `run.sh`
  that owns `./data`.

Like april-detect, it uses host networking: the tees are loopback-only, and
the detectors already live on the host's network.
