"""Tests for the Flask backend.

Amended alongside the driver rewrite: the backend owns a LocationDriver, so
/api/play hands it the planned points by route_id rather than handing a child
process a file path. That removes the old path-traversal surface entirely --
an unknown id is simply not in the map -- so the traversal test is replaced by
an unknown-id test.
"""
import pytest

from gpschanger.router import Route, RouterError
from gpschanger.server import create_app

ROUTE = Route([(37.3317, -122.0302), (37.3327, -122.0302), (37.3337, -122.0302)],
              222.4, 178.0)


class FakeDriver:
    def __init__(self):
        self.started = None
        self.stopped = False
        self.mounted = False
        self._state = "idle"

    def auto_mount(self):
        self.mounted = True

    def start(self, points):
        self.started = list(points)
        self._state = "walking"

    def stop(self):
        self.stopped = True
        self._state = "idle"

    def status(self):
        return {
            "state": self._state,
            "index": 0,
            "total": len(self.started or []),
            "lat": None,
            "lon": None,
            "error": None,
        }


@pytest.fixture
def client(monkeypatch, tmp_path):
    import gpschanger.server as srv
    monkeypatch.setattr(srv, "route_between", lambda a, b, mode="walk": ROUTE)
    monkeypatch.setattr(srv, "ROUTE_DIR", str(tmp_path))
    driver = FakeDriver()
    app = create_app(driver=driver)
    app.config.update(TESTING=True)
    c = app.test_client()
    c.driver = driver
    return c


BODY = {"a": [37.3317, -122.0302], "b": [37.3337, -122.0302],
        "speed_min_kmh": 4.0, "speed_max_kmh": 6.0}


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["Content-Type"]


def test_route_returns_coords_and_writes_a_gpx(client):
    r = client.post("/api/route", json=BODY)
    assert r.status_code == 200
    data = r.get_json()
    assert data["points"] > 2
    assert len(data["coords"]) == data["points"]
    assert data["gpx_path"].endswith(".gpx")
    assert data["route_id"]


def test_returned_coords_come_from_the_parsed_gpx(client):
    """What is drawn on screen must be what the phone will walk."""
    import gpxpy
    data = client.post("/api/route", json=BODY).get_json()
    parsed = gpxpy.parse(open(data["gpx_path"]).read())
    pts = [p for t in parsed.tracks for s in t.segments for p in s.points]
    assert [[p.latitude, p.longitude] for p in pts] == data["coords"]


def test_route_rejects_an_inverted_speed_band(client):
    bad = BODY | {"speed_min_kmh": 6.0, "speed_max_kmh": 4.0}
    r = client.post("/api/route", json=bad)
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_route_rejects_a_malformed_body(client):
    r = client.post("/api/route", json={"a": [37.0], "b": [37.1, -122.0]})
    assert r.status_code == 400


def test_route_reports_router_failure_as_502(client, monkeypatch):
    import gpschanger.server as srv

    def boom(a, b, mode="walk"):
        raise RouterError("NoRoute")

    monkeypatch.setattr(srv, "route_between", boom)
    r = client.post("/api/route", json=BODY)
    assert r.status_code == 502


def test_play_starts_the_driver_with_the_planned_points(client):
    data = client.post("/api/route", json=BODY).get_json()
    r = client.post("/api/play", json={"route_id": data["route_id"]})
    assert r.status_code == 200
    assert client.driver.started is not None
    assert len(client.driver.started) == data["points"]
    first = client.driver.started[0]
    assert [first.lat, first.lon] == data["coords"][0]


def test_play_mounts_the_developer_disk_image_first(client):
    """auto-mount must be re-run after every phone reboot, so do it on play."""
    data = client.post("/api/route", json=BODY).get_json()
    client.post("/api/play", json={"route_id": data["route_id"]})
    assert client.driver.mounted


def test_play_rejects_an_unknown_route_id(client):
    r = client.post("/api/play", json={"route_id": "nope"})
    assert r.status_code == 404


def test_stop_stops_the_driver(client):
    assert client.post("/api/stop").status_code == 200
    assert client.driver.stopped


