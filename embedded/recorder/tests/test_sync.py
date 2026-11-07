"""The clock mapping: recover true capture times from a sender clock that
drifts, through a delay that jitters, across a reboot."""

from __future__ import annotations

import random

from recorder.format import Session
from recorder.sync import Obs, compare, fit_sender, fit_session

from .helpers import write_session

MIN_DELAY_S = 0.002


def synthetic(seconds: float = 120.0, hz: float = 20.0, drift_ppm: float = 50.0,
              reboot_at: float | None = None, seed: int = 1):
    """A sender sampling at `hz`, its clock running `drift_ppm` fast, each
    sample arriving after 2 ms + an exponential delay (mean 6 ms, with the
    odd 100 ms Wi-Fi stall). Delivery is in order, as over TCP: a stalled
    sample holds up the ones behind it. Returns observations and the true
    capture times on the host clock."""
    rnd = random.Random(seed)
    obs, truth = [], []
    last_arrival = 0.0
    host0 = 5000.0
    origin = 123.0                      # sender uptime at host0
    for i in range(int(seconds * hz)):
        t = i / hz
        if reboot_at is not None and t >= reboot_at:
            origin = 0.5 - reboot_at    # rebooted: uptime restarts near 0
        cap_host = host0 + t
        sender_s = origin + t * (1 + drift_ppm * 1e-6)
        delay = MIN_DELAY_S + rnd.expovariate(1 / 0.006)
        if rnd.random() < 0.01:
            delay += 0.1
        last_arrival = max(last_arrival, cap_host + delay)
        tm = int(last_arrival * 1e9)
        ms = sender_s * 1000.0
        obs.append(Obs(tm, ms, ms, i))
        truth.append(cap_host)
    return obs, truth


def test_recovers_capture_times_through_jitter_and_drift():
    obs, truth = synthetic(drift_ppm=50.0)
    fit = fit_sender("s", "t_ms", obs)
    # Capture times come out late by exactly the minimum delay, which no
    # one-way method can see; everything else is gone.
    err_ms = [(c - t - MIN_DELAY_S) * 1000 for c, t in
              zip(fit.capture_s, truth, strict=True)]
    assert max(abs(e) for e in err_ms) < 1.0
    assert fit.drift_ppm is not None and abs(fit.drift_ppm - 50.0) < 10.0
    assert fit.segments == 1


def test_arrival_time_alone_would_be_far_worse():
    """The point of all this: aligning by arrival smears by the jitter."""
    obs, truth = synthetic()
    arrival_err = [abs(o.tm / 1e9 - t) * 1000 for o, t in zip(obs, truth, strict=True)]
    assert max(arrival_err) > 50.0


def test_out_of_order_delivery_is_not_a_reboot():
    obs, _ = synthetic(seconds=20.0)
    # Swap neighbours' arrival times: what reordering in transit looks like.
    for i in range(0, len(obs) - 1, 7):
        obs[i].tm, obs[i + 1].tm = obs[i + 1].tm, obs[i].tm
    assert fit_sender("s", "t_ms", obs).segments == 1


def test_drift_needs_a_long_enough_segment():
    obs, _ = synthetic(seconds=30.0)
    assert fit_sender("s", "t_ms", obs).drift_ppm is None


def test_a_reboot_starts_a_new_segment():
    obs, truth = synthetic(seconds=60.0, reboot_at=30.0)
    fit = fit_sender("s", "t_ms", obs)
    assert fit.segments == 2
    err_ms = [(c - t - MIN_DELAY_S) * 1000 for c, t in
              zip(fit.capture_s, truth, strict=True)]
    assert max(abs(e) for e in err_ms) < 1.0


def test_fit_session_maps_every_device(tmp_path):
    write_session(tmp_path, seconds=3.0)
    with Session(tmp_path) as s:
        fits = fit_session(s)
    assert set(fits) == {"rower/1/seat", "rower/2/seat", "cam1", "apriltag:cam1"}
    assert fits["cam1"].anchor == "t_send_ms"
    assert fits["apriltag:cam1"].kind == "host"
    # The session was written with a constant 4 ms delay for the seats, and
    # the tee stamp 0.2 ms before the recorder's: nothing above the minimum.
    for dev in ("rower/1/seat", "rower/2/seat", "cam1"):
        assert max(abs(x) for x in fits[dev].latency_ms) < 0.01
    # Detector records were stamped 50 ms after capture.
    assert all(abs(x - 50.0) < 0.01 for x in fits["apriltag:cam1"].latency_ms)


def test_seat_and_camera_land_on_one_axis(tmp_path):
    """In helpers.write_session, sample i of seat 1 is captured 3 ms after
    the session's i/20 s mark and frame j at j/15 s. Mapped through their own,
    unrelated clocks they must keep exactly that relationship."""
    w = write_session(tmp_path, seconds=3.0)
    with Session(tmp_path) as s:
        fits = fit_session(s)
    seat1, cam = fits["rower/1/seat"], fits["cam1"]
    base = w["tm0"] / 1e9
    seat_rel = [c - base - i / 20 for i, c in enumerate(seat1.capture_s)]
    cam_rel = [c - base - j / 15 for j, c in enumerate(cam.capture_s)]
    # Each is late by its own minimum delay (4 ms and 30+9-... ms resp.);
    # within a device the residual is constant, i.e. no jitter introduced.
    assert max(seat_rel) - min(seat_rel) < 1e-4
    assert max(cam_rel) - min(cam_rel) < 1e-4


def test_compare_finds_one_device_out_of_line():
    a_obs, _ = synthetic(seconds=30.0, seed=1)
    b_obs, _ = synthetic(seconds=30.0, seed=2)
    fa = {"x": fit_sender("x", "t_ms", a_obs), "y": fit_sender("y", "t_ms", b_obs)}

    def shifted(obs, by_s):
        return [Obs(o.tm + int(by_s * 1e9), o.anchor_ms, o.cap_ms, o.seq) for o in obs]

    good = {"x": fit_sender("x", "t_ms", shifted(a_obs, 7.0)),
            "y": fit_sender("y", "t_ms", shifted(b_obs, 7.0))}
    c = compare(fa, good)
    assert abs(c["shift_s"] - 7.0) < 1e-6
    assert c["abs_p95_ms"] < 0.01

    # Delay y's arrivals by 5 ms more than x's: its mapping moves by 5 ms.
    bad = {"x": good["x"], "y": fit_sender("y", "t_ms", shifted(b_obs, 7.005))}
    c = compare(fa, bad)
    assert 4.0 < c["max_bias_ms"] < 6.0
