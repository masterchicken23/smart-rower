#!/usr/bin/env python3
"""
apriltag_zmq_tracker.py  —  Jetson Orin Nano AprilTag tracker over ZeroMQ

Subscribes to a ZeroMQ PUB stream of JPEG frames, detects AprilTags and prints
each tag's position (meters) and rotation (degrees) in the camera frame.

Camera frame (OpenCV convention):  +X right, +Y down, +Z out of the lens.
Rotation is printed as ZYX Euler angles about those camera axes, zeroed so an
upright tag squarely facing the camera reads 0/0/0:
  rx = tilt toward/away (about X)   ry = turn left/right (about Y)
  rz = spin in the image plane (about Z)

Backends are picked automatically, fastest available first:

  detector  cuda   NVIDIA cuAprilTags (the CUDA detector inside Isaac ROS),
                   loaded via ctypes from libcuapriltags.so.  tag36h11 only.
            cpu    pupil_apriltags (multithreaded C)   pip install pupil-apriltags

  decoder   nvjpeg torchvision.io.decode_jpeg(device="cuda"); frame stays on GPU
                   and is handed straight to cuAprilTags (no host round trip).
            cpu    cv2.imdecode (grayscale-only decode for the cpu detector)

Wire format: each ZMQ message is single- or multi-part; the LAST part is the
JPEG. Any prefix before the JPEG start marker (FF D8) is ignored, so a
"topic" glued onto the front of a single-part message also works.

Examples
  python3 apriltag_zmq_tracker.py --endpoint tcp://cam-pi.local:5555 --tag-size 0.10
  python3 apriltag_zmq_tracker.py --calib camera.npz --detector cuda \
          --cuapriltags-lib ./libcuapriltags.so
  python3 apriltag_zmq_tracker.py --json > poses.jsonl
"""
import argparse
import ctypes
import json
import math
import os
import sys
import time

import numpy as np
import zmq
import cv2


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ─────────────────────────────── camera model ────────────────────────────────
class Intrinsics:
    def __init__(self, fx, fy, cx, cy):
        self.fx, self.fy, self.cx, self.cy = float(fx), float(fy), float(cx), float(cy)

    def key(self):
        return (self.fx, self.fy, self.cx, self.cy)

    @staticmethod
    def resolve(args, w, h):
        """Calibration file > explicit fx/fy/cx/cy > estimate from --hfov."""
        if args.calib:
            d = np.load(args.calib)
            K = None
            for k in ("K", "camera_matrix", "mtx"):
                if k in d:
                    K = np.asarray(d[k], dtype=np.float64)
                    break
            if K is None:
                raise SystemExit(f"{args.calib}: needs a 3x3 'K' / 'camera_matrix' / 'mtx' array")
            fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
            if "image_size" in d:  # rescale if calibrated at another resolution
                cw, ch = (float(v) for v in d["image_size"])
                sx, sy = w / cw, h / ch
                fx, cx, fy, cy = fx * sx, cx * sx, fy * sy, cy * sy
            return Intrinsics(fx, fy, cx, cy)
        if args.fx:
            return Intrinsics(args.fx, args.fy or args.fx,
                              args.cx if args.cx is not None else w / 2,
                              args.cy if args.cy is not None else h / 2)
        f = (w / 2) / math.tan(math.radians(args.hfov) / 2)
        log(f"[warn] no calibration given; assuming {args.hfov:.0f}° HFOV -> f={f:.1f}px. "
            "Distances will be approximate until you pass --calib.")
        return Intrinsics(f, f, w / 2, h / 2)


def rot_to_euler_deg(R):
    """ZYX (yaw-pitch-roll) Euler angles of a 3x3 rotation, in degrees."""
    sy = math.hypot(R[0, 0], R[1, 0])
    if sy > 1e-6:
        roll = math.atan2(R[2, 1], R[2, 2])
        pitch = math.atan2(-R[2, 0], sy)
        yaw = math.atan2(R[1, 0], R[0, 0])
    else:  # gimbal lock
        roll = math.atan2(-R[1, 2], R[1, 1])
        pitch = math.atan2(-R[2, 0], sy)
        yaw = 0.0
    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