def test_status_reports_driver_state(client):
    s = client.get("/api/status").get_json()
    assert s["state"] == "idle"
    assert s["running"] is False

    data = client.post("/api/route", json=BODY).get_json()
    client.post("/api/play", json={"route_id": data["route_id"]})

    s = client.get("/api/status").get_json()
    assert s["state"] == "walking"
    assert s["running"] is True


# --- regressions from real-device runs -------------------------

def test_route_normalises_a_wrapped_longitude(client, monkeypatch):
    """Leaflet hands back longitudes beyond +/-180 once the user pans across a
    world copy. The real run sent lon beyond +/-180 (a full turn off) and OSRM
    answered 400.
    """
    import gpschanger.server as srv
    seen = {}

    def spy(a, b, mode="walk"):
        seen["a"], seen["b"] = a, b
        return ROUTE

    monkeypatch.setattr(srv, "route_between", spy)
    body = BODY | {"a": [51.5074, -360.1278], "b": [51.5090, -360.1290]}
    r = client.post("/api/route", json=body)

    assert r.status_code == 200
    assert seen["a"][1] == pytest.approx(-0.1278)
    assert seen["b"][1] == pytest.approx(-0.1290)
    assert seen["a"][0] == pytest.approx(51.5074)


def test_route_normalises_a_longitude_wrapped_the_other_way(client, monkeypatch):
    import gpschanger.server as srv
    seen = {}
    monkeypatch.setattr(srv, "route_between",
                        lambda a, b, mode="walk": (seen.update(a=a), ROUTE)[1])
    client.post("/api/route", json=BODY | {"a": [51.5, 359.872]})
    assert seen["a"][1] == pytest.approx(-0.128)


def test_route_rejects_an_impossible_latitude(client):
    r = client.post("/api/route", json=BODY | {"a": [95.0, -0.128]})
    assert r.status_code == 400
    assert "latitude" in r.get_json()["error"]


# --- driving mode (added later) ------------------------

def _spy_router(monkeypatch, seen):
    import gpschanger.server as srv

    def spy(a, b, mode="walk"):
        seen["mode"] = mode
        return ROUTE

    monkeypatch.setattr(srv, "route_between", spy)


def test_route_defaults_to_walk_mode(client, monkeypatch):
    seen = {}
    _spy_router(monkeypatch, seen)
    client.post("/api/route", json=BODY)
    assert seen["mode"] == "walk"


def test_route_passes_drive_mode_through(client, monkeypatch):
    seen = {}
    _spy_router(monkeypatch, seen)
    r = client.post("/api/route", json=BODY | {"mode": "drive",
                                               "speed_min_kmh": 40.0,
                                               "speed_max_kmh": 90.0})
    assert r.status_code == 200
    assert seen["mode"] == "drive"


def test_route_rejects_an_unknown_mode(client):
    r = client.post("/api/route", json=BODY | {"mode": "teleport"})
    assert r.status_code == 400
    assert "mode" in r.get_json()["error"]


def test_drive_mode_defaults_to_car_speeds(client):
    """Omitting the band in drive mode must not silently walk the motorway."""
    r = client.post("/api/route", json={"a": BODY["a"], "b": BODY["b"], "mode": "drive"})
    assert r.status_code == 200
    d = r.get_json()
    # ROUTE is 222.4 m; at 40-90 km/h that is well under a minute.
    assert d["duration_s"] < 60


def test_walk_mode_defaults_to_walking_speeds(client):
    r = client.post("/api/route", json={"a": BODY["a"], "b": BODY["b"]})
    assert r.status_code == 200
    d = r.get_json()
    # 222.4 m at 4-6 km/h is roughly 2.2-3.3 minutes.
    assert 100 < d["duration_s"] < 260


def test_the_response_echoes_the_mode(client):
    d = client.post("/api/route", json=BODY | {"mode": "drive"}).get_json()
    assert d["mode"] == "drive"


# --- search + traffic stops (added later) --------------

from gpschanger.geocode import GeocodeError, Place          # noqa: E402
from gpschanger.traffic import Control, TrafficError        # noqa: E402

