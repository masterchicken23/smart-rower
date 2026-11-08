"""log() must never wait on stderr: a stuck stream (a dead SSH pty, a
journald behind a busy card) costs log lines, never time on the caller."""

from __future__ import annotations

import threading
import time

from cam_sender.log import AsyncLog


class Stuck:
    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.out: list[str] = []

    def write(self, s: str) -> None:
        self.gate.wait(10)
        self.out.append(s)

    def flush(self) -> None:
        pass


def test_log_never_waits_on_a_stuck_stream():
    gate = threading.Event()
    st = Stuck(gate)
    lg = AsyncLog(st, max_lines=10)
    t0 = time.perf_counter()
    for i in range(200):
        lg.write(f"line {i}\n")
    assert time.perf_counter() - t0 < 0.1
    assert lg.dropped > 0
    assert lg.flush(0.05) is False                 # still stuck: says so, returns
    gate.set()
    assert lg.flush(2.0) is True
    text = "".join(st.out)
    assert "line 199" in text
    assert "dropped" in text


def test_log_survives_a_dead_stream():
    class Dead:
        def write(self, _s: str) -> None:
            raise OSError(5, "Input/output error")    # EIO from a closed pty

        def flush(self) -> None:
            pass

    lg = AsyncLog(Dead())
    lg.write("x\n")
    assert lg.flush(1.0)
    lg.write("y\n")
    assert lg.flush(1.0)
    assert lg.errors == 2
