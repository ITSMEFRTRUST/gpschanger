# gpschanger/pacer.py
"""Turn a road route plus a speed band into 1 Hz timestamped track points.

Pure: no network, no device, no filesystem. The speed band is the reason this
project exists, so the varying-speed logic lives here and is unit-tested.
"""
import datetime as dt
import math
import random
from typing import NamedTuple

R_EARTH = 6371008.8  # mean Earth radius, metres


class TrackPoint(NamedTuple):
    lat: float
    lon: float
    time: dt.datetime


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = p2 - p1
    dl = math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(h))


def _point_at_distance(segments, target_m):
    """Interpolate the (lat, lon) lying target_m along the polyline.

    Linear in lat/lon, which is accurate at OSRM's point spacing (metres).
    """
    acc = 0.0
    for a, b, length in segments:
        if length > 0 and acc + length >= target_m:
            f = (target_m - acc) / length
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        acc += length
    return segments[-1][1]


def plan_walk(
    route: list[tuple[float, float]],
    speed_min_kmh: float,
    speed_max_kmh: float,
    *,
    interval_s: float = 1.0,
    reroll_s: float = 3.0,
    start: dt.datetime | None = None,
    rng: random.Random | None = None,
) -> list[TrackPoint]:
    """Walk `route` at a speed re-rolled inside [min, max] every `reroll_s`.

    Emits one point per `interval_s` of travel (time-based cadence: iOS
    delivers location updates at roughly 1 Hz, so distance-based spacing
    produces awkward gaps). The final point is clamped exactly to the route
    end, so its step may be shorter than the others.
    """
    if len(route) < 2:
        raise ValueError("route needs at least two points")
    if speed_min_kmh <= 0:
        raise ValueError("speed_min_kmh must be > 0")
    if speed_min_kmh > speed_max_kmh:
        raise ValueError("speed_min_kmh must be <= speed_max_kmh")

    rng = rng or random.Random()
    if start is None:
        start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")

    segments = [(a, b, haversine(a, b)) for a, b in zip(route, route[1:])]
    total_m = sum(s[2] for s in segments)
    if total_m == 0:
        raise ValueError("route has zero length")

    points = [TrackPoint(route[0][0], route[0][1], start)]
    travelled = 0.0
    elapsed = 0.0
    speed_mps = 0.0
    next_reroll = 0.0

    while travelled < total_m:
        if elapsed >= next_reroll:
            speed_mps = rng.uniform(speed_min_kmh, speed_max_kmh) / 3.6
            next_reroll = elapsed + reroll_s
        travelled = min(total_m, travelled + speed_mps * interval_s)
        elapsed += interval_s
        lat, lon = _point_at_distance(segments, travelled)
        points.append(TrackPoint(lat, lon, start + dt.timedelta(seconds=elapsed)))

    return points


# --- traffic-aware drive pacing ---------------------------------------------
#
# Constant-speed travel through every junction is the tell that gives a spoofed
# drive away, so plan_drive integrates a simple longitudinal model instead:
# cruise inside the band, brake for what is ahead, sit still for the dwell,
# accelerate away. Everything here is pure and deterministic under a seeded rng.

ACCEL_MPS2 = 1.8        # comfortable pull-away
DECEL_MPS2 = 2.5        # comfortable braking; ~7 m/s^2 would be an emergency
LOOKAHEAD_M = 400.0     # far enough to brake from motorway speed
ARRIVE_EPS_M = 1.0
CREEP_MPS = 0.5         # slow enough to count as arrived at the line
MAX_TICKS = 24 * 60 * 60

# kind -> (probability of actually stopping, dwell range, speed if not stopping)
#
# These are a driving style, not road law: only lights and the occasional
# crossing actually stop. Stop signs and give-ways are rolled through rather
# than obeyed, and mini-roundabouts are ignored entirely (absent from this
# table = no event). Edit to taste.
TRAFFIC_POLICY = {
    "traffic_signals":  (0.60, (8.0, 40.0), 25 / 3.6),
    "stop":             (0.00, (0.0, 0.0),  25 / 3.6),
    "give_way":         (0.00, (0.0, 3.0),  20 / 3.6),
    "crossing":         (0.05, (2.0, 6.0),  30 / 3.6),
}

# Turns slow to this band whatever their shape. Gentle ("slight") turns never
# reach here at all -- the router drops them, because you do not lift off for
# a lane-width curve.
TURN_KMH = (50.0, 60.0)
MAX_CONTROL_OFFSET_M = 25.0   # farther than this and the node is not on our route


class StopEvent(NamedTuple):
    distance_m: float    # how far along the route it sits
    target_mps: float    # speed to be doing on arrival; 0 means a full stop
    dwell_s: float       # how long to sit still once stopped
    kind: str


def route_distances(route: list[tuple[float, float]]) -> list[float]:
    """Cumulative distance in metres to each vertex of the route."""
    out = [0.0]
    for a, b in zip(route, route[1:]):
        out.append(out[-1] + haversine(a, b))
    return out


