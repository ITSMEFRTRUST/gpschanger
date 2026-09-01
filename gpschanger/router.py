# gpschanger/router.py
"""Road routing via OSRM, on foot or by car.

Uses the FOSSGIS instances. The official demo server at
router.project-osrm.org accepts foot/walking in the URL but SILENTLY returns
car routing -- six profile strings there return byte-identical results at
26 km/h. Do not point this at that host.

FOSSGIS splits the profiles across separate hosts rather than serving them all
from one: the instance name and the profile in the path must agree, so they
are kept together in MODES and never composed by hand.
"""
from typing import Callable, NamedTuple

import requests

BASE_URL = "https://routing.openstreetmap.de/routed-foot"   # the walking instance

# mode -> (FOSSGIS instance, OSRM profile segment in the request path)
MODES = {
    "walk": ("routed-foot", "foot"),
    "drive": ("routed-car", "driving"),
}
_HOST = "https://routing.openstreetmap.de"
USER_AGENT = "gpschanger/0.1 (+https://github.com/ITSMEFRTRUST/gpschanger)"
TIMEOUT_S = 15


class RouterError(Exception):
    pass


class Route(NamedTuple):
    points: list[tuple[float, float]]   # (lat, lon)
    distance_m: float
    duration_s: float
    nodes: tuple[int, ...] = ()         # OSM nodes traversed (drive mode only)
    turns: tuple[tuple[float, float], ...] = ()   # (lat, lon) of each turn

# Maneuver types that are not a turn you would slow down for.
_NOT_TURNS = {"depart", "arrive", "new name", "notification", "use lane", "continue"}
# Nor are these: a slight left/right is a lane-width curve you take at speed.
_SMOOTH_MODIFIERS = {"straight", "slight left", "slight right"}


def foot_route(
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    get: Callable | None = None,
) -> Route:
    """Walking route between two (lat, lon) points. Thin alias for route_between."""
    return route_between(a, b, mode="walk", get=get)


def route_between(
    a: tuple[float, float],
    b: tuple[float, float],
    mode: str = "walk",
    *,
    get: Callable | None = None,
) -> Route:
    """Road route between two (lat, lon) points, on foot or by car.

    OSRM speaks lon,lat -- the reverse of the usual order -- on both the
    request path and in geometry.coordinates, so coordinates are flipped
    twice: once going out, once coming back.
    """
    try:
        instance, profile = MODES[mode]
    except KeyError:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {sorted(MODES)}") from None

    get = get or requests.get
    coords = f"{a[1]},{a[0]};{b[1]},{b[0]}"     # lon,lat pairs

    # Drive mode needs the OSM nodes (to look up traffic controls) and the
    # steps (to find turns). Walking asks for neither -- it is a lot of extra
    # payload on a long route and nothing consumes it.
    params = {"overview": "full", "geometries": "geojson", "steps": "false"}
    if mode == "drive":
        params["steps"] = "true"
        params["annotations"] = "nodes"
    # Every transport-level failure has to come out as RouterError: server.py
    # renders that as a JSON 502, whereas an escaping requests exception
    # becomes Flask's HTML 500 and the browser then dies parsing it as JSON.
    try:
        response = get(
            f"{_HOST}/{instance}/route/v1/{profile}/{coords}",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_S,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RouterError(f"OSRM request failed: {exc}") from exc

    # OSRM signals failure in the body, not the HTTP status.
    if data.get("code") != "Ok":
        raise RouterError(f"OSRM returned {data.get('code')}: {data.get('message', '')}")
    routes = data.get("routes") or []
    if not routes:
        raise RouterError("OSRM returned no routes")

    route = routes[0]
    points = [(c[1], c[0]) for c in route["geometry"]["coordinates"]]

    nodes: tuple[int, ...] = ()
    turns: list[tuple[float, float]] = []
    for leg in route.get("legs") or []:
        nodes += tuple(int(n) for n in (leg.get("annotation") or {}).get("nodes", []))
        for step in leg.get("steps") or []:
            maneuver = step.get("maneuver") or {}
            if maneuver.get("type") in _NOT_TURNS:
                continue
            if maneuver.get("modifier") in _SMOOTH_MODIFIERS:
                continue
            location = maneuver.get("location")
            if location and len(location) >= 2:
                turns.append((float(location[1]), float(location[0])))

    return Route(points, float(route["distance"]), float(route["duration"]),
                 nodes, tuple(turns))
