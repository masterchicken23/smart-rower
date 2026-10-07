#!/usr/bin/env python3
"""
Entry point: `python -m april_detect`.

One process per camera. Configuration is entirely environment variables
(APRIL_*), for the reason given in config.py; there are no command-line flags.
SIGTERM from `docker stop` sets the stop event, the loop finishes its current
frame, and the sockets close with LINGER 0 so shutdown cannot hang on a peer.
"""

from __future__ import annotations

import signal
import sys
import threading

from . import log
from .config import load
from .pipeline import Pipeline


def main(argv: list[str] | None = None) -> int:
    s = load()
    log.set_level(s.log_level)

    # cv2 spawns a thread pool per process sized to every core. Four
    # containers doing that oversubscribe the Orin's six cores for no gain on
    # 720p grey; the detector has its own APRIL_THREADS.
    import cv2

    cv2.setNumThreads(s.cv_threads)

    stop = threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        log.info(f"[main] signal {signum}, stopping")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log.info(f"[main] april-detect starting: source={s.source} "
             f"out={s.out_address} cam={s.camera_id or '(from sender)'}")
    Pipeline(s).run(stop)
    log.info("[main] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
