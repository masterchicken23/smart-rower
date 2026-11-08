# cam-sender

Raspberry Pi Zero camera → JPEG → ZMQ, in exactly the format
[`april-detect`](../april-detect) consumes ([SENDER.md](../april-detect/SENDER.md)),
timing header included.

```
 camera ─▶ ISP ─▶ VideoCore MJPEG ─▶ cam-sender ─▶ ZMQ PUB :5555 ──Wi-Fi──▶ april-camN (Jetson)
```

- **1280×720 at 15 fps** by default, both configurable.
- **Greyscale** by default (`--color` for colour). The detector uses only
  luminance, and greyscale is smaller on the shared Wi-Fi.
- **JPEG on the GPU.** An original Pi Zero / Zero W (ARMv6, one core, no NEON)
  cannot software-encode 720p at 15 fps; the VideoCore encoder can, so the
  CPU never touches pixels.
- **Full SENDER.md header**: `seq` counts every frame the camera produced, and
  `t_ms` is the **sensor's** capture timestamp, on the kernel clock it is
  detected to be on. `t_send_ms` is stamped on the same clock just before the
  send. The detector then reports `clock: "est"` with real `ms.enc`, `ms.net`
  and `ms.e2e` for this camera.
- Plain pyzmq, no imageZMQ. The bytes are identical to imageZMQ's
  `send_jpg(header, jpeg)`, so imageZMQ receivers (`pose_stream.py`) work too.