def _distance_along(route, cumulative, point) -> float | None:
    """Where along the route a point sits, or None if it is not on the route."""
    best_d, best_at = None, None
    for (lat, lon), along in zip(route, cumulative):
        d = haversine((lat, lon), point)
        if best_d is None or d < best_d:
            best_d, best_at = d, along
    if best_d is None or best_d > MAX_CONTROL_OFFSET_M:
        return None
    return best_at


def drive_events(route, controls, *, turns=(), rng=None) -> list[StopEvent]:
    """Turn Overpass controls and OSRM turn locations into pacing events.

    Signals are probabilistic on purpose: a light that is always red reads as
    fake just as clearly as one that is never red.
    """
    rng = rng or random.Random()
    cumulative = route_distances(route)
    events: list[StopEvent] = []

    for control in controls:
        policy = TRAFFIC_POLICY.get(control.kind)
        if policy is None:
            continue
        along = _distance_along(route, cumulative, (control.lat, control.lon))
        if along is None:
            continue
        stop_chance, (dwell_lo, dwell_hi), rolling_mps = policy
        if rng.random() < stop_chance:
            events.append(StopEvent(along, 0.0, rng.uniform(dwell_lo, dwell_hi),
                                    control.kind))
        else:
            events.append(StopEvent(along, rolling_mps, 0.0, control.kind))

    for turn in turns:
        along = _distance_along(route, cumulative, turn)
        if along is not None:
            events.append(StopEvent(along, rng.uniform(*TURN_KMH) / 3.6, 0.0, "turn"))

    events.sort(key=lambda e: e.distance_m)
    return events


def plan_drive(
    route: list[tuple[float, float]],
    speed_min_kmh: float,
    speed_max_kmh: float,
    events: list[StopEvent],
    *,
    interval_s: float = 1.0,
    reroll_s: float = 8.0,
    accel: float = ACCEL_MPS2,
    decel: float = DECEL_MPS2,
    start: dt.datetime | None = None,
    rng: random.Random | None = None,
) -> list[TrackPoint]:
    """Drive `route`, braking for `events` and dwelling where they say to."""
    if len(route) < 2:
        raise ValueError("route needs at least two points")
    if speed_min_kmh <= 0:
        raise ValueError("speed_min_kmh must be > 0")
    if speed_min_kmh > speed_max_kmh:
        raise ValueError("speed_min_kmh must be <= speed_max_kmh")

    rng = rng or random.Random()
    if start is None:
        start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")

    segments = [(a, b, haversine(a, b)) for a, b in zip(route, route[1:])]
    total_m = sum(s[2] for s in segments)
    if total_m == 0:
        raise ValueError("route has zero length")

    pending = sorted((e for e in events if 0.0 <= e.distance_m <= total_m),
                     key=lambda e: e.distance_m)

    points = [TrackPoint(route[0][0], route[0][1], start)]
    s = v = elapsed = 0.0
    dwell_left = 0.0
    served = 0
    cruise = 0.0
    next_reroll = 0.0

    for _ in range(MAX_TICKS):
        if s >= total_m:
            break

        if dwell_left > 0:                       # parked at a red light
            dwell_left -= interval_s
            elapsed += interval_s
            lat, lon = _point_at_distance(segments, s)
            points.append(TrackPoint(lat, lon, start + dt.timedelta(seconds=elapsed)))
            continue

        if elapsed >= next_reroll:
            cruise = rng.uniform(speed_min_kmh, speed_max_kmh) / 3.6
            next_reroll = elapsed + reroll_s

        # Slowest speed we may be doing now and still make every upcoming
        # constraint at a comfortable braking rate.
        v_limit = cruise
        i = served
        while i < len(pending) and pending[i].distance_m - s <= LOOKAHEAD_M:
            gap = max(0.0, pending[i].distance_m - s)
            # Discrete-time safe braking speed: the speed we may hold for one
            # whole tick and STILL be able to reach the target afterwards.
            # Using the continuous sqrt(2*a*gap) instead evaluates the curve
            # only at tick starts, so the car sails past it and the last
            # sample before a stop dumps ~7 m/s in one second.
            brake = decel * interval_s
            v_limit = min(v_limit,
                          -brake + math.sqrt(brake ** 2
                                             + pending[i].target_mps ** 2
                                             + 2 * decel * gap))
            i += 1

        v = min(v_limit, v + accel * interval_s) if v < v_limit \
            else max(v_limit, v - decel * interval_s)
        v = max(0.0, v)

        s = min(total_m, s + v * interval_s)
        elapsed += interval_s
        lat, lon = _point_at_distance(segments, s)
        points.append(TrackPoint(lat, lon, start + dt.timedelta(seconds=elapsed)))

        # Arrived if we are on the line, or crawling within a few metres of it
        # (the braking curve approaches the line asymptotically).
        gap_to_next = (pending[served].distance_m - s) if served < len(pending) else None
        if gap_to_next is not None and (gap_to_next <= ARRIVE_EPS_M
                                        or (v < CREEP_MPS and gap_to_next < 5.0)):
            event = pending[served]
            served += 1
            if event.dwell_s > 0:
                s = event.distance_m
                v = 0.0
                dwell_left = event.dwell_s
    else:
        raise ValueError("drive did not terminate; check the speed band")

    return points