# ─────────────────────────────── CUDA runtime ────────────────────────────────
class CudaRT:
    """Just enough of libcudart via ctypes to upload a frame."""
    H2D = 1

    def __init__(self):
        self.lib = None
        for name in ("libcudart.so", "libcudart.so.12", "libcudart.so.11.0",
                     "/usr/local/cuda/lib64/libcudart.so"):
            try:
                # RTLD_GLOBAL so libcuapriltags.so can resolve CUDA symbols from it
                self.lib = ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)
                break
            except OSError:
                continue
        if self.lib is None:
            raise OSError("libcudart not found (is CUDA / JetPack installed?)")
        L = self.lib
        L.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        L.cudaFree.argtypes = [ctypes.c_void_p]
        L.cudaMemcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        L.cudaGetErrorString.restype = ctypes.c_char_p
        self._buf, self._cap = None, 0

    def _check(self, rc, what):
        if rc != 0:
            raise RuntimeError(f"{what} failed: {self.lib.cudaGetErrorString(rc).decode()}")

    def upload(self, host):
        """Copy a contiguous numpy array into a reusable device buffer; return ptr."""
        n = host.nbytes
        if n > self._cap:
            if self._buf:
                self.lib.cudaFree(self._buf)
            p = ctypes.c_void_p()
            self._check(self.lib.cudaMalloc(ctypes.byref(p), n), "cudaMalloc")
            self._buf, self._cap = p, n
        self._check(self.lib.cudaMemcpy(self._buf, host.ctypes.data, n, self.H2D), "cudaMemcpy")
        return self._buf.value


# ───────────────────────────── cuAprilTags (CUDA) ────────────────────────────
class _Float2(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


class _CuTag(ctypes.Structure):
    # Mirrors cuAprilTagsID_t. CUDA's float2 is 8-byte aligned, so the C struct
    # is padded to 88 bytes; ctypes aligns float2 to 4, hence the explicit pad.
    _fields_ = [("corners", _Float2 * 4),
                ("id", ctypes.c_uint16),
                ("hamming_error", ctypes.c_uint8),
                ("orientation", ctypes.c_float * 9),  # 3x3, column-major
                ("translation", ctypes.c_float * 3),  # same units as tag_size
                ("_pad", ctypes.c_uint32)]


class _CuImage(ctypes.Structure):
    _fields_ = [("dev_ptr", ctypes.c_void_p),  # uchar3* (packed RGB on device)
                ("pitch", ctypes.c_size_t),
                ("width", ctypes.c_uint16),
                ("height", ctypes.c_uint16)]


class _CuIntrinsics(ctypes.Structure):
    _fields_ = [("fx", ctypes.c_float), ("fy", ctypes.c_float),
                ("cx", ctypes.c_float), ("cy", ctypes.c_float)]


assert ctypes.sizeof(_CuTag) == 88 and ctypes.sizeof(_CuImage) == 24


class CuAprilTagsDetector:
    name = "cuda (cuAprilTags)"
    wants = "rgb_device"
    FAMILIES = {"tag36h11": 0}

    def __init__(self, lib_path, family, tag_size, max_tags=32, tile_size=4):
        if family not in self.FAMILIES:
            raise ValueError(f"cuAprilTags supports {list(self.FAMILIES)} only")
        self.cuda = CudaRT()  # must load first (RTLD_GLOBAL)
        self.lib = ctypes.CDLL(lib_path)
        L = self.lib
        L.nvCreateAprilTagsDetector.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_int32, ctypes.POINTER(_CuIntrinsics), ctypes.c_float]
        L.cuAprilTagsDetect.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_CuImage), ctypes.POINTER(_CuTag),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.c_void_p]
        L.cuAprilTagsDestroy.argtypes = [ctypes.c_void_p]
        self.family = self.FAMILIES[family]
        self.tag_size, self.max_tags, self.tile = float(tag_size), max_tags, tile_size
        self.out = (_CuTag * max_tags)()
        self.handle, self.cfg = None, None

    def _ensure(self, w, h, K):
        cfg = (w, h, K.key())
        if cfg == self.cfg:
            return
        self.close()
        cam = _CuIntrinsics(K.fx, K.fy, K.cx, K.cy)
        hnd = ctypes.c_void_p()
        rc = self.lib.nvCreateAprilTagsDetector(ctypes.byref(hnd), w, h, self.tile,
                                                self.family, ctypes.byref(cam), self.tag_size)
        if rc != 0:
            raise RuntimeError(f"nvCreateAprilTagsDetector failed ({rc})")
        self.handle, self.cfg = hnd, cfg

    def detect(self, frame, K):
        """frame: (dev_ptr, pitch, w, h) of packed RGB on the GPU."""
        ptr, pitch, w, h = frame
        self._ensure(w, h, K)
        img = _CuImage(ptr, pitch, w, h)
        n = ctypes.c_uint32(0)
        rc = self.lib.cuAprilTagsDetect(self.handle, ctypes.byref(img), self.out,
                                        ctypes.byref(n), self.max_tags, None)
        if rc != 0:
            raise RuntimeError(f"cuAprilTagsDetect failed ({rc})")
        tags = []
        for i in range(n.value):
            t = self.out[i]
            R = np.array(t.orientation, dtype=np.float64).reshape(3, 3).T  # col-major -> R
            tags.append(dict(id=int(t.id), R=R,
                             t=np.array(t.translation, dtype=np.float64),
                             hamming=int(t.hamming_error),
                             corners=[(c.x, c.y) for c in t.corners]))
        return tags

    def close(self):
        if self.handle:
            self.lib.cuAprilTagsDestroy(self.handle)
            self.handle, self.cfg = None, None