PLACES = [Place("Baker Street", "Baker Street, Marylebone, London", 51.5237, -0.1585)]

DRIVE_ROUTE = Route(
    [(51.5 + i * 0.0002, -0.128) for i in range(40)],
    800.0, 60.0,
    nodes=(111, 222, 333),
    turns=((51.5 + 20 * 0.0002, -0.128),),
)


def test_search_returns_results(client, monkeypatch):
    import gpschanger.server as srv
    monkeypatch.setattr(srv, "search", lambda q, near=None: PLACES)
    r = client.get("/api/search?q=main+street")
    assert r.status_code == 200
    got = r.get_json()["results"]
    assert got[0]["name"] == "Baker Street"
    assert got[0]["lat"] == pytest.approx(51.5237)


def test_search_biases_towards_the_map_centre(client, monkeypatch):
    import gpschanger.server as srv
    seen = {}

    def spy(q, near=None):
        seen["q"], seen["near"] = q, near
        return PLACES

    monkeypatch.setattr(srv, "search", spy)
    client.get("/api/search?q=main&lat=51.507&lon=-0.128")
    assert seen["near"] == (pytest.approx(51.507), pytest.approx(-0.128))


def test_search_rejects_an_empty_query(client):
    assert client.get("/api/search?q=").status_code == 400


def test_search_reports_a_geocoder_failure_as_502(client, monkeypatch):
    import gpschanger.server as srv

    def boom(q, near=None):
        raise GeocodeError("down")

    monkeypatch.setattr(srv, "search", boom)
    assert client.get("/api/search?q=main").status_code == 502


def test_drive_mode_looks_up_traffic_controls_for_the_route_nodes(client, monkeypatch):
    import gpschanger.server as srv
    seen = {}

    monkeypatch.setattr(srv, "route_between", lambda a, b, mode="walk": DRIVE_ROUTE)

    def spy(nodes):
        seen["nodes"] = list(nodes)
        return [Control(222, 51.5 + 10 * 0.0002, -0.128, "traffic_signals")]

    monkeypatch.setattr(srv, "traffic_controls", spy)
    r = client.post("/api/route", json=BODY | {"mode": "drive"})
    assert r.status_code == 200
    assert seen["nodes"] == [111, 222, 333]


def test_drive_mode_reports_how_many_stops_it_planned(client, monkeypatch):
    """Signals are a dice roll, so assert the wiring, not a particular outcome:
    every planned stop must be reported, and a route with no controls has none."""
    import gpschanger.server as srv
    monkeypatch.setattr(srv, "route_between", lambda a, b, mode="walk": DRIVE_ROUTE)

    lights = [Control(200 + i, 51.5 + i * 0.0002, -0.128, "traffic_signals")
              for i in range(5, 35)]
    monkeypatch.setattr(srv, "traffic_controls", lambda nodes: lights)
    d = client.post("/api/route", json=BODY | {"mode": "drive"}).get_json()
    assert d["stops"] >= 1, "30 lights and not one caught us"

    monkeypatch.setattr(srv, "traffic_controls", lambda nodes: [])
    d = client.post("/api/route", json=BODY | {"mode": "drive"}).get_json()
    assert d["stops"] == 0


def test_walk_mode_does_no_traffic_lookup(client, monkeypatch):
    import gpschanger.server as srv
    called = []
    monkeypatch.setattr(srv, "traffic_controls", lambda nodes: called.append(1) or [])
    client.post("/api/route", json=BODY)
    assert not called


def test_a_traffic_lookup_failure_does_not_fail_the_route(client, monkeypatch):
    """Overpass is a free shared service; a route without stops beats no route."""
    import gpschanger.server as srv
    monkeypatch.setattr(srv, "route_between", lambda a, b, mode="walk": DRIVE_ROUTE)

    def boom(nodes):
        raise TrafficError("overpass timed out")

    monkeypatch.setattr(srv, "traffic_controls", boom)
    r = client.post("/api/route", json=BODY | {"mode": "drive"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["points"] > 0
    assert d["stops"] == 0
    assert "traffic" in d["note"].lower()
