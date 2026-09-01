# tests/test_router.py
import pytest
from gpschanger.router import Route, RouterError, foot_route, BASE_URL, USER_AGENT


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


OK_PAYLOAD = {
    "code": "Ok",
    "routes": [{
        "geometry": {"type": "LineString",
                     "coordinates": [[-122.0302, 37.3317], [-122.0301, 37.3327]]},
        "distance": 111.2,
        "duration": 89.0,
    }],
    "waypoints": [{"distance": 0.3}, {"distance": 0.5}],
}


def test_uses_the_foot_profile_endpoint_not_the_car_only_demo_server():
    """router.project-osrm.org silently returns car routing for foot profiles."""
    assert "routing.openstreetmap.de" in BASE_URL
    assert "routed-foot" in BASE_URL
    assert "project-osrm.org" not in BASE_URL


def test_sends_coordinates_as_lon_lat_not_lat_lon():
    seen = {}
    def fake_get(url, **kw):
        seen["url"] = url
        seen["kw"] = kw
        return FakeResponse(OK_PAYLOAD)
    foot_route((37.3317, -122.0302), (37.3327, -122.0301), get=fake_get)
    # lon comes first in each pair
    assert "-122.0302,37.3317;-122.0301,37.3327" in seen["url"]


def test_requests_full_geojson_geometry():
    seen = {}
    def fake_get(url, **kw):
        seen.update(kw)
        return FakeResponse(OK_PAYLOAD)
    foot_route((37.3317, -122.0302), (37.3327, -122.0301), get=fake_get)
    assert seen["params"]["geometries"] == "geojson"
    assert seen["params"]["overview"] == "full"


def test_sends_an_identifying_user_agent():
    seen = {}
    def fake_get(url, **kw):
        seen.update(kw)
        return FakeResponse(OK_PAYLOAD)
    foot_route((37.3317, -122.0302), (37.3327, -122.0301), get=fake_get)
    assert "gpschanger" in seen["headers"]["User-Agent"]


def test_flips_response_coordinates_back_to_lat_lon():
    r = foot_route((37.3317, -122.0302), (37.3327, -122.0301),
                   get=lambda url, **kw: FakeResponse(OK_PAYLOAD))
    assert r.points[0] == (37.3317, -122.0302)
    assert r.distance_m == pytest.approx(111.2)
    assert r.duration_s == pytest.approx(89.0)


def test_raises_on_osrm_error_code_despite_http_200():
    """OSRM returns NoRoute with an OK-looking HTTP status."""
    payload = {"code": "NoRoute", "message": "no route found"}
    with pytest.raises(RouterError, match="NoRoute"):
        foot_route((37.3, -122.0), (0.0, 0.0),
                   get=lambda url, **kw: FakeResponse(payload))


def test_raises_when_no_routes_returned():
    with pytest.raises(RouterError):
        foot_route((37.3, -122.0), (37.4, -122.0),
                   get=lambda url, **kw: FakeResponse({"code": "Ok", "routes": []}))


# --- regressions from real-device runs -------------------------

def test_an_http_error_becomes_a_RouterError():
    """A 400 from OSRM must not escape as a raw requests exception.

    server.py turns RouterError into a JSON 502; anything else becomes
    Flask's HTML 500, and the browser then dies on "Unexpected token '<',
    "<!doctype "... is not valid JSON".
    """
    import requests

    def fake_get(url, **kw):
        raise requests.HTTPError("400 Client Error: Bad Request")

    with pytest.raises(RouterError):
        foot_route((37.0, -122.0), (37.1, -122.1), get=fake_get)


def test_a_failing_raise_for_status_becomes_a_RouterError():
    import requests

    class Failing(FakeResponse):
        def raise_for_status(self):
            raise requests.HTTPError("400 Client Error: Bad Request")

    with pytest.raises(RouterError):
        foot_route((37.0, -122.0), (37.1, -122.1),
                   get=lambda url, **kw: Failing(OK_PAYLOAD))


def test_a_connection_failure_becomes_a_RouterError():
    import requests

    def fake_get(url, **kw):
        raise requests.ConnectionError("name resolution failed")

    with pytest.raises(RouterError):
        foot_route((37.0, -122.0), (37.1, -122.1), get=fake_get)


# --- driving mode (added later) ------------------------

def test_walk_mode_uses_the_foot_instance_and_foot_profile():
    from gpschanger.router import route_between
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return FakeResponse(OK_PAYLOAD)

    route_between((37.0, -122.0), (37.1, -122.1), mode="walk", get=fake_get)
    assert "routed-foot" in seen["url"]
    assert "/route/v1/foot/" in seen["url"]


def test_drive_mode_uses_the_car_instance_and_driving_profile():
    """FOSSGIS splits by instance: routed-car serves the driving profile."""
    from gpschanger.router import route_between
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return FakeResponse(OK_PAYLOAD)

    route_between((37.0, -122.0), (37.1, -122.1), mode="drive", get=fake_get)
    assert "routed-car" in seen["url"]
    assert "/route/v1/driving/" in seen["url"]
    assert "routed-foot" not in seen["url"]


