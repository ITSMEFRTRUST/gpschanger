"""Tests for traffic-aware drive pacing.

A car that teleports through every junction at a constant 70 km/h is the tell
that gives a spoof away, so these assert the physics rather than just the
endpoints: it must brake before a stop, actually sit still for the dwell, and
pull away again afterwards.
"""
import datetime as dt
import random

import pytest

from gpschanger.pacer import (
    StopEvent, drive_events, haversine, plan_drive, route_distances,
)
from gpschanger.traffic import Control

START = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def straight_route(metres=1200.0, step=10.0):
    """A due-north line, one vertex every `step` metres."""
    deg_per_m = 1.0 / 111_320.0
    n = int(metres / step) + 1
    return [(51.5 + i * step * deg_per_m, -0.128) for i in range(n)]


ROUTE = straight_route()
TOTAL = route_distances(ROUTE)[-1]


def speeds(points):
    """km/h between consecutive points."""
    out = []
    for a, b in zip(points, points[1:]):
        secs = (b.time - a.time).total_seconds()
        out.append(haversine((a.lat, a.lon), (b.lat, b.lon)) / secs * 3.6)
    return out


# --- plan_drive -------------------------------------------------------------

def test_without_events_it_behaves_like_ordinary_pacing():
    pts = plan_drive(ROUTE, 40, 90, [], start=START, rng=random.Random(1))
    assert pts[0].lat == pytest.approx(ROUTE[0][0])
    assert pts[-1].lat == pytest.approx(ROUTE[-1][0], abs=1e-6)
    assert all(b.time > a.time for a, b in zip(pts, pts[1:]))


def test_it_sits_still_for_the_dwell_at_a_full_stop():
    event = StopEvent(distance_m=TOTAL / 2, target_mps=0.0, dwell_s=20.0, kind="traffic_signals")
    pts = plan_drive(ROUTE, 40, 90, [event], start=START, rng=random.Random(1))

    still = [round(haversine((a.lat, a.lon), (b.lat, b.lon)), 3)
             for a, b in zip(pts, pts[1:])]
    longest = best = 0
    for gap in still:
        best = best + 1 if gap < 0.5 else 0
        longest = max(longest, best)
    assert 18 <= longest <= 23, f"stationary for {longest}s, expected ~20"


def test_it_brakes_before_the_stop_rather_than_teleporting_to_zero():
    event = StopEvent(distance_m=TOTAL / 2, target_mps=0.0, dwell_s=10.0, kind="stop")
    pts = plan_drive(ROUTE, 60, 60, [event], start=START, rng=random.Random(2))
    v = speeds(pts)

    stop_at = next(i for i, s in enumerate(v) if s < 0.5)
    approach = v[max(0, stop_at - 8):stop_at]
    assert len(approach) >= 4, "no approach phase at all"
    assert approach == sorted(approach, reverse=True), f"not decelerating: {approach}"


def test_it_pulls_away_again_after_the_stop():
    event = StopEvent(distance_m=TOTAL / 2, target_mps=0.0, dwell_s=10.0, kind="stop")
    pts = plan_drive(ROUTE, 60, 60, [event], start=START, rng=random.Random(3))
    v = speeds(pts)
    last_still = max(i for i, s in enumerate(v) if s < 0.5)
    after = v[last_still + 1:last_still + 6]
    assert after == sorted(after), f"not accelerating away: {after}"
    assert after[-1] > 10


def test_deceleration_stays_within_a_comfortable_rate():
    event = StopEvent(distance_m=TOTAL / 2, target_mps=0.0, dwell_s=5.0, kind="stop")
    pts = plan_drive(ROUTE, 90, 90, [event], start=START, rng=random.Random(4))
    # Drop the final step: it is clamped to the route end, so it covers less
    # than a full second and reads as a huge deceleration that never happened.
    v = speeds(pts)[:-1]
    drops = [(a - b) / 3.6 for a, b in zip(v, v[1:]) if b < a]
    assert max(drops) < 4.0, f"braking at {max(drops):.1f} m/s^2 is a crash, not a stop"


def test_a_slow_through_event_slows_without_stopping():
    event = StopEvent(distance_m=TOTAL / 2, target_mps=15 / 3.6, dwell_s=0.0, kind="turn")
    pts = plan_drive(ROUTE, 80, 80, [event], start=START, rng=random.Random(5))
    v = speeds(pts)
    assert min(v) < 20, "never slowed for the turn"
    assert min(v) > 2, "came to a full stop at a slow-through event"


def test_stops_make_the_trip_take_longer():
    plain = plan_drive(ROUTE, 50, 50, [], start=START, rng=random.Random(6))
    events = [StopEvent(TOTAL * f, 0.0, 15.0, "traffic_signals") for f in (0.3, 0.6)]
    stopped = plan_drive(ROUTE, 50, 50, events, start=START, rng=random.Random(6))
    assert len(stopped) > len(plain) + 25


