"""The report's account of damage: missing frames and failed CRCs."""

from __future__ import annotations

import json

from recorder.format import INDEX_EXT, Session
from recorder.report import render, summarize

from .helpers import write_session


def test_report_counts_missing_and_damaged_frames(tmp_path):
    write_session(tmp_path, seconds=1.0)
    with Session(tmp_path) as sess:
        frames = list(sess.frames("cam1"))
    with open(tmp_path / "cam1.mjpeg", "r+b") as f:     # damage frame 3
        f.seek(frames[3].rec["off"] + 5)
        f.write(b"\x00")
    idx = tmp_path / ("cam1" + INDEX_EXT)
    lines = [json.loads(x) for x in idx.read_text().splitlines()]
    lines[7]["off"] = 10**12                             # an index line gone bad
    idx.write_text("".join(json.dumps(x) + "\n" for x in lines))

    with Session(tmp_path) as sess:
        quick, _ = summarize(sess)
        full, _ = summarize(sess, verify=True)
    assert quick["cameras"]["cam1"]["missing"] == 1
    assert quick["cameras"]["cam1"]["crc_bad"] is None   # not read without --verify
    assert full["cameras"]["cam1"]["crc_bad"] == 1
    assert "crc bad" in render(full)
