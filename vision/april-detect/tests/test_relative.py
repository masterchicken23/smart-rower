"""The rower tag measured against the boat's reference tag: relative.oar() on
hand-built tag records, then end to end through Pipeline.process() on frames
rendered with two 5 cm tags at known poses.

What is pinned: distance is the camera-frame separation (it needs no rotation
at all), `rel_m` is in the reference tag's own frame, `height_m` follows the
configured up axis, and a missing tag gives `null` rather than a guess.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import ValidationError

from april_detect import pose, relative, synth
from april_detect.calib import Calibration
from tests.test_pipeline import H, K, W, frame, run, settings

TAG_M = 0.05


def tag(tag_id: int, pos, R=None, err_px=0.1, edge_px=50.0, amb=0.05):
    """A tag record as pipeline._tag builds it, with a square of `edge_px`."""
    R = np.eye(3) if R is None else R
    e = edge_px / 2
    return {
        "id": tag_id, "size_m": TAG_M,
        "pos_m": list(pos), "quat": pose.R_to_quat(R),
        "dist_m": float(np.linalg.norm(pos)),
        "corners_px": [[-e, e], [e, e], [e, -e], [-e, -e]],
        "err_px": err_px, "ambiguity": amb,
    }


# ---- relative.oar on records ------------------------------------------- #
def test_distance_is_camera_frame_separation():
    ref = tag(7, [0.0, 0.0, 1.0])
    oar = tag(0, [0.3, -0.4, 1.0])
    out = relative.oar([ref, oar], oar_id=0, ref_id=7, up_axis="-y")
    assert out["oar_id"] == 0 and out["ref_id"] == 7
    assert out["dist_m"] == pytest.approx(0.5, abs=1e-4)


def test_distance_ignores_reference_rotation():
    """The flip ambiguity spoils a small tag's rotation, never its position, so
    the separation must not depend on the reference tag's rotation at all."""
    a = tag(7, [0.0, 0.0, 1.0], R=np.eye(3))
    b = tag(7, [0.0, 0.0, 1.0], R=synth.euler_to_R(30, -20, 45))
    oar = tag(0, [0.2, 0.1, 1.3])
    d_a = relative.oar([a, oar], 0, 7, "-y")["dist_m"]
    d_b = relative.oar([b, oar], 0, 7, "-y")["dist_m"]
    assert d_a == pytest.approx(d_b, abs=1e-4)


def test_rel_is_in_reference_tag_frame():
    """Reference spun 90 degrees in its own plane: camera +x is the reference
    tag's -y (its up), so an oar 0.2 m to the camera's right is 0.2 m above."""
    R_ref = synth.euler_to_R(0, 0, 90)
    ref = tag(7, [0.0, 0.0, 1.0], R=R_ref)
    oar = tag(0, [0.2, 0.0, 1.0])
    out = relative.oar([ref, oar], 0, 7, "-y")
    expect = R_ref.T @ np.array([0.2, 0.0, 0.0])
    assert np.allclose(out["rel_m"], expect, atol=1e-4)
    assert out["height_m"] == pytest.approx(0.2, abs=1e-4)


@pytest.mark.parametrize("axis,expect", [
    ("-y", 0.1), ("+y", -0.1), ("-z", -0.05), ("+z", 0.05),
    ("+x", 0.2), ("-x", -0.2)])
def test_height_follows_the_up_axis(axis, expect):
    """A reference facing the camera upright: tag -y is up in the image, -z is
    toward the camera. Oar 0.2 right, 0.1 up, 0.05 further away."""
    ref = tag(7, [0.0, 0.0, 1.0])
    oar = tag(0, [0.2, -0.1, 1.05])
    out = relative.oar([ref, oar], 0, 7, axis)
    assert out["height_m"] == pytest.approx(expect, abs=1e-4)
    assert out["up_axis"] == axis


