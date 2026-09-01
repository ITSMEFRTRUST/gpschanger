"""Tests for place search."""
import pytest

from gpschanger.geocode import GeocodeError, Place, search


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


PAYLOAD = {
    "features": [
        {"geometry": {"coordinates": [-0.1585, 51.5237], "type": "Point"},
         "properties": {"name": "Baker Street", "city": "London",
                        "state": "England", "country": "United Kingdom",
                        "osm_key": "highway", "osm_value": "residential"}},
        {"geometry": {"coordinates": [-0.1620, 51.5190], "type": "Point"},
         "properties": {"name": "Baker Street", "city": "London",
                        "state": "England", "country": "United Kingdom"}},
    ]
}


def test_returns_places_with_lat_lon_the_right_way_round():
    """The API speaks lon,lat; everything downstream speaks lat,lon."""
    places = search("main street", get=lambda url, **kw: FakeResponse(PAYLOAD))
    assert isinstance(places[0], Place)
    assert places[0].lat == pytest.approx(51.5237)
    assert places[0].lon == pytest.approx(-0.1585)


def test_label_includes_the_place_and_its_context():
    places = search("main street", get=lambda url, **kw: FakeResponse(PAYLOAD))
    assert places[0].name == "Baker Street"
    assert "London" in places[0].label
    assert "England" in places[0].label


def test_sends_the_query_and_a_limit():
    seen = {}

    def fake_get(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return FakeResponse(PAYLOAD)

    search("baker street london", limit=4, get=fake_get)
    assert seen["kw"]["params"]["q"] == "baker street london"
    assert seen["kw"]["params"]["limit"] == 4
    assert "User-Agent" in seen["kw"]["headers"]


def test_biases_results_towards_a_point_when_given_one():
    """Searching "main street" from London should not rank Tokyo first."""
    seen = {}

    def fake_get(url, **kw):
        seen["params"] = kw["params"]
        return FakeResponse(PAYLOAD)

    search("main street", near=(51.507, -0.128), get=fake_get)
    assert seen["params"]["lat"] == pytest.approx(51.507)
    assert seen["params"]["lon"] == pytest.approx(-0.128)


def test_omits_the_bias_when_not_given_one():
    seen = {}

    def fake_get(url, **kw):
        seen["params"] = kw["params"]
        return FakeResponse(PAYLOAD)

    search("main street", get=fake_get)
    assert "lat" not in seen["params"]


def test_an_empty_query_is_rejected_without_calling_out():
    called = []

    def fake_get(url, **kw):
        called.append(url)
        return FakeResponse(PAYLOAD)

    with pytest.raises(ValueError):
        search("   ", get=fake_get)
    assert not called


def test_a_transport_failure_becomes_a_GeocodeError():
    import requests

    def boom(url, **kw):
        raise requests.ConnectionError("down")

    with pytest.raises(GeocodeError):
        search("main street", get=boom)


def test_no_results_is_an_empty_list_not_an_error():
    places = search("zzzz", get=lambda url, **kw: FakeResponse({"features": []}))
    assert places == []


def test_features_without_geometry_are_skipped():
    bad = {"features": [{"properties": {"name": "Broken"}}, PAYLOAD["features"][0]]}
    places = search("x", get=lambda url, **kw: FakeResponse(bad))
    assert len(places) == 1
    assert places[0].name == "Baker Street"
