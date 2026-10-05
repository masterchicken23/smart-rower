#!/usr/bin/env python3
"""
pi_mjpeg_pub.py  —  Pi Zero W camera -> ZeroMQ JPEG publisher

Forwards the camera's own MJPEG frames without decoding or re-encoding,
so the Pi Zero's CPU stays nearly idle. Falls back to cv2.imencode if the
OpenCV build can't hand over raw MJPEG.

Message: multipart [topic, jpeg_bytes]   (matches apriltag_zmq_tracker.py)

  python3 pi_mjpeg_pub.py --device /dev/video0 --width 640 --height 480 --fps 30
"""
import argparse
import time

import cv2
import zmq

ap = argparse.ArgumentParser()
ap.add_argument("--device", default="/dev/video0")
ap.add_argument("--width", type=int, default=640)
ap.add_argument("--height", type=int, default=480)
ap.add_argument("--fps", type=int, default=30)
ap.add_argument("--port", type=int, default=5555)
ap.add_argument("--topic", default="cam")
ap.add_argument("--quality", type=int, default=80, help="only used on the re-encode fallback")
args = ap.parse_args()

cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
cap.set(cv2.CAP_PROP_FPS, args.fps)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)  # ask for the raw MJPEG buffer
if not cap.isOpened():
    raise SystemExit(f"cannot open {args.device}")

sock = zmq.Context.instance().socket(zmq.PUB)
sock.setsockopt(zmq.SNDHWM, 2)  # drop rather than queue stale frames
sock.bind(f"tcp://*:{args.port}")
topic = args.topic.encode()
print(f"publishing {args.device} on tcp://*:{args.port} topic='{args.topic}'", flush=True)

n, t0, mode = 0, time.monotonic(), None
while True:
    ok, frame = cap.read()
    if not ok:
        time.sleep(0.01)
        continue
    if frame.ndim == 1 or frame.shape[0] == 1:  # raw MJPEG bytes
        data, mode = frame.tobytes(), mode or "passthrough"
    else:  # driver gave decoded pixels
        ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
        if not ok:
            continue
        data, mode = enc.tobytes(), mode or "re-encode"
    sock.send_multipart([topic, data], copy=False)
    n += 1
    if time.monotonic() - t0 >= 5:
        print(f"{n / (time.monotonic() - t0):.1f} fps ({mode}, {len(data) // 1024} KB/frame)", flush=True)
        n, t0 = 0, time.monotonic()