@pytest.mark.parametrize("present", [[7], [0], []])
def test_missing_tag_is_null(present):
    tags = [tag(i, [0.1 * i, 0.0, 1.0]) for i in present]
    assert relative.oar(tags, 0, 7, "-y") is None


def test_error_estimate_from_fit_residual():
    """Each tag's depth uncertainty is its distance times residual over edge
    length (scale is what fixes depth); the two add in quadrature."""
    ref = tag(7, [0.0, 0.0, 1.0], err_px=0.5, edge_px=50.0)   # 10 mm
    oar = tag(0, [0.0, 0.0, 2.0], err_px=0.3, edge_px=25.0)   # 24 mm
    out = relative.oar([ref, oar], 0, 7, "-y")
    assert out["err_m"] == pytest.approx(math.hypot(0.010, 0.024), abs=1e-4)
    assert out["ref_ambiguity"] == 0.05


def test_no_residual_means_no_error_estimate():
    """Detector-native pose reports no residual, so no estimate either."""
    ref = tag(7, [0.0, 0.0, 1.0], err_px=None)
    oar = tag(0, [0.1, 0.0, 1.0])
    assert relative.oar([ref, oar], 0, 7, "-y")["err_m"] is None


# ---- settings ---------------------------------------------------------- #
def test_ids_from_empty_env_are_unset():
    s = settings(oar_tag_id="", ref_tag_id="")
    assert s.oar_tag_id is None and s.ref_tag_id is None


@pytest.mark.parametrize("kw", [
    {"oar_tag_id": 0},                                  # half configured
    {"ref_tag_id": 7},
    {"oar_tag_id": 3, "ref_tag_id": 3},                 # same tag
    {"oar_tag_id": 0, "ref_tag_id": 7, "tag_ids": "0"}, # ref filtered out
])
def test_bad_pairing_is_refused(kw):
    with pytest.raises(ValidationError):
        settings(**kw)


# ---- end to end -------------------------------------------------------- #
def two_tags(ref_R=None):
    ref = {"id": 7, "size_m": TAG_M,
           "R": synth.euler_to_R(0, 20, 0) if ref_R is None else ref_R,
           "t": [-0.12, 0.05, 0.75]}
    oar = {"id": 0, "size_m": TAG_M, "R": synth.euler_to_R(10, -15, 5),
           "t": [0.10, -0.06, 0.70]}
    return ref, oar


def test_pipeline_publishes_oar_against_ground_truth():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    ref, oar = two_tags()
    s = settings(tag_size_m=TAG_M, oar_tag_id=0, ref_tag_id=7)
    rec = run(s, frame(cal, [ref, oar]))
    assert rec["n"] == 2
    got = rec["oar"]
    d_true = float(np.linalg.norm(np.subtract(oar["t"], ref["t"])))
    rel_true = ref["R"].T @ np.subtract(oar["t"], ref["t"])
    assert got["dist_m"] == pytest.approx(d_true, abs=0.005)
    assert np.allclose(got["rel_m"], rel_true, atol=0.01)
    assert got["height_m"] == pytest.approx(-rel_true[1], abs=0.01)
    assert got["err_m"] is not None and got["err_m"] < 0.01


def test_pipeline_oar_null_when_reference_out_of_view():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    _, oar = two_tags()
    s = settings(tag_size_m=TAG_M, oar_tag_id=0, ref_tag_id=7)
    rec = run(s, frame(cal, [oar]))
    assert "oar" in rec and rec["oar"] is None


def test_pipeline_without_pairing_has_no_oar_field():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    rec = run(settings(tag_size_m=TAG_M), frame(cal, list(two_tags())))
    assert "oar" not in rec


def test_meta_describes_the_pairing():
    from april_detect.pipeline import Pipeline

    s = settings(tag_size_m=TAG_M, oar_tag_id=0, ref_tag_id=7,
                 ref_up_axis="-z")
    m = Pipeline(s).meta()
    assert m["oar"] == {"oar_id": 0, "ref_id": 7, "up_axis": "-z"}
    assert Pipeline(settings()).meta()["oar"] is None
