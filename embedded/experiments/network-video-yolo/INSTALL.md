# Install

Two independent installs. Nothing is shared between the devices except the
imageZMQ wire format.

---

## Sender — Raspberry Pi Zero

### First, ask the Pi what it can do

```bash
python3 pi_sender.py --list-backends
```

That prints every backend, whether it is usable, and the apt command to fix the
ones that are not. Confirm the camera itself is seen, too:

```bash
rpicam-hello --list-cameras
```

### Current Raspberry Pi OS (Bookworm) — both Zero W and Zero 2 W

Bookworm removed the legacy camera stack, so **there is no `python3-picamera`
package to install**. Use picamera2, or rpicam-apps if picamera2 is missing or
too heavy for a 512MB Zero. Bookworm's pip also refuses to install into the
system Python (PEP 668), hence the venv.

```bash
sudo apt update && sudo apt install -y python3-picamera2 rpicam-apps python3-numpy
python3 -m venv --system-site-packages ~/venv-sender
source ~/venv-sender/bin/activate
pip install -r requirements-sender.txt
pip install simplejpeg          # optional, ~2x faster software JPEG
```

Install numpy from **apt**, not pip, as above. The pip and piwheels numpy wheels
for ARM link against the system OpenBLAS, which Raspberry Pi OS does not ship, so
a pip-installed numpy fails to import with a long C-extension traceback ending in
`libopenblas.so.0: cannot open shared object file`. If you already have a pip
numpy and would rather keep it:

```bash
sudo apt install -y libopenblas0
```

```bash
python3 pi_sender.py --bind 'tcp://*:5555' --ratio 40 --fps 15
```

`--backend auto` tries picamera2, then rpicam, then v4l2, then legacy picamera,
and verifies each actually yields a frame before committing to it.

The rpicam backend needs no Python camera library at all — it pipes MJPEG from
`rpicam-vid`. If picamera2 will not install, this alone is enough:

```bash
sudo apt install -y rpicam-apps && python3 pi_sender.py --backend rpicam --bind 'tcp://*:5555'
```

Note that rpicam-apps encodes MJPEG with libjpeg on the CPU, and ARMv7 has no
NEON, so on an original Zero W it is slower than picamera2's `--hw-jpeg`. If you
cannot hold the frame rate, lower `--width/--height` before anything else.

### Legacy Bullseye images (Zero / Zero W only)

Only if you are still on Bullseye with **Interface Options > Legacy Camera**
enabled. This is the fastest path on a Zero W: JPEG is encoded on the GPU, so the
ARMv6 core never touches a bitmap and OpenCV is not needed.

```bash
sudo apt update && sudo apt install -y python3-picamera python3-pip
pip3 install -r requirements-sender.txt
python3 pi_sender.py --backend picamera --bind 'tcp://*:5555' --ratio 40 --fps 15
```

Add `--hw-jpeg` on the Zero 2 W to use the V4L2 hardware MJPEG encoder instead of
encoding in software (bitrate-controlled, so `--quality` stops applying).

---

## Receiver — Jetson Orin Nano 8GB (JetPack 6.1, Python 3.10)

**Order matters.** `pip install ultralytics` pulls generic PyPI torch wheels that
have no Tegra CUDA support; steps 3–4 overwrite them with NVIDIA's aarch64 builds.

```bash
sudo apt update && sudo apt install -y python3-pip && pip install -U pip
```

```bash
pip install -r requirements-receiver.txt
```

```bash
pip install https://github.com/ultralytics/assets/releases/download/v0.0.0/torch-2.10.0-cp310-cp310-linux_aarch64.whl
pip install https://github.com/ultralytics/assets/releases/download/v0.0.0/torchvision-0.25.0-cp310-cp310-linux_aarch64.whl
```

That torch wheel needs cuDSS:

```bash
wget https://developer.download.nvidia.com/compute/cudss/0.7.1/local_installers/cudss-local-tegra-repo-ubuntu2204-0.7.1_0.7.1-1_arm64.deb
sudo dpkg -i cudss-local-tegra-repo-ubuntu2204-0.7.1_0.7.1-1_arm64.deb
sudo cp /var/cudss-local-tegra-repo-ubuntu2204-0.7.1/cudss-*-keyring.gpg /usr/share/keyrings/
sudo apt-get update && sudo apt-get install -y cudss && sudo reboot
```

