"""Find the traffic controls a route passes through, via Overpass.

The trick that makes this cheap and exact: OSRM can annotate a route with the
OSM node ids it traverses (`annotations=nodes`), so instead of asking Overpass
"what is near this 70 km polyline" -- a huge, slow query -- we hand it the
exact node ids and ask which of them are signals or stop signs.

This is best-effort enrichment. Overpass is a free shared service and is
allowed to be slow or down; the caller is expected to catch TrafficError and
carry on with an unpaced route rather than failing the whole trip.
"""
from typing import Callable, Iterable, NamedTuple

import requests

BASE_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "gpschanger/0.1 (+https://github.com/ITSMEFRTRUST/gpschanger)"
TIMEOUT_S = 60

# Node ids per request. Overpass takes a POST body so the limit is practical
# rather than a URL length, but one enormous query is more likely to time out
# than a few smaller ones.
CHUNK = 1500

KINDS = ("traffic_signals", "stop", "give_way", "crossing", "mini_roundabout")


class TrafficError(Exception):
    pass


class Control(NamedTuple):
    node_id: int
    lat: float
    lon: float
    kind: str


def _query(node_ids: list[int]) -> str:
    ids = ",".join(str(int(n)) for n in node_ids)
    kinds = "|".join(KINDS)
    return (f'[out:json][timeout:{TIMEOUT_S}];'
            f'node(id:{ids})["highway"~"^({kinds})$"];'
            f'out body;')


def traffic_controls(
    node_ids: Iterable[int],
    *,
    chunk: int = CHUNK,
    post: Callable | None = None,
) -> list[Control]:
    """Which of these OSM nodes are traffic controls, and where."""
    ids = list(dict.fromkeys(int(n) for n in node_ids))   # de-duped, order kept
    if not ids:
        return []

    post = post or requests.post
    seen: dict[int, Control] = {}
    for start in range(0, len(ids), chunk):
        batch = ids[start:start + chunk]
        try:
            response = post(BASE_URL, data={"data": _query(batch)},
                            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise TrafficError(f"overpass lookup failed: {exc}") from exc

        for element in data.get("elements", []):
            lat, lon = element.get("lat"), element.get("lon")
            kind = (element.get("tags") or {}).get("highway")
            if lat is None or lon is None or kind not in KINDS:
                continue
            node_id = int(element["id"])
            seen.setdefault(node_id, Control(node_id, float(lat), float(lon), kind))
    return list(seen.values())