# ────────────────────────────── pupil_apriltags (CPU) ────────────────────────
class PupilDetector:
    name = "cpu (pupil_apriltags)"
    wants = "gray_host"
    # pupil's tag frame is rotated 180° about Z vs. the camera; flip it so an
    # upright tag squarely facing the camera reads rx=ry=rz=0.
    R_FIX = np.diag([-1.0, -1.0, 1.0])

    def __init__(self, family, tag_size, threads, decimate):
        from pupil_apriltags import Detector
        self.det = Detector(families=family, nthreads=threads,
                            quad_decimate=decimate, refine_edges=1)
        self.tag_size = float(tag_size)

    def detect(self, gray, K):
        out = []
        for d in self.det.detect(gray, estimate_tag_pose=True,
                                 camera_params=(K.fx, K.fy, K.cx, K.cy),
                                 tag_size=self.tag_size):
            out.append(dict(id=int(d.tag_id), R=np.asarray(d.pose_R) @ self.R_FIX,
                            t=np.asarray(d.pose_t).ravel(), hamming=int(d.hamming),
                            corners=[tuple(c) for c in d.corners]))
        return out

    def close(self):
        pass


# ──────────────────────────────── JPEG decoders ──────────────────────────────
class CpuDecoder:
    name = "cpu (cv2.imdecode)"

    @staticmethod
    def gray(buf):
        return cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_GRAYSCALE)

    @staticmethod
    def rgb(buf):
        bgr = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
        return None if bgr is None else np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


class NvJpegDecoder:
    """GPU JPEG decode via torchvision (nvJPEG). Output stays in GPU memory."""
    name = "gpu (nvJPEG via torchvision)"

    def __init__(self):
        import torch
        from torchvision.io import decode_jpeg, ImageReadMode
        if not torch.cuda.is_available():
            raise RuntimeError("torch.cuda not available")
        self.torch, self._decode, self._mode = torch, decode_jpeg, ImageReadMode.RGB
        self._keep = None
        ok, enc = cv2.imencode(".jpg", np.full((16, 16, 3), 128, np.uint8))
        self.rgb_device(enc.tobytes())  # self-test: raises if nvJPEG isn't built in

    def rgb_device(self, buf):
        t = self.torch.frombuffer(bytearray(buf), dtype=self.torch.uint8)
        chw = self._decode(t, mode=self._mode, device="cuda")
        hwc = chw.permute(1, 2, 0).contiguous()
        self.torch.cuda.synchronize()
        self._keep = hwc  # keep tensor alive while the detector reads it
        h, w, _ = hwc.shape
        return hwc.data_ptr(), w * 3, w, h


# ─────────────────────────────── backend selection ───────────────────────────
def build_pipeline(args):
    det = None
    if args.detector in ("auto", "cuda"):
        try:
            det = CuAprilTagsDetector(args.cuapriltags_lib, args.family, args.tag_size)
        except Exception as e:
            if args.detector == "cuda":
                raise SystemExit(f"CUDA detector unavailable: {e}")
            log(f"[info] CUDA detector unavailable ({e}); using CPU")
    if det is None:
        det = PupilDetector(args.family, args.tag_size, args.threads, args.decimate)

    nvj = None
    if det.wants == "rgb_device" and args.decoder in ("auto", "nvjpeg"):
        try:
            nvj = NvJpegDecoder()
        except Exception as e:
            if args.decoder == "nvjpeg":
                raise SystemExit(f"nvJPEG decoder unavailable: {e}")
            log(f"[info] nvJPEG unavailable ({e}); decoding on CPU")
    elif args.decoder == "nvjpeg":
        log("[info] nvJPEG only helps the CUDA detector; decoding on CPU")

    def get_frame(jpeg):
        """Return (frame_for_detector, width, height) or None."""
        if det.wants == "gray_host":
            g = CpuDecoder.gray(jpeg)
            return None if g is None else (g, g.shape[1], g.shape[0])
        if nvj is not None:
            try:
                f = nvj.rgb_device(jpeg)
                return f, f[2], f[3]
            except Exception:
                pass  # e.g. camera MJPEG missing Huffman tables -> CPU path below
        rgb = CpuDecoder.rgb(jpeg)
        if rgb is None:
            return None
        h, w, _ = rgb.shape
        return (det.cuda.upload(rgb), w * 3, w, h), w, h

    dec_name = nvj.name if nvj else CpuDecoder.name
    return det, get_frame, dec_name


