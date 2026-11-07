"""The control API, in-process, with no sockets: the taps are left out and
items are handed to the session manager directly."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recorder import session as session_mod
from recorder.api import PREFIX, create_app
from recorder.format import MqttMsg

from .helpers import settings


@pytest.fixture
def app(tmp_path):
    return create_app(settings(tmp_path), start_taps=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def test_every_route_is_under_the_prefix(app):
    """The funnel serves /, /health, /livez, /snapshot ... on 8000. Nothing
    here may shadow those, even behind one proxy."""
    paths = list(app.openapi()["paths"])
    assert paths and all(p.startswith(PREFIX) for p in paths)
    assert app.docs_url == f"{PREFIX}/docs"
    assert app.openapi_url.startswith(PREFIX)


def test_nothing_is_served_outside_the_prefix(client):
    for path in ("/", "/livez", "/health", "/status", "/docs"):
        assert client.get(path).status_code == 404


def test_start_status_stop(client, app):
    assert client.get(f"{PREFIX}/livez").json() == {"ok": True, "recording": False}
    st = client.get(f"{PREFIX}/status").json()
    assert st["recording"] is False and st["session"] is None

    r = client.post(f"{PREFIX}/start", params={"label": "drill"})
    assert r.status_code == 200
    name = r.json()["session"]
    assert name.endswith("_drill")

    app.state.manager.offer(MqttMsg(1, 1.0, "rower/1/seat", 0, False, b"{}"))
    st = client.get(f"{PREFIX}/status").json()
    assert st["recording"] is True and st["session"]["name"] == name

    r = client.post(f"{PREFIX}/stop")
    assert r.status_code == 200
    assert r.json()["session"] == name
    assert r.json()["written"] == {"mqtt": 1}

    sessions = client.get(f"{PREFIX}/sessions").json()
    assert [s["name"] for s in sessions] == [name]
    assert sessions[0]["complete"] is True


def test_conflicts_are_409(client):
    assert client.post(f"{PREFIX}/stop").status_code == 409
    assert client.post(f"{PREFIX}/start").status_code == 200
    r = client.post(f"{PREFIX}/start")
    assert r.status_code == 409 and r.json()["error"] == "already recording"
    assert client.post(f"{PREFIX}/stop").status_code == 200


def test_low_disk_is_507(client, monkeypatch, app):
    app.state.manager.s.min_free_gb = 1.0
    monkeypatch.setattr(session_mod, "disk_free", lambda _p: 1000)
    r = client.post(f"{PREFIX}/start")
    assert r.status_code == 507


def test_shutdown_finishes_a_running_session(tmp_path):
    app = create_app(settings(tmp_path), start_taps=False)
    with TestClient(app) as c:
        name = c.post(f"{PREFIX}/start").json()["session"]
    # Leaving the context runs the lifespan shutdown, as SIGTERM does.
    from recorder.format import read_meta
    meta = read_meta(tmp_path / name)
    assert meta["complete"] is True and meta["stop_reason"] == "shutdown"
