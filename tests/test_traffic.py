"""Tests for the Overpass traffic-control lookup."""
import pytest

from gpschanger.traffic import Control, TrafficError, traffic_controls


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


def elements(*specs):
    return {"elements": [
        {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": {"highway": kind}}
        for i, lat, lon, kind in specs
    ]}


PAYLOAD = elements(
    (10000001, 51.5241, -0.1580, "traffic_signals"),
    (10000002, 51.5233, -0.1602, "stop"),
    (10000003, 51.5248, -0.1571, "crossing"),
)


def test_parses_controls_with_kind_and_position():
    got = traffic_controls([1, 2, 3], post=lambda url, **kw: FakeResponse(PAYLOAD))
    assert all(isinstance(c, Control) for c in got)
    kinds = {c.kind for c in got}
    assert kinds == {"traffic_signals", "stop", "crossing"}
    first = next(c for c in got if c.kind == "traffic_signals")
    assert first.lat == pytest.approx(51.5241)
    assert first.lon == pytest.approx(-0.1580)


def test_asks_only_about_the_nodes_on_the_route():
    seen = {}

    def fake_post(url, **kw):
        seen["data"] = kw["data"]["data"]
        return FakeResponse(PAYLOAD)

    traffic_controls([111, 222, 333], post=fake_post)
    assert "node(id:111,222,333)" in seen["data"].replace(" ", "")
    assert "traffic_signals" in seen["data"]


def test_an_empty_node_list_makes_no_request():
    called = []
    assert traffic_controls([], post=lambda url, **kw: called.append(1)) == []
    assert not called


def test_long_node_lists_are_chunked_and_merged():
    """A 70 km route annotates thousands of nodes; one request would be huge."""
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["data"]["data"])
        n = len(calls)
        return FakeResponse(elements((n, 51.5 + n, -0.128, "stop")))

    got = traffic_controls(list(range(2500)), chunk=1000, post=fake_post)
    assert len(calls) == 3
    assert len(got) == 3


def test_duplicate_nodes_across_chunks_are_not_double_counted():
    def fake_post(url, **kw):
        return FakeResponse(PAYLOAD)

    got = traffic_controls(list(range(2500)), chunk=1000, post=fake_post)
    assert len({c.node_id for c in got}) == len(got) == 3


def test_a_transport_failure_becomes_a_TrafficError():
    import requests

    def boom(url, **kw):
        raise requests.ConnectionError("overpass down")

    with pytest.raises(TrafficError):
        traffic_controls([1], post=boom)


def test_elements_missing_coordinates_are_skipped():
    payload = {"elements": [
        {"type": "node", "id": 1, "tags": {"highway": "stop"}},
        {"type": "node", "id": 2, "lat": 51.5, "lon": -0.128, "tags": {"highway": "stop"}},
    ]}
    got = traffic_controls([1, 2], post=lambda url, **kw: FakeResponse(payload))
    assert [c.node_id for c in got] == [2]


def test_unknown_highway_values_are_skipped():
    payload = {"elements": [
        {"type": "node", "id": 3, "lat": 51.5, "lon": -0.128, "tags": {"highway": "bus_stop"}},
    ]}
    assert traffic_controls([3], post=lambda url, **kw: FakeResponse(payload)) == []