# ─────────────────────────────────── ZeroMQ ──────────────────────────────────
def make_socket(args):
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.SUB)
    s.setsockopt(zmq.RCVHWM, 4)
    s.setsockopt(zmq.SUBSCRIBE, args.topic.encode())
    s.connect(args.endpoint)
    return s


def recv_latest(sock, poller, timeout_ms):
    """Block for one message, then drain the queue so we always process the newest."""
    if sock not in dict(poller.poll(timeout_ms)):
        return None, 0
    msg, dropped = sock.recv_multipart(), 0
    while True:
        try:
            msg = sock.recv_multipart(zmq.NOBLOCK)
            dropped += 1
        except zmq.Again:
            return msg, dropped


def extract_jpeg(msg):
    data = msg[-1]
    i = data.find(b"\xff\xd8")
    return None if i < 0 else data[i:]


# ──────────────────────────────────── main ───────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default="tcp://cam-pi.local:5555", help="ZMQ PUB endpoint to connect to")
    ap.add_argument("--topic", default="", help="ZMQ subscription prefix ('' = everything)")
    ap.add_argument("--family", default="tag36h11")
    ap.add_argument("--tag-size", type=float, default=0.10,
                    help="edge length of the tag's black square, meters")
    ap.add_argument("--detector", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--decoder", choices=["auto", "nvjpeg", "cpu"], default="auto")
    ap.add_argument("--cuapriltags-lib", default=os.environ.get("CUAPRILTAGS_LIB", "./libcuapriltags.so"))
    ap.add_argument("--calib", help=".npz with K (3x3) [and optional image_size=(w,h)]")
    ap.add_argument("--fx", type=float)
    ap.add_argument("--fy", type=float)
    ap.add_argument("--cx", type=float)
    ap.add_argument("--cy", type=float)
    ap.add_argument("--hfov", type=float, default=70.0, help="fallback HFOV (deg) if uncalibrated")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4, help="CPU detector threads")
    ap.add_argument("--decimate", type=float, default=1.0,
                    help="CPU detector quad_decimate (2.0 = faster, shorter range)")
    ap.add_argument("--json", action="store_true", help="print one JSON object per tag")
    ap.add_argument("--timeout", type=float, default=2.0, help="seconds before 'no frames' warning")
    args = ap.parse_args()

    det, get_frame, dec_name = build_pipeline(args)
    sock = make_socket(args)
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    log(f"[info] detector={det.name}  decoder={dec_name}  endpoint={args.endpoint}")

    K, K_size = None, None
    frames = dropped_total = 0
    t_dec = t_det = 0.0
    t_stat = time.monotonic()
    try:
        while True:
            msg, dropped = recv_latest(sock, poller, int(args.timeout * 1000))
            if msg is None:
                log(f"[warn] no frames from {args.endpoint} in {args.timeout:.0f}s")
                continue
            dropped_total += dropped
            jpeg = extract_jpeg(msg)
            if jpeg is None:
                continue

            t0 = time.perf_counter()
            got = get_frame(jpeg)
            t1 = time.perf_counter()
            if got is None:
                continue
            frame, w, h = got
            if (w, h) != K_size:
                K, K_size = Intrinsics.resolve(args, w, h), (w, h)
                log(f"[info] {w}x{h}  fx={K.fx:.1f} fy={K.fy:.1f} cx={K.cx:.1f} cy={K.cy:.1f}")

            tags = det.detect(frame, K)
            t2 = time.perf_counter()
            frames += 1
            t_dec += t1 - t0
            t_det += t2 - t1

            ts = time.time()
            for tg in tags:
                x, y, z = (float(v) for v in tg["t"])
                rx, ry, rz = rot_to_euler_deg(tg["R"])
                if args.json:
                    print(json.dumps(dict(ts=round(ts, 4), id=tg["id"],
                                          x=x, y=y, z=z, rx=rx, ry=ry, rz=rz,
                                          hamming=tg["hamming"])), flush=True)
                else:
                    print(f"tag {tg['id']:3d} | pos m  x={x:+.3f} y={y:+.3f} z={z:+.3f} "
                          f"(d={math.sqrt(x*x+y*y+z*z):.3f}) | rot deg  rx={rx:+6.1f} "
                          f"ry={ry:+6.1f} rz={rz:+6.1f}", flush=True)

            now = time.monotonic()
            if now - t_stat >= 1.0:
                log(f"[stats] {frames / (now - t_stat):5.1f} fps  decode {1e3 * t_dec / max(frames, 1):.1f} ms"
                    f"  detect {1e3 * t_det / max(frames, 1):.1f} ms  dropped {dropped_total}")
                frames = dropped_total = 0
                t_dec = t_det = 0.0
                t_stat = now
    except KeyboardInterrupt:
        pass
    finally:
        det.close()
        sock.close(0)


if __name__ == "__main__":
    main()
