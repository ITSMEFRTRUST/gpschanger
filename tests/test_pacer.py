# tests/test_pacer.py
import datetime as dt
import random
import pytest
from gpschanger.pacer import TrackPoint, haversine, plan_walk

# ~1.1 km of straight-ish road, 4 waypoints
ROUTE = [(37.3317, -122.0302), (37.3327, -122.0302),
         (37.3337, -122.0302), (37.3347, -122.0302)]
START = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_haversine_known_distance():
    # 0.001 degrees of latitude is ~111.2 m anywhere on Earth
    d = haversine((37.0, -122.0), (37.001, -122.0))
    assert 110.0 < d < 112.0


def test_returns_points_starting_at_route_start():
    pts = plan_walk(ROUTE, 4.0, 6.0, start=START, rng=random.Random(42))
    assert pts[0].lat == pytest.approx(ROUTE[0][0])
    assert pts[0].lon == pytest.approx(ROUTE[0][1])


def test_ends_at_route_end():
    pts = plan_walk(ROUTE, 4.0, 6.0, start=START, rng=random.Random(42))
    assert pts[-1].lat == pytest.approx(ROUTE[-1][0], abs=1e-6)
    assert pts[-1].lon == pytest.approx(ROUTE[-1][1], abs=1e-6)


def test_timestamps_are_monotonic_and_aware():
    pts = plan_walk(ROUTE, 4.0, 6.0, start=START, rng=random.Random(42))
    assert all(p.time.tzinfo is not None for p in pts)
    assert all(b.time > a.time for a, b in zip(pts, pts[1:]))


def test_cadence_is_one_point_per_interval():
    pts = plan_walk(ROUTE, 4.0, 6.0, interval_s=1.0, start=START, rng=random.Random(42))
    gaps = [(b.time - a.time).total_seconds() for a, b in zip(pts, pts[1:])]
    # every gap is one interval, except possibly the final clamped step
    assert all(g == pytest.approx(1.0) for g in gaps[:-1])


def test_every_speed_falls_inside_the_band():
    pts = plan_walk(ROUTE, 4.0, 6.0, start=START, rng=random.Random(42))
    for a, b in zip(pts[:-2], pts[1:-1]):          # skip final clamped step
        metres = haversine((a.lat, a.lon), (b.lat, b.lon))
        secs = (b.time - a.time).total_seconds()
        kmh = metres / secs * 3.6
        assert 4.0 - 0.01 <= kmh <= 6.0 + 0.01, f"{kmh} outside band"


def test_speed_actually_varies():
    """The whole point of a band: it must not be a flat line."""
    pts = plan_walk(ROUTE, 4.0, 6.0, start=START, rng=random.Random(42))
    speeds = {round(haversine((a.lat, a.lon), (b.lat, b.lon))
                    / (b.time - a.time).total_seconds() * 3.6, 2)
              for a, b in zip(pts[:-2], pts[1:-1])}
    assert len(speeds) > 3, "speed is constant; the band is not being used"


def test_deterministic_for_a_seeded_rng():
    a = plan_walk(ROUTE, 4.0, 6.0, start=START, rng=random.Random(7))
    b = plan_walk(ROUTE, 4.0, 6.0, start=START, rng=random.Random(7))
    assert a == b


def test_rejects_invalid_speed_band():
    with pytest.raises(ValueError):
        plan_walk(ROUTE, 6.0, 4.0, start=START)     # min > max
    with pytest.raises(ValueError):
        plan_walk(ROUTE, 0.0, 6.0, start=START)     # zero speed never arrives


def test_rejects_degenerate_route():
    with pytest.raises(ValueError):
        plan_walk([(37.0, -122.0)], 4.0, 6.0, start=START)
