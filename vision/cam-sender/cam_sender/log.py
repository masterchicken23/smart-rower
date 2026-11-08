#!/usr/bin/env python3
"""
Logging that never blocks the caller.

The repo's usual `print(..., file=sys.stderr, flush=True)` would sit on the
frame loop here, and stderr can block:

  * run over SSH, a dropped connection leaves sshd holding the pty until its
    TCP keepalive gives up (hours). Once the tty buffer fills, the next stats
    line blocks -- and with it the frame loop, so frames stop being sent
    *and* recorded, which is exactly when the local recording is needed;
  * under systemd or Docker, stderr is a pipe to journald or the log driver,
    which can fall behind when the SD card is busy (the recording's own
    writes and fsyncs).

So `log()` only appends a line to a bounded deque. A daemon thread does the
writing, swallows errors (EIO from a dead pty), and says how many lines it
had to drop when it catches up. The output format is unchanged: one line,
`[subsystem]` prefix, on stderr.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Any, Optional, TextIO

MAX_LINES = 1000


class AsyncLog:
    def __init__(self, stream: Optional[TextIO] = None,
                 max_lines: int = MAX_LINES) -> None:
        self._stream = stream
        self._max = max(1, max_lines)
        self._q: deque = deque()
        self._cv = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._busy = False
        self._unreported = 0
        self.dropped = 0
        """Lines dropped because the stream was not keeping up."""
        self.errors = 0
        """Writes that raised (a closed pty, a broken pipe)."""

    @property
    def stream(self) -> TextIO:
        # Looked up at write time, so a replaced sys.stderr (pytest's capture)
        # is honoured.
        return self._stream if self._stream is not None else sys.stderr

    def write(self, line: str) -> None:
        """Queue one line. Never blocks for longer than a lock acquire."""
        with self._cv:
            if len(self._q) >= self._max:
                self._q.popleft()
                self.dropped += 1
                self._unreported += 1
            self._q.append(line)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="log",
                                                daemon=True)
                self._thread.start()
            self._cv.notify()

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait up to `timeout` s for everything queued to be written. False
        if the stream is still stuck -- the caller carries on regardless."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._q or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                lines = list(self._q)
                self._q.clear()
                dropped, self._unreported = self._unreported, 0
                self._busy = True
            if dropped:
                lines.insert(0, f"[log] {dropped} line(s) dropped: stderr was "
                                "not keeping up\n")
            try:
                s = self.stream
                s.write("".join(lines))
                s.flush()
            except Exception:                   # noqa: BLE001 -- see module doc
                self.errors += 1
            with self._cv:
                self._busy = False
                self._cv.notify_all()


_LOG = AsyncLog()


def log(*a: Any) -> None:
    """A line to stderr, as print(*a) would write it, without waiting."""
    _LOG.write(" ".join(str(x) for x in a) + "\n")


def flush(timeout: float = 1.0) -> bool:
    return _LOG.flush(timeout)