def test_drive_mode_still_sends_lon_lat():
    from gpschanger.router import route_between
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return FakeResponse(OK_PAYLOAD)

    route_between((37.3317, -122.0302), (37.3337, -122.0301), mode="drive", get=fake_get)
    assert "-122.0302,37.3317;-122.0301,37.3337" in seen["url"]


def test_an_unknown_mode_is_rejected():
    from gpschanger.router import route_between
    with pytest.raises(ValueError):
        route_between((37.0, -122.0), (37.1, -122.1), mode="teleport",
                      get=lambda url, **kw: FakeResponse(OK_PAYLOAD))


def test_foot_route_still_works_and_means_walk():
    from gpschanger.router import route_between  # noqa: F401
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return FakeResponse(OK_PAYLOAD)

    foot_route((37.0, -122.0), (37.1, -122.1), get=fake_get)
    assert "routed-foot" in seen["url"]


# --- route detail for traffic-aware drive pacing -----------------------------

DETAIL_PAYLOAD = {
    "code": "Ok",
    "routes": [{
        "geometry": {"type": "LineString",
                     "coordinates": [[-122.0302, 37.3317], [-122.0301, 37.3327]]},
        "distance": 111.2,
        "duration": 89.0,
        "legs": [{
            "annotation": {"nodes": [111, 222, 333]},
            "steps": [
                {"maneuver": {"type": "depart", "location": [-122.0302, 37.3317]}},
                {"maneuver": {"type": "turn", "modifier": "left",
                              "location": [-122.0301, 37.3320]}},
                {"maneuver": {"type": "new name", "modifier": "straight",
                              "location": [-122.0301, 37.3323]}},
                {"maneuver": {"type": "arrive", "location": [-122.0301, 37.3327]}},
            ],
        }],
    }],
}


def test_drive_mode_asks_for_nodes_and_steps():
    """Both are needed to place traffic stops: nodes identify the controls,
    steps give the turns."""
    from gpschanger.router import route_between
    seen = {}

    def fake_get(url, **kw):
        seen["params"] = kw["params"]
        return FakeResponse(DETAIL_PAYLOAD)

    route_between((37.0, -122.0), (37.1, -122.1), mode="drive", get=fake_get)
    assert seen["params"]["annotations"] == "nodes"
    assert seen["params"]["steps"] == "true"


def test_walk_mode_does_not_ask_for_them():
    from gpschanger.router import route_between
    seen = {}

    def fake_get(url, **kw):
        seen["params"] = kw["params"]
        return FakeResponse(OK_PAYLOAD)

    route_between((37.0, -122.0), (37.1, -122.1), mode="walk", get=fake_get)
    assert seen["params"].get("annotations") is None
    assert seen["params"]["steps"] == "false"


def test_drive_mode_returns_the_traversed_node_ids():
    from gpschanger.router import route_between
    route = route_between((37.0, -122.0), (37.1, -122.1), mode="drive",
                          get=lambda url, **kw: FakeResponse(DETAIL_PAYLOAD))
    assert list(route.nodes) == [111, 222, 333]


def test_turn_locations_come_back_as_lat_lon_and_exclude_non_turns():
    from gpschanger.router import route_between
    route = route_between((37.0, -122.0), (37.1, -122.1), mode="drive",
                          get=lambda url, **kw: FakeResponse(DETAIL_PAYLOAD))
    # depart, arrive and a straight "new name" are not turns.
    assert len(route.turns) == 1
    assert route.turns[0] == (pytest.approx(37.3320), pytest.approx(-122.0301))


def test_a_route_without_detail_still_has_empty_nodes_and_turns():
    route = foot_route((37.0, -122.0), (37.1, -122.1),
                       get=lambda url, **kw: FakeResponse(OK_PAYLOAD))
    assert route.nodes == ()
    assert route.turns == ()


def test_slight_turns_are_not_treated_as_turns():
    """A slight left is a lane-width curve; you do not lift off for it."""
    from gpschanger.router import route_between
    payload = {
        "code": "Ok",
        "routes": [{
            "geometry": {"type": "LineString",
                         "coordinates": [[-122.03, 37.33], [-122.03, 37.34]]},
            "distance": 111.2, "duration": 89.0,
            "legs": [{"annotation": {"nodes": [1]}, "steps": [
                {"maneuver": {"type": "turn", "modifier": "slight right",
                              "location": [-122.03, 37.331]}},
                {"maneuver": {"type": "turn", "modifier": "slight left",
                              "location": [-122.03, 37.332]}},
                {"maneuver": {"type": "turn", "modifier": "sharp left",
                              "location": [-122.03, 37.333]}},
            ]}],
        }],
    }
    route = route_between((37.0, -122.0), (37.1, -122.1), mode="drive",
                          get=lambda url, **kw: FakeResponse(payload))
    assert len(route.turns) == 1        # only the sharp left
