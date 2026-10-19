#!/usr/bin/env python3
"""
Fisheye (Kannala-Brandt / OpenCV `cv2.fisheye`) calibration from a checkerboard,
writing the ROS camera_info YAML that april_detect.calib.from_file() reads.

Input is a video of the board being moved in front of the camera, a folder of
stills, or both. For a video, frames are sampled, and a frame is kept only if
the board was found, is sharp, and sits somewhere new in the image: a
calibration needs varied views, not a hundred copies of one.

    python tools/calibrate_fisheye.py clip.mp4 --board 9x6 --square-mm 25 \\
        --out config/cam1.yaml
    python tools/calibrate_fisheye.py stills/ --board 9x6 --square-mm 25 --out cam1.yaml
    python tools/calibrate_fisheye.py clip.mp4 --board 9x6 --square-mm 25 --dry-run

--board is the number of INNER corners (columns x rows), not squares: a board
of 10x7 squares is 9x6. Record the clip with the same camera, mode (resolution,
crop), flips and focus that the detector will see -- see the README's
"Calibration" section for how to capture it. Run with --preview to write a
raw-vs-rectified picture and check by eye that the straight lines come out
straight, to the frame edge.

Exit status is non-zero when the result should not be used (too few views, or an
RMS reprojection error above --max-rms), unless --force.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
MIN_VIEWS = 10


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


@dataclass
class View:
    name: str
    corners: np.ndarray            # (N, 2) float64, pixels
    sharpness: float


# --------------------------------------------------------------------------- #
# Frames in
# --------------------------------------------------------------------------- #
def iter_frames(src: Path, every_s: float) -> Iterator[tuple[str, np.ndarray]]:
    """(name, greyscale frame) from a video, sampled every `every_s` seconds,
    or from every image in a folder (or a single image)."""
    import cv2

    if src.is_dir():
        for p in sorted(src.iterdir()):
            if p.suffix.lower() in IMAGE_EXTS:
                img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if img is None:
                    log(f"[skip] {p}: unreadable")
                    continue
                yield p.name, img
        return
    if src.suffix.lower() in IMAGE_EXTS:
        img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise SystemExit(f"{src}: unreadable image")
        yield src.name, img
        return

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise SystemExit(f"{src}: cannot open as a video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    step = max(1, round(every_s * fps)) if fps > 0 else 1
    n = 0
    try:
        while cap.grab():
            if n % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    yield (f"{src.name}@{n / fps:.1f}s" if fps > 0
                           else f"{src.name}#{n}"), (
                        cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                        if frame.ndim == 3 else frame)
            n += 1
    finally:
        cap.release()


# --------------------------------------------------------------------------- #
# Board detection and view selection
# --------------------------------------------------------------------------- #
def find_board(gray: np.ndarray, board: tuple[int, int]) -> Optional[np.ndarray]:
    """Inner-corner pixels (N, 2), in a consistent order, or None.

    The sector-based detector is tried first: it copes with strong lens
    distortion and a partly blurred board far better than the classic one,
    which is why it is the default for a fisheye."""
    import cv2

    ok, c = cv2.findChessboardCornersSB(
        gray, board, cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY)
    if not ok:
        ok, c = cv2.findChessboardCorners(
            gray, board,
            cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
            | cv2.CALIB_CB_FAST_CHECK)
        if not ok:
            return None
        c = cv2.cornerSubPix(
            gray, c, (5, 5), (-1, -1),
            (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))
    return c.reshape(-1, 2).astype(np.float64)


def sharpness(gray: np.ndarray, corners: np.ndarray) -> float:
    """Variance of the Laplacian inside the board's bounding box: higher is
    sharper. Only comparable between frames of the same clip, which is how it
    is used."""
    import cv2

    x0, y0 = np.maximum(corners.min(0).astype(int) - 8, 0)
    x1, y1 = corners.max(0).astype(int) + 8
    roi = gray[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    return float(cv2.Laplacian(roi, cv2.CV_64F).var())


def choose_views(cands: list[View], blur_frac: float, min_move_px: float,
                 max_views: int) -> list[View]:
    """Drop blurred views, then keep a view only if its corners moved at least
    `min_move_px` (RMS) from every view already kept, so the set spans the
    frame instead of repeating one pose. Over `max_views`, thin evenly."""
    if not cands:
        return []
    floor = blur_frac * float(np.median([v.sharpness for v in cands]))
    kept: list[View] = []
    for v in cands:
        if v.sharpness < floor:
            continue
        if all(np.sqrt(np.mean((v.corners - k.corners) ** 2)) >= min_move_px
               for k in kept):
            kept.append(v)
    if len(kept) > max_views:
        idx = np.linspace(0, len(kept) - 1, max_views).round().astype(int)
        kept = [kept[i] for i in idx]
    return kept


def coverage(views: list[View], size: tuple[int, int], n: int = 3) -> np.ndarray:
    """Corner counts per cell of an n x n grid over the image."""
    w, h = size
    grid = np.zeros((n, n), int)
    for v in views:
        gx = np.clip((v.corners[:, 0] / w * n).astype(int), 0, n - 1)
        gy = np.clip((v.corners[:, 1] / h * n).astype(int), 0, n - 1)
        np.add.at(grid, (gy, gx), 1)
    return grid


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    K: np.ndarray
    D: np.ndarray                  # (4,)
    size: tuple[int, int]
    rms: float                     # OpenCV's overall RMS, px
    per_view: list[float]          # RMS reprojection error per kept view, px
    views: list[View]              # the views the result was computed from


def board_points(board: tuple[int, int], square_mm: float) -> np.ndarray:
    """(N, 3) object points in metres, row-major to match findChessboardCorners."""
    cols, rows = board
    p = np.zeros((rows * cols, 3), np.float64)
    p[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    return p * (square_mm / 1000.0)


def _calibrate_once(views: list[View], obj: np.ndarray, size: tuple[int, int],
                    fix_high_order: bool):
    import cv2

    def flag(name: str) -> int:
        # OpenCV 4.x keeps these under cv2.fisheye, 5.x moved them to cv2.
        return int(getattr(cv2.fisheye, name, None) or getattr(cv2, name))

    # CALIB_CHECK_COND is left off: it rejects near face-on views of the board
    # with an error that can stop the whole fit. A bad view is caught instead
    # by its own reprojection error, in calibrate().
    flags = flag("CALIB_RECOMPUTE_EXTRINSIC") | flag("CALIB_FIX_SKEW")
    if fix_high_order:
        flags |= flag("CALIB_FIX_K3") | flag("CALIB_FIX_K4")
    objs = [obj.reshape(1, -1, 3) for _ in views]
    imgs = [v.corners.reshape(1, -1, 2) for v in views]
    K, D = np.eye(3), np.zeros((4, 1))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-12)
    rms, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
        objs, imgs, size, K, D, None, None, flags, crit)
    return rms, K, D.reshape(-1), rvecs, tvecs


def _view_errors(views, obj, K, D, rvecs, tvecs) -> list[float]:
    import cv2

    errs = []
    for v, r, t in zip(views, rvecs, tvecs, strict=True):
        proj, _ = cv2.fisheye.projectPoints(obj.reshape(-1, 1, 3), r, t, K, D)
        d = proj.reshape(-1, 2) - v.corners
        errs.append(float(np.sqrt(np.mean(np.sum(d * d, axis=1)))))
    return errs


def calibrate(views: list[View], board: tuple[int, int], square_mm: float,
              size: tuple[int, int], fix_high_order: bool = False,
              max_view_err: float = 2.0) -> Result:
    """Fisheye calibration that survives bad views.

    After a fit, if some view's own error is above `max_view_err` x the overall
    RMS (and at least 1 px), the view whose removal lowers the RMS most is
    dropped and the fit repeated."""
    import cv2

    obj = board_points(board, square_mm)
    views = list(views)
    drops_left = max(2, len(views) // 4)
    while True:
        if len(views) < MIN_VIEWS:
            raise ValueError(f"only {len(views)} usable views left (need "
                             f"{MIN_VIEWS}); capture more, and more varied, views")
        rms, K, D, rv, tv = _calibrate_once(views, obj, size, fix_high_order)
        errs = _view_errors(views, obj, K, D, rv, tv)
        worst = int(np.argmax(errs))
        if errs[worst] > max(1.0, max_view_err * rms):
            # One gross outlier drags every coefficient, so the view with the
            # biggest error is not necessarily the culprit. Refit without each
            # view in turn and drop the one whose absence helps most.
            if drops_left == 0:
                raise ValueError(
                    f"the fit will not settle (RMS {rms:.2f}px after dropping "
                    f"a quarter of the views): check --board and --square-mm, "
                    f"that the board is flat, and that the lens and mode are "
                    f"unchanged during the recording")
            drops_left -= 1
            culprit, best = worst, float("inf")
            for i in range(len(views)):
                try:
                    r = _calibrate_once(views[:i] + views[i + 1:], obj, size,
                                        fix_high_order)[0]
                except cv2.error:
                    continue
                if r < best:
                    culprit, best = i, r
            log(f"[calib] dropping {views[culprit].name}: RMS {rms:.2f}px "
                f"-> {best:.2f}px without it")
            del views[culprit]
            continue
        return Result(K, D, size, float(rms), errs, views)


# --------------------------------------------------------------------------- #
# Out
# --------------------------------------------------------------------------- #
def to_yaml(res: Result, camera_name: str) -> str:
    """ROS camera_info layout, as april_detect.calib.from_file() reads it."""
    w, h = res.size
    k = ", ".join(f"{v:.6f}" for v in res.K.ravel())
    d = ", ".join(f"{v:.8f}" for v in res.D)
    return (
        f"# Fisheye calibration from tools/calibrate_fisheye.py: {len(res.views)} views, "
        f"RMS {res.rms:.3f} px.\n"
        f"# Valid only for this camera at {w}x{h} with the same crop, flips and lens\n"
        f"# setup it was recorded with. Use with APRIL_UNDISTORT=points.\n"
        f"image_width: {w}\n"
        f"image_height: {h}\n"
        f"camera_name: {camera_name}\n"
        f"camera_matrix:\n"
        f"  rows: 3\n"
        f"  cols: 3\n"
        f"  data: [{k}]\n"
        f"distortion_model: equidistant\n"
        f"distortion_coefficients:\n"
        f"  rows: 1\n"
        f"  cols: 4\n"
        f"  data: [{d}]\n"
    )


def write_preview(path: Path, gray: np.ndarray, res: Result) -> None:
    """Raw frame beside its rectified version (balance 0.5: a wide but not
    degenerate field), with a grid, so straightness can be judged by eye."""
    import cv2

    w, h = res.size
    Kn = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
        res.K, res.D, (w, h), np.eye(3), balance=0.5)
    m1, m2 = cv2.fisheye.initUndistortRectifyMap(
        res.K, res.D, np.eye(3), Kn, (w, h), cv2.CV_16SC2)
    rect = cv2.remap(gray, m1, m2, cv2.INTER_LINEAR)
    pair = cv2.cvtColor(np.hstack([gray, rect]), cv2.COLOR_GRAY2BGR)
    for x in range(0, 2 * w, 80):
        cv2.line(pair, (x, 0), (x, h), (0, 200, 0), 1)
    for y in range(0, h, 80):
        cv2.line(pair, (0, y), (2 * w, y), (0, 200, 0), 1)
    cv2.imwrite(str(path), pair)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fisheye calibration from a checkerboard video or stills; "
                    "writes the ROS camera_info YAML april-detect loads.")
    ap.add_argument("source", nargs="+", type=Path,
                    help="video file(s), image file(s) or folder(s) of stills")
    ap.add_argument("--board", required=True, metavar="COLSxROWS",
                    help="INNER corners, e.g. 9x6 for a board of 10x7 squares")
    ap.add_argument("--square-mm", type=float, required=True,
                    help="edge of one square, mm")
    ap.add_argument("--out", type=Path, help="YAML to write")
    ap.add_argument("--name", default="cam1", help="camera_name in the YAML")
    ap.add_argument("--every-s", type=float, default=0.5,
                    help="video: sample one frame per this many seconds")
    ap.add_argument("--max-views", type=int, default=60)
    ap.add_argument("--min-move-px", type=float, default=25.0,
                    help="a view must move the board's corners this much (RMS) "
                         "from every kept view")
    ap.add_argument("--blur-frac", type=float, default=0.5,
                    help="drop views sharper-than-median by less than this "
                         "fraction")
    ap.add_argument("--fix-high-order", action="store_true",
                    help="fix k3 and k4 at 0 (fit k1, k2 only): steadier with "
                         "few views near the frame edge")
    ap.add_argument("--max-rms", type=float, default=1.0,
                    help="refuse a result above this RMS, px")
    ap.add_argument("--preview", type=Path,
                    help="write raw|rectified side by side (PNG) here")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only; write nothing")
    ap.add_argument("--force", action="store_true",
                    help="write the YAML even if it fails the checks")
    args = ap.parse_args()

    try:
        cols, rows = (int(v) for v in args.board.lower().split("x"))
    except ValueError:
        raise SystemExit("--board must look like 9x6") from None
    board = (cols, rows)

    cands: list[View] = []
    size: Optional[tuple[int, int]] = None
    sample: Optional[np.ndarray] = None
    seen = 0
    for src in args.source:
        for name, gray in iter_frames(src, args.every_s):
            seen += 1
            s = (gray.shape[1], gray.shape[0])
            if size is None:
                size = s
            elif s != size:
                raise SystemExit(f"{name} is {s[0]}x{s[1]} but earlier frames "
                                 f"are {size[0]}x{size[1]}: one calibration, "
                                 f"one resolution")
            c = find_board(gray, board)
            if c is None:
                continue
            cands.append(View(name, c, sharpness(gray, c)))
            if sample is None:
                sample = gray
    if size is None:
        raise SystemExit("no frames read")
    log(f"[frames] {seen} read, board found in {len(cands)}  ({size[0]}x{size[1]})")

    views = choose_views(cands, args.blur_frac, args.min_move_px, args.max_views)
    log(f"[views] {len(views)} kept (sharp and distinct)")
    if len(views) < MIN_VIEWS:
        log(f"[fail] need at least {MIN_VIEWS} views; got {len(views)}. Move the "
            f"board more between poses, or lower --min-move-px / --every-s")
        if not args.force:
            return 2

    try:
        res = calibrate(views, board, args.square_mm, size, args.fix_high_order)
    except Exception as e:  # noqa: BLE001 - report the reason, not a traceback
        log(f"[fail] {e}")
        return 2

    grid = coverage(res.views, size)
    log("[coverage] corners per image third (rows top->bottom, cols left->right):")
    for r in grid:
        log("    " + "  ".join(f"{int(v):5d}" for v in r))
    empty = [(i, j) for i in range(3) for j in range(3) if grid[i, j] == 0]
    if empty:
        log(f"[warn] no corners in {len(empty)} of 9 regions: the coefficients "
            f"are unconstrained there. Capture the board in those parts of the frame")
    elif grid.min() < 0.3 * grid.mean():
        log("[warn] thin coverage in some regions; put the board there more")

    log(f"[result] {len(res.views)} views, RMS {res.rms:.3f} px "
        f"(per view: median {np.median(res.per_view):.2f}, "
        f"max {max(res.per_view):.2f})")
    log(f"[result] fx {res.K[0, 0]:.2f}  fy {res.K[1, 1]:.2f}  "
        f"cx {res.K[0, 2]:.2f}  cy {res.K[1, 2]:.2f}   "
        f"D {np.array2string(res.D, precision=5)}")
    ok = res.rms <= args.max_rms
    if not ok:
        log(f"[fail] RMS {res.rms:.2f} px is above --max-rms {args.max_rms:g}")
    if abs(res.K[0, 0] - res.K[1, 1]) / res.K[0, 0] > 0.02:
        log("[warn] fx and fy differ by over 2%: square pixels are expected; "
            "check the board's square size and that the board is flat")

    if args.preview and sample is not None:
        write_preview(args.preview, sample, res)
        log(f"[preview] {args.preview}")
    if args.dry_run or not args.out:
        if not args.out and not args.dry_run:
            log("[note] no --out: nothing written")
        return 0 if ok else 2
    if not ok and not args.force:
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(to_yaml(res, args.name), encoding="utf-8")
    log(f"[wrote] {args.out}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
