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

## Development

The tests and the `test` backend need no Pi. With the april-detect venv
(the tests decode with the detector's own `april_detect/wire.py`):

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
