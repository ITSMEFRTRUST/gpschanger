"""Local Flask app: map UI plus the route/play/stop API.

Binds 127.0.0.1 only -- it writes files and drives a paired device, with no
authentication of any kind.

Amended 2026-09-01: the app owns a LocationDriver (a background thread), not a
child process. /api/route keeps the planned points in memory under a route_id
and /api/play hands those points straight to the driver, so no file path ever
crosses the API boundary from the client.
"""
import datetime as dt
import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .device import DeviceError, LocationDriver
from .geocode import GeocodeError, search
from .gpx import build_gpx, validate_gpx
from .pacer import drive_events, plan_drive, plan_walk
from .router import MODES, RouterError, route_between
from .traffic import TrafficError, traffic_controls

HERE = Path(__file__).parent
ROUTE_DIR = str(HERE.parent / "routes")

# A run is walking or holding; both mean "the phone is being spoofed right now".
LIVE_STATES = ("walking", "holding")

# Per-mode defaults. reroll_s is how often the pacer picks a new speed inside
# the band: a walker's pace wanders every few seconds, a car holds a speed for
# much longer, and re-rolling a car every 3 s reads as constant hard braking.
MODE_DEFAULTS = {
    "walk": {"speed_min_kmh": 4.0, "speed_max_kmh": 6.0, "reroll_s": 3.0},
    "drive": {"speed_min_kmh": 40.0, "speed_max_kmh": 90.0, "reroll_s": 8.0},
}


def _coords(pair) -> tuple[float, float]:
    """Validate one [lat, lon] pair from the client.

    Leaflet reports longitudes outside +/-180 once the user pans across a world
    copy -- a real run sent one a full 360 degrees off (-360.13 instead of
    -0.13) and OSRM answered 400. Fold those back rather than rejecting them:
    the point the user clicked is unambiguous, only its representation wrapped.
    """
    try:
        lat, lon = float(pair[0]), float(pair[1])
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        raise ValueError("malformed coordinate") from exc
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude {lat} is out of range")
    return lat, round(((lon + 180.0) % 360.0) - 180.0, 7)


def create_app(driver: LocationDriver | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(HERE / "static"))
    # Bound to a new name so the closures below capture a non-optional driver.
    device = driver or LocationDriver()
    os.makedirs(ROUTE_DIR, exist_ok=True)

    # route_id -> planned TrackPoints. Small and per-process; a route is cheap
    # to recompute, so nothing here needs to survive a restart.
    planned: dict[str, list] = {}

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.post("/api/route")
    def api_route():
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("mode", "walk")
        if mode not in MODES:
            return jsonify(
                error=f"unknown mode {mode!r}; expected one of {sorted(MODES)}"), 400
        defaults = MODE_DEFAULTS[mode]

        try:
            a = _coords(body["a"])
            b = _coords(body["b"])
            lo = float(body.get("speed_min_kmh", defaults["speed_min_kmh"]))
            hi = float(body.get("speed_max_kmh", defaults["speed_max_kmh"]))
        except (KeyError, TypeError, IndexError):
            return jsonify(error="malformed request body"), 400
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        try:
            route = route_between(a, b, mode)
        except RouterError as exc:
            return jsonify(error=f"routing failed: {exc}"), 502

        stops = 0
        note = None
        try:
            if mode == "drive":
                # Best-effort enrichment: Overpass is a free shared service and
                # is allowed to be slow or down. A route with no traffic stops
                # is far better than no route at all.
                controls = []
                if route.nodes:
                    try:
                        controls = traffic_controls(route.nodes)
                    except TrafficError as exc:
                        note = f"traffic data unavailable, driving without stops ({exc})"
                events = drive_events(route.points, controls, turns=route.turns)
                stops = sum(1 for e in events if e.dwell_s > 0)
                points = plan_drive(route.points, lo, hi, events,
                                    reroll_s=defaults["reroll_s"])
            else:
                points = plan_walk(route.points, lo, hi, reroll_s=defaults["reroll_s"])
            xml = build_gpx(points)
            parsed = validate_gpx(xml)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        route_id = f"route-{stamp}-{uuid.uuid4().hex[:8]}"
        path = os.path.join(ROUTE_DIR, f"{route_id}.gpx")
        Path(path).write_text(xml)

        # Serve the points we parsed back out of the GPX, not the ones we
        # computed, so the polyline on screen is literally what the phone
        # walks -- and drive the device from those same points.
        planned[route_id] = parsed

        return jsonify(
            route_id=route_id,
            mode=mode,
            stops=stops,
            note=note,
            coords=[[p.lat, p.lon] for p in parsed],
            points=len(parsed),
            distance_m=route.distance_m,
            duration_s=(parsed[-1].time - parsed[0].time).total_seconds(),
            gpx_path=path,
        )

    @app.get("/api/search")
    def api_search():
        near = None
        lat, lon = request.args.get("lat"), request.args.get("lon")
        if lat and lon:
            try:
                near = (float(lat), float(lon))
            except ValueError:
                near = None      # a bad bias is not worth failing the search
        try:
            places = search(request.args.get("q", ""), near=near)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except GeocodeError as exc:
            return jsonify(error=str(exc)), 502
        return jsonify(results=[
            {"name": p.name, "label": p.label, "lat": p.lat, "lon": p.lon}
            for p in places
        ])

    @app.post("/api/play")
    def api_play():
        body = request.get_json(force=True, silent=True) or {}
        points = planned.get(body.get("route_id", ""))
        if points is None:
            return jsonify(error="unknown route_id; plan a route first"), 404
        try:
            # Must be re-run after every phone reboot, so do it every play
            # rather than once at startup. It is a no-op when already mounted.
            device.auto_mount()
            device.start(points)
        except DeviceError as exc:
            return jsonify(error=str(exc)), 502
        return _status_payload(device)

    @app.post("/api/stop")
    def api_stop():
        device.stop()
        return _status_payload(device)

    @app.get("/api/status")
    def api_status():
        return _status_payload(device)

    return app


def _status_payload(driver):
    status = driver.status()
    status["running"] = status["state"] in LIVE_STATES
    return jsonify(**status)


if __name__ == "__main__":
    # debug=False deliberately: the reloader would run a second copy of the
    # app, and two processes both trying to establish the userspace tunnel
    # (PyTCP's stack is a process-global singleton) is a good way to orphan
    # a live spoof loop mid-run.
    create_app().run(host="127.0.0.1", port=8770, debug=False)