Optional, and only needed for `--export-engine` (PyPI has no aarch64 build):

```bash
pip install https://github.com/ultralytics/assets/releases/download/v0.0.0/onnxruntime_gpu-1.23.0-cp310-cp310-linux_aarch64.whl
```

### Build the TensorRT engine (before using `--model *.engine`)

Engines are compiled on the device. They are specific to that Jetson, its
TensorRT/JetPack version and the `--imgsz` you build them at, so nothing
downloads one for you and you cannot copy one between machines:

```bash
python3 pose_stream.py --model yolo26n-pose.pt --imgsz 640 --half --export-engine
```

This downloads `yolo26n-pose.pt` if needed and writes `yolo26n-pose.engine` beside
it. It takes several minutes on an Orin Nano and the fan will spin up — let it
finish. Then run with the path it prints, absolute if you start the receiver from
another directory:

```bash
python3 pose_stream.py --model /home/you/yolo26n-pose.engine --connect tcp://<pi-ip>:5555
```

If the export dies with `pybind11::init(): factory function returned nullptr` at
`trt.Builder`, TensorRT's bindings loaded but could not initialise against the
driver. Diagnose with:

```bash
python3 pose_stream.py --check-env
```

Two causes account for nearly all of these. First, a pip-installed `tensorrt`
shadowing JetPack's — Ultralytics runs `check_requirements("tensorrt")` during
export and, if the system package is not visible from your venv, installs the
PyPI wheel, which targets desktop CUDA and cannot open a Tegra device. `--check-env`
flags this when tensorrt resolves under `site-packages` instead of
`dist-packages`:

```bash
pip uninstall -y tensorrt tensorrt-cu12 tensorrt_lean tensorrt_dispatch
```

and recreate the venv with `--system-site-packages` so JetPack's copy is visible.
Second, `torch.cuda.is_available()` being False — TensorRT cannot build without a
working CUDA context, so fix the Tegra torch wheels first.

Rebuild the engine whenever you change `--imgsz`, upgrade JetPack, or move to a
different board. Skipping this entirely is fine — pass the `.pt` and it runs
through PyTorch instead, slower and with more host RAM, but with no build step.

The wheel URLs above are specific to **JetPack 6.1 / cp310**. For any other
JetPack or Python version, get the matching wheels from
<https://docs.ultralytics.com/guides/nvidia-jetson/>.

If you work inside a venv, create it with `--system-site-packages`: TensorRT's
Python bindings ship as a JetPack apt package and have no pip wheel, so
`import tensorrt` fails in an isolated venv and `.engine` models won't load.

Verify CUDA is actually live before blaming the scripts:

```bash
python3 -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

---

## Testing with a video file instead of a camera

`file_sender.py` plays a recording onto the wire as if it were the Pi camera. It
reuses pi_sender.py's compression and network code, so the test path matches the
real one — keep the two files together. Pace is driven by each frame's
presentation timestamp, so variable-frame-rate phone recordings play at their
true speed, and frames are skipped rather than sent late if decoding falls
behind.

```bash
python3 file_sender.py --video row.mov --bind 'tcp://*:5555' --loop
```

MOV files from phones carry a rotation in their display matrix. OpenCV applies it
automatically and the detected angle is logged at startup — worth reading, since
a sideways frame quietly wrecks pose estimation. `--orientation none` disables it
for a wrongly-tagged file. Other useful flags: `--speed` to run faster than real
time, `--start`/`--duration` to isolate a segment, `--width/--height` to match the
Pi's output size, and `--no-realtime` to push frames as fast as they decode.

## Smoke test without a camera or a Jetson

Both scripts run on any Linux box with `pip install imagezmq pyzmq opencv-python`
(the receiver additionally needs ultralytics + torch). Terminal 1:

```bash
python3 pi_sender.py --backend dummy --bind 'tcp://*:5555' --fps 20 --pace --ratio 40
```

Terminal 2:

```bash
python3 pose_stream.py --connect tcp://127.0.0.1:5555 --fps 12 --device cpu --no-half | jq -c 'select(.type=="pose") | {tick,src,n,age_ms,infer_ms}'
```

The sender's stderr should report a steady achieved ratio, and the receiver's
`tick` rate should hold at exactly `--fps` regardless of the sender's rate.