def test_several_stops_are_all_served():
    events = [StopEvent(TOTAL * f, 0.0, 8.0, "stop") for f in (0.2, 0.4, 0.6, 0.8)]
    pts = plan_drive(ROUTE, 60, 60, events, start=START, rng=random.Random(7))
    v = speeds(pts)
    runs = 0
    inside = False
    for s in v:
        if s < 0.5 and not inside:
            runs, inside = runs + 1, True
        elif s >= 0.5:
            inside = False
    assert runs == 4, f"served {runs} of 4 stops"


def test_it_still_finishes_at_b():
    events = [StopEvent(TOTAL * f, 0.0, 5.0, "stop") for f in (0.5, 0.99)]
    pts = plan_drive(ROUTE, 60, 60, events, start=START, rng=random.Random(8))
    assert pts[-1].lat == pytest.approx(ROUTE[-1][0], abs=1e-5)


def test_an_event_at_the_very_start_does_not_hang():
    events = [StopEvent(0.0, 0.0, 5.0, "stop")]
    pts = plan_drive(ROUTE, 60, 60, events, start=START, rng=random.Random(9))
    assert pts[-1].lat == pytest.approx(ROUTE[-1][0], abs=1e-5)


def test_events_beyond_the_route_are_ignored():
    events = [StopEvent(TOTAL * 5, 0.0, 30.0, "stop")]
    pts = plan_drive(ROUTE, 60, 60, events, start=START, rng=random.Random(10))
    assert pts[-1].lat == pytest.approx(ROUTE[-1][0], abs=1e-5)


def test_deterministic_for_a_seeded_rng():
    events = [StopEvent(TOTAL / 2, 0.0, 12.0, "traffic_signals")]
    a = plan_drive(ROUTE, 40, 90, events, start=START, rng=random.Random(11))
    b = plan_drive(ROUTE, 40, 90, events, start=START, rng=random.Random(11))
    assert a == b


def test_speed_never_exceeds_the_band():
    pts = plan_drive(ROUTE, 40, 60, [], start=START, rng=random.Random(12))
    assert max(speeds(pts)[:-1]) <= 60.5


# --- drive_events -----------------------------------------------------------

def test_a_stop_sign_is_rolled_through_never_obeyed():
    """Stop signs slow you down here; they do not stop you."""
    control = Control(1, ROUTE[len(ROUTE) // 2][0], ROUTE[0][1], "stop")
    for seed in range(30):
        events = drive_events(ROUTE, [control], rng=random.Random(seed))
        assert len(events) == 1
        assert events[0].dwell_s == 0.0, "stopped at a stop sign"
        assert events[0].target_mps > 0


def test_a_give_way_is_never_obeyed_either():
    control = Control(1, ROUTE[len(ROUTE) // 2][0], ROUTE[0][1], "give_way")
    for seed in range(30):
        events = drive_events(ROUTE, [control], rng=random.Random(seed))
        assert events[0].dwell_s == 0.0
        assert events[0].target_mps > 0


def test_a_mini_roundabout_produces_no_event_at_all():
    """Skipped entirely, not merely a slow-through."""
    control = Control(1, ROUTE[len(ROUTE) // 2][0], ROUTE[0][1], "mini_roundabout")
    assert drive_events(ROUTE, [control], rng=random.Random(1)) == []


def test_crossings_almost_never_catch_you():
    controls = [Control(i, lat, lon, "crossing")
                for i, (lat, lon) in enumerate(ROUTE[5:105])]
    stopped = [e for e in drive_events(ROUTE, controls, rng=random.Random(2))
               if e.dwell_s > 0]
    assert 0 <= len(stopped) <= 15, f"{len(stopped)}/100 crossings stopped, expected ~5"


def test_traffic_signals_sometimes_catch_you_and_sometimes_do_not():
    """A light that is always red is as unrealistic as one that is never red."""
    controls = [Control(i, lat, lon, "traffic_signals")
                for i, (lat, lon) in enumerate(ROUTE[10:90:4])]
    events = drive_events(ROUTE, controls, rng=random.Random(3))
    stopped = [e for e in events if e.dwell_s > 0]
    assert 0 < len(stopped) < len(events), "signals are all-or-nothing"
    assert all(8.0 <= e.dwell_s <= 40.0 for e in stopped)


def test_events_come_back_sorted_by_distance_along_the_route():
    controls = [Control(i, lat, lon, "stop")
                for i, (lat, lon) in enumerate(reversed(ROUTE[10:80:7]))]
    events = drive_events(ROUTE, controls, rng=random.Random(4))
    assert [e.distance_m for e in events] == sorted(e.distance_m for e in events)


def test_turns_slow_to_fifty_or_sixty_whatever_their_shape():
    turns = [ROUTE[len(ROUTE) // 3]]
    for seed in range(20):
        events = drive_events(ROUTE, [], turns=turns, rng=random.Random(seed))
        assert len(events) == 1
        assert events[0].kind == "turn"
        assert events[0].dwell_s == 0.0
        kmh = events[0].target_mps * 3.6
        assert 50.0 <= kmh <= 60.0, f"turn speed {kmh:.1f} km/h outside 50-60"


def test_controls_far_from_the_route_are_dropped():
    """Overpass answers by node id, but a bad match must not warp the route."""
    control = Control(1, 10.0, 20.0, "stop")
    assert drive_events(ROUTE, [control], rng=random.Random(6)) == []
