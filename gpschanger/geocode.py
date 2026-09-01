"""Place search via Photon (komoot's OSM geocoder).

Photon rather than Nominatim: both work and neither needs a key, but Photon is
explicitly built for as-you-type search, while Nominatim's usage policy asks
callers not to issue a request per keystroke.

Results are biased towards a point when one is given -- without that, "main
street" ranks by nothing useful and the first hit can be on another continent.
"""
from typing import Callable, NamedTuple

import requests

BASE_URL = "https://photon.komoot.io/api/"
USER_AGENT = "gpschanger/0.1 (+https://github.com/ITSMEFRTRUST/gpschanger)"
TIMEOUT_S = 10


class GeocodeError(Exception):
    pass


class Place(NamedTuple):
    name: str
    label: str      # name plus its context, for display
    lat: float
    lon: float


def _label(props: dict, name: str) -> str:
    parts = [name]
    for key in ("street", "district", "city", "county", "state", "country"):
        value = props.get(key)
        if value and value not in parts:
            parts.append(value)
    return ", ".join(p for p in parts if p)


def search(
    query: str,
    *,
    limit: int = 6,
    near: tuple[float, float] | None = None,
    get: Callable | None = None,
) -> list[Place]:
    """Search for a street, place or address. Returns [] when nothing matches."""
    query = (query or "").strip()
    if not query:
        raise ValueError("empty search query")

    params: dict[str, object] = {"q": query, "limit": limit}
    if near is not None:
        params["lat"], params["lon"] = float(near[0]), float(near[1])

    get = get or requests.get
    try:
        response = get(BASE_URL, params=params,
                       headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise GeocodeError(f"search failed: {exc}") from exc

    places = []
    for feature in data.get("features", []):
        coords = (feature.get("geometry") or {}).get("coordinates")
        if not coords or len(coords) < 2:
            continue                                  # Photon can return a
        props = feature.get("properties") or {}       # feature with no point
        name = props.get("name") or props.get("street") or props.get("city") or query
        places.append(Place(name=name, label=_label(props, name),
                            lat=float(coords[1]), lon=float(coords[0])))
    return places
