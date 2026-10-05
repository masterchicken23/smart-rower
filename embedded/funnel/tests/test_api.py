#!/usr/bin/env python3
"""
The REST surface, exercised in-process over ASGI -- no broker, no sensors, no
network, no running compute loop.

The tests drive one tick by hand and then read the API, which mirrors how the
service actually works: handlers only ever read what the last tick published,
so a test does not need the loop running to test the endpoints.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from funnel.api.app import create_app
from funnel.config import Settings
from funnel.models import Sample, StreamKey


def make_settings(**kw) -> Settings:
    base = {
        "mqtt_host": "127.0.0.1", "compute_hz": 10.0, "max_age_ms": 1000.0,
        "record_enabled": False, "buffer_seconds": 10.0, "cors": "*",
    }
    base.update(kw)
    return Settings(**base)


def push(store, key, *, age_s=0.0, seq=1, **values):
    store.push(key, Sample(t_recv=time.monotonic() - age_s, t_src=None,
                           seq=seq, values=values))


@pytest.fixture
def app_parts():
    """An app with one seat, one boat stream, and exactly one tick computed."""
    app = create_app(make_settings())
    parts = app.state.parts
    store = parts["store"]
    push(store, StreamKey("rower", 3, "seat"), d_mm=812, seq=412)
    push(store, StreamKey("rower", 7, "seat"), d_mm=640, seq=99)
    push(store, StreamKey("boat", None, "imu"), pitch_deg=2.5)
    store.put_presence(3, {"up": True, "dev": "ce40d0"})
    parts["loop"].tick_once()
    return app, parts


@pytest.fixture
async def client(app_parts):
    app, _ = app_parts
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://funnel.test") as c:
        yield c


# ---- discovery --------------------------------------------------------- #

async def test_index_lists_endpoints(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "/snapshot" in r.json()["endpoints"]


async def test_livez_is_up_regardless_of_data(client):
    """The container healthcheck probes this, so it must not depend on sensors
    being switched on."""
    r = await client.get("/livez")
    assert r.status_code == 200
    assert r.json()["alive"] is True


async def test_rowers_discovered_from_data(client):
    r = await client.get("/rowers")
    assert r.status_code == 200
    seats = [x["seat"] for x in r.json()]
    assert seats == [3, 7]
    assert r.json()[0]["up"] is True        # from the retained status topic


async def test_streams_reports_counters(client):
    r = await client.get("/streams")
    paths = {x["path"]: x for x in r.json()}
    assert set(paths) == {"boat/imu", "rower/3/seat", "rower/7/seat"}
    assert paths["rower/3/seat"]["n_recv"] == 1
    assert paths["rower/3/seat"]["last_seq"] == 412
    assert paths["rower/3/seat"]["stale"] is False


# ---- reads ------------------------------------------------------------- #

async def test_rower_stream_returns_the_values(client):
    r = await client.get("/rower/3/seat")
    assert r.status_code == 200
    assert r.json()["values"] == {"d_mm": 812}
    assert r.headers["x-funnel-status"] == "ok"
    assert r.headers["cache-control"] == "no-store"
    assert "x-funnel-age-ms" in r.headers
    assert r.headers["x-funnel-tick"] == "1"


async def test_rower_returns_all_its_streams(client):
    r = await client.get("/rower/3")
    assert r.status_code == 200
    assert r.json()["seat"] == 3
    assert "seat" in r.json()["streams"]


async def test_boat_namespace_holds_non_rower_data(client):
    r = await client.get("/boat/imu")
    assert r.status_code == 200
    assert r.json()["values"] == {"pitch_deg": 2.5}

    r = await client.get("/boat")
    assert r.status_code == 200
    assert "imu" in r.json()["streams"]


async def test_snapshot_is_one_request_for_everything(client):
    r = await client.get("/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["tick"] == 1
    assert set(body["rowers"]) == {"3", "7"}        # JSON keys are strings
    assert body["rowers"]["3"]["seat"]["values"] == {"d_mm": 812}
    assert body["boat"]["imu"]["values"] == {"pitch_deg": 2.5}
    assert body["presence"]["3"]["up"] is True


async def test_position_is_the_derived_endpoint(client):
    """The app's seat endpoint. It must exist and be shaped as the contract
    says even though the transformation behind it is still a no-op."""
    r = await client.get("/rower/3/position")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"position", "travel_mm", "velocity_mms", "t",
                         "age_ms", "stale", "n_in", "calibrated",
                         "ready", "pending"}
    assert body["ready"] is False        # stages are identity no-ops
    assert body["pending"]
    assert body["stale"] is False
    assert r.headers["x-funnel-status"] == "ok"


async def test_position_does_not_leak_the_raw_reading_as_a_position(client):
    """The whole point of the endpoint: an unprocessed sensor value must not
    be presented as a normalised seat position."""
    raw = (await client.get("/rower/3/seat")).json()["values"]["d_mm"]
    pos = (await client.get("/rower/3/position")).json()
    assert pos["position"] is None
    assert pos["position"] != raw


async def test_position_appears_in_the_snapshot_beside_the_raw_stream(client):
    body = (await client.get("/snapshot")).json()
    seat3 = body["rowers"]["3"]
    assert "position" in seat3          # derived, for display
    assert "seat" in seat3              # raw, for debugging


async def test_position_absent_for_a_seat_with_no_seat_stream(client,
                                                              app_parts):
    """A seat publishing only, say, a force stream has no seat position, and
    that is a 404 rather than a null-filled block."""
    _, parts = app_parts
    push(parts["store"], StreamKey("rower", 4, "force"), n=231)
    parts["loop"].tick_once()
    assert (await client.get("/rower/4")).status_code == 200
    assert (await client.get("/rower/4/position")).status_code == 404


async def test_position_404_for_unknown_seat(client):
    assert (await client.get("/rower/99/position")).status_code == 404


async def test_pipeline_status_is_visible_in_health(client):
    stages = (await client.get("/health")).json()["pipelines"]["seat_position"]
    assert stages
    assert all(v == "identity" for v in stages.values())


async def test_raw_is_a_slice_of_the_buffer(client):
    r = await client.get("/rower/3/raw?window_s=5")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "rower/3/seat"
    assert body["n"] == 1
    assert body["samples"][0]["values"] == {"d_mm": 812}


async def test_raw_window_is_capped(client):
    """A debug endpoint still must not let a caller ask for unbounded work."""
    assert (await client.get("/rower/3/raw?window_s=600")).status_code == 422


# ---- missing and stale ------------------------------------------------- #

async def test_unknown_seat_is_404(client):
    r = await client.get("/rower/5")
    assert r.status_code == 404


async def test_unknown_stream_on_known_seat_is_404(client):
    r = await client.get("/rower/3/nosuch")
    assert r.status_code == 404


async def test_unknown_boat_stream_is_404(client):
    assert (await client.get("/boat/nosuch")).status_code == 404


async def test_stale_data_is_503_not_a_stale_200():
    """The central convention: a reading that stopped updating must not be
    served as current, because it is indistinguishable from a real one."""
    app = create_app(make_settings(max_age_ms=500.0))
    parts = app.state.parts
    push(parts["store"], StreamKey("rower", 2, "seat"), age_s=5.0, d_mm=700)
    parts["loop"].tick_once()

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://funnel.test") as c:
        r = await c.get("/rower/2/seat")
        assert r.status_code == 503
        assert r.headers["x-funnel-status"] == "stale"
        assert r.json() == {}

        # The derived endpoint follows the same rule -- a position computed
        # from samples that stopped arriving is not a current position.
        r = await c.get("/rower/2/position")
        assert r.status_code == 503
        assert r.headers["x-funnel-status"] == "stale"

        # ... but the dashboard's single poll still succeeds, flagging it.
        r = await c.get("/snapshot")
        assert r.status_code == 200
        assert r.json()["rowers"]["2"]["seat"]["stale"] is True
        assert r.json()["rowers"]["2"]["position"]["stale"] is True


async def test_health_is_503_when_nothing_is_fresh():
    app = create_app(make_settings(max_age_ms=500.0))
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://funnel.test") as c:
        r = await c.get("/health")
        assert r.status_code == 503
        assert r.json()["status"] == "no-data"


async def test_health_is_200_with_fresh_data(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["tick"]["hz"] == 10.0
    assert body["recording"] is None        # off by default
    assert "mqtt" in body["sources"]


# ---- the decoupling requirement ---------------------------------------- #

async def test_requests_do_not_advance_the_tick(client, app_parts):
    """Nothing is computed per request: a client polling fast must not change
    what the service does."""
    _, parts = app_parts
    before = parts["loop"].n_ticks
    for _ in range(50):
        await client.get("/snapshot")
    assert parts["loop"].n_ticks == before


async def test_reads_are_consistent_within_one_tick(client):
    """Two reads between ticks must agree, because both see the same frozen
    snapshot rather than a re-derived one."""
    a = (await client.get("/snapshot")).json()
    b = (await client.get("/snapshot")).json()
    assert a["tick"] == b["tick"]
    assert a["rowers"] == b["rowers"]


async def test_scaling_needs_no_configuration(client, app_parts):
    """Seats appear because something published. There is no crew size to set."""
    _, parts = app_parts
    for seat in range(1, 17):
        push(parts["store"], StreamKey("rower", seat, "seat"), d_mm=500 + seat)
    parts["loop"].tick_once()
    r = await client.get("/rowers")
    assert [x["seat"] for x in r.json()] == list(range(1, 17))


async def test_before_the_first_tick_nothing_pretends_to_have_data():
    app = create_app(make_settings())
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://funnel.test") as c:
        r = await c.get("/snapshot")
        assert r.status_code == 200
        assert r.json()["tick"] == 0
        assert r.headers["x-funnel-status"] == "starting"
        assert (await c.get("/rower/1/seat")).status_code == 404