- **Optional local recording** (`--record`): every frame is also written to
  the Pi's own disk. It starts and stops with the Jetson recorder's sessions,
  so a Wi-Fi outage does not cost the camera's video
  ([below](#local-recording)).

## Running it (native, recommended)

On Raspberry Pi OS Bookworm (32-bit is the only kind there is for a Zero W),
with the camera enabled and detected (`rpicam-hello --list-cameras`):

```sh
sudo apt install -y python3-picamera2 python3-zmq python3-simplejpeg
cd vision/cam-sender
python3 -m cam_sender                          # cam name = hostname, binds :5555
```

Install nothing with pip on the Pi: picamera2 and libcamera exist only as apt
packages, and apt's builds are the ones made for ARMv6. If you want a venv,
create it with `--system-site-packages`.

Point the detector at it: `CAMn_SOURCE=tcp://<pi-hostname>.local:5555` in
`april-detect/.env`.

To start on boot, use [`cam-sender.service`](cam-sender.service). The
instructions are at the top of the file.

### Options

Each flag defaults to a `CAM_*` environment variable (see
[.env.example](.env.example)). A flag given on the command line wins.
`python3 -m cam_sender --help` lists them all.

| flag | env | default | |
|---|---|---|---|
| `--name` | `CAM_NAME` | hostname | `cam` in every header |
| `--bind` | `CAM_BIND` | `tcp://*:5555` | the sender binds, the detector connects |
| `--backend` | `CAM_BACKEND` | `hw` | `hw` GPU MJPEG · `sw` CPU simplejpeg · `uvc` USB webcam passthrough · `test` synthetic, no camera |
| `--device` | `CAM_DEVICE` | `/dev/video0` | `uvc`: the webcam's V4L2 device |
| `--width` `--height` | `CAM_WIDTH` `CAM_HEIGHT` | 1280 720 | keep the calibration's aspect ratio |
| `--fps` | `CAM_FPS` | 15 | sets the sensor's frame duration, so the camera paces itself (`uvc`: frames are dropped down to it) |
| `--color` | `CAM_COLOR` | off | greyscale otherwise |
| `--target-kb` | `CAM_TARGET_KB` | 35 | KB/frame. `hw`: sets the encoder bitrate. `sw`/`test`: quality is servoed toward it |
| `--quality` | `CAM_QUALITY` | 60 | `sw`/`test`: JPEG quality (fixed if `--target-kb 0`) |
| `--hflip` `--vflip` | `CAM_HFLIP` `CAM_VFLIP` | off | |
| `--sndhwm` | `CAM_SNDHWM` | 2 | PUB drops beyond this, never queues |
| `--stats-interval` | `CAM_STATS_INTERVAL_S` | 5 | seconds; 0 disables |

The `--record` and `--rec-*` flags are in [Local recording](#local-recording).

### Stats line

```
[stats]  15.0 fps    34.8 KB/frame   4.27 Mbit/s  skipped 0  failed 0  t_ms: sensor/monotonic
```

- `skipped`: frames the camera produced that were never sent (the sender
  could not keep up). They still use up a `seq`, so the detector counts them too.
- `t_ms` shows where capture time comes from.
  - `sensor/<clock>` is correct.
  - `dequeue` means the sensor timestamp matched no kernel clock (logged once at
    start-up). Timing then still works, but `ms.enc` and `ms.e2e` read low by the
    capture-to-dequeue time.

### Bandwidth

At 15 fps, each 10 KB per frame costs 1.2 Mbit/s. The 35 KB default (4.3 Mbit/s)
keeps four cameras at about 17 Mbit/s on one 2.4 GHz channel (SENDER.md,
"Size"). AprilTags tolerate heavy compression. Check real frames with
`april-detect/tools/tap.py` before raising it.

### Backends

- **`hw`** (default). picamera2 plus the VideoCore MJPEG encoder, rate-controlled
  by bitrate. Greyscale costs nothing: the ISP is set to `Saturation=0`, so the
  chroma planes are flat and compress to almost nothing.
- **`sw`**. CPU JPEG straight from the YUV planes (greyscale is the Y plane
  only), with per-frame exposure and gain in the header. It reads exact sensor
  timestamps but costs CPU. A Zero W cannot hold 720p15 this way; a Zero 2 W
  can.
- **`uvc`**. For a USB webcam (`--device`, default `/dev/video0`). It reads
  V4L2 directly, as `v4l2-ctl --stream-mmap` does, not through libcamera:
  libcamera's UVC handler fails to start some webcams (`Failed to start
  streaming: Protocol error` on the Innomaker U20CAM) that stream fine through
  plain V4L2, and it would hand `hw`/`sw` MJPEG, which they refuse. The
  camera's own JPEGs are sent untouched, so it costs almost no CPU. Most
  webcams offer only 30 fps for MJPEG, so frames are dropped down to `--fps`
  (they show as `seq` gaps). `--target-kb` and `--quality` do not apply, and
  greyscale works only if the camera has a saturation control.
- **`test`**. Synthetic frames, no camera. Runs anywhere and checks the whole
  chain (below).

## Local recording

With `--record` (`CAM_RECORD=true`), the sender also writes every frame to
its own disk. A Wi-Fi outage then costs the Jetson's copy of this camera, not
the camera's own. [`embedded/recorder`](../../embedded/recorder) starts and
stops these recordings. When a session starts there, each recording camera
starts one under the same name, and it stops with that session.

```sh
python3 -m cam_sender --record --rec-control tcp://10.74.228.133:5580
```

`--rec-control` is the recorder's camera control address (its
`REC_CAM_CONTROL_BIND`, port 5580). The bare IP also works, with port 5580
assumed, and a malformed address stops the sender at startup. Give the
Jetson's IP rather than a `.local` name. With Wi-Fi down, each mDNS lookup can take seconds. The lookup
cannot hold up frames, because the control socket has its own thread and ZMQ
context, but the camera would learn of a start or stop that much later.

The recorder repeats its state every second, and the camera follows it:

| the camera hears | it does |
|---|---|
| a session, while idle | starts it in `rec/<session>/`, or resumes that directory if cam-sender restarted mid-session |
| the same session again | nothing |
| a different session | stops the old one (`superseded`) and starts the new one |
| no session (a stop, or `REC_CAM_SYNC=false` on the recorder) | stops (`recorder stopped`) |
| **nothing: the network is down** | **keeps recording**, and says so in the log after 10 s |

If a session is stopped while the camera is out of reach, the camera stops
when it next hears the recorder. The extra tail is harmless.

Three guards end a session on the camera's own account. A session they end
is not restarted, not even by a restarted cam-sender:

- free space falls under `--rec-min-free-mb`;
- the session runs longer than `--rec-max-s` (3 h by default), for a stop
  that never arrives;
- 50 writes in a row fail, meaning the card is failing.

While a frame is being kept, its header carries `"rec": "<session>"`. The
detector ignores the key. The recorder shows it per camera in
`/recorder/status` (`sources.video.camN.rec`) and counts the confirmed frames
per camera in the session summary (`cam_local`).

| flag | env | default | |
|---|---|---|---|
| `--record` | `CAM_RECORD` | off | |
| `--record-dir` | `CAM_RECORD_DIR` | `rec` | relative to the working directory |
| `--rec-control` | `CAM_REC_CONTROL` | | required with `--record` |
| `--rec-queue-mb` | `CAM_REC_QUEUE_MB` | 32 | frames waiting for the disk (~1 min). Past it the oldest is dropped from the recording, never from the stream, and counted |
| `--rec-min-free-mb` | `CAM_REC_MIN_FREE_MB` | 1000 | no start, and a running session stops, below this |
| `--rec-max-s` | `CAM_REC_MAX_S` | 10800 | 0: no limit |
| `--rec-fsync-s` | `CAM_REC_FSYNC_S` | 5 | the most a power cut can cost; 0 disables |

The stats line gains `rec <session> <n> written  q <MB>  drop <n>`, or
`rec idle`. It adds `(control silent N s)` while the recorder is not being
heard.

### What it writes

It uses the recorder's own session format (embedded/recorder README, "What's
in a session"):

```
rec/20261107T143000Z_drill/
  meta.json         segments (one per start or resume), stop reasons, counts
  cam1.mjpeg        the JPEGs as sent, concatenated
  cam1.idx.jsonl    {"tm","tw","off","len","crc","pre"} per frame
```

`pre` is the header exactly as sent, so each frame matches its copy on the
Jetson by `seq`. `tm` is `t_send_ms` on this Pi's clock. At the default
35 KB per frame and 15 fps, a session takes about 1.9 GB an hour.

### Getting it back

Copy the session into the Jetson's session directory as a subdirectory, then
read it with the recorder's tools:

```sh
rsync -a pi@cam1.local:smart-rower/vision/cam-sender/rec/20261107T143000Z_drill/ \
    data/20261107T143000Z_drill/cam1/
python -m recorder.report data/20261107T143000Z_drill/cam1      # or recorder.play
```

Keep it in its own directory. Its `tm` is on the camera's clock, so its files
must not be merged into the Jetson's.

### What cannot stall the frame loop

The local copy exists for the frames the network could not take. So nothing
on the frame loop waits on the network or the disk:

- **Sending.** It is non-blocking, on a PUB with a high-water mark of 2. A
  stalled link costs frames on the wire (`failed`) and never waits.
- **Recording.** Each frame goes into a bounded queue, and a writer thread
  does the disk work. A slow SD card costs recorded frames (`drop`), never
  sent ones.
- **Control.** The control socket has its own thread and ZMQ context.
- **Logging.** Lines are queued and written by a separate thread, because
  stderr can block: a dropped SSH session's pty, or journald behind a busy
  card.
- **Heartbeat.** The heartbeat file is touched by its own thread.
- **SIGHUP is ignored.** A dropped SSH session therefore does not kill the
  sender, and its recording, when sshd gives up. For real sessions, still run
  it as a service (`cam-sender.service`).

A camera that cannot keep up shows `skipped` in the stats line.

## Docker (optional)

The official `debian`/`python` armhf images are built for ARMv7 and crash with
"Illegal instruction" on a Zero W. No maintained ARMv6 image ships picamera2,
so the base image is bootstrapped from the official Raspbian mirror, once:

```sh
sudo apt install -y debootstrap docker.io
./docker/build-base.sh          # smart-rower/raspbian-bookworm-armv6:base
cp .env.example .env            # optional
./run.sh -d                     # docker compose up --build -d
docker ps                       # rower-cam-sender ... (healthy)
```

- `build-base.sh` runs on any 32-bit Raspberry Pi OS. On a Pi 3/4 it is much
  faster than on the Zero, and the result runs on the Zero unchanged:
  `docker save smart-rower/raspbian-bookworm-armv6:base | ssh <zero> docker load`.
- Install Docker from Raspberry Pi OS's own `docker.io` package. Docker's apt
  repository is ending 32-bit armhf support.
- The container is `privileged` because libcamera needs several device nodes
  with dynamic major numbers. Mounting `/run/udev` lets it enumerate them.
  [docker-compose.yml](docker-compose.yml) describes how to narrow this.
- Healthcheck: the sender touches `/tmp/cam-sender.alive` while frames flow,
  so a camera that stops delivering marks the container unhealthy.
- Local recording: the container writes to `/rec`, which is the host's
  `CAM_REC_DATA` (default `./rec`). The container's user, uid 10001, must
  own it: `sudo chown 10001:10001 rec`. `run.sh` creates the directory and
  reminds you.

## Development

The tests and the `test` backend need no Pi. Run them with the april-detect
venv. They decode frames with the detector's own `april_detect/wire.py` and
read local recordings with the recorder's own `recorder/format.py`:

```sh
cd vision/cam-sender
../april-detect/.venv/Scripts/python -m pytest          # bin/python on Linux
```

End to end against the real detector, on one machine:

```sh
python -m cam_sender --backend test --name cam1 --bind tcp://127.0.0.1:5590
# in april-detect/:
APRIL_SOURCE=tcp://127.0.0.1:5590 APRIL_OUT_ADDRESS=tcp://127.0.0.1:5591 python -m april_detect
python tools/tap.py tcp://127.0.0.1:5591
```

`tap.py` should show `cam1` at ~15/s, `lost 0`, and `[est]` timing.
