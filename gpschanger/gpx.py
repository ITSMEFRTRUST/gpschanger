# gpschanger/gpx.py
"""Emit GPX that pymobiledevice3's play_gpx_file() will pace correctly.

The consumer skips points whose <time> is None and guards against negative
deltas, so a malformed file does not crash -- it replays instantly or
teleports. That silence is why validate_gpx() exists: we check our own output
with the same library the consumer uses, before handing the file over.
"""
import datetime as dt

import gpxpy
import gpxpy.gpx

from .pacer import TrackPoint


def build_gpx(points: list[TrackPoint], creator: str = "gpschanger") -> str:
    """Serialise track points to GPX 1.1.

    Timestamps MUST be timezone-aware: gpxpy's to_xml() mirrors whatever
    tzinfo it is given, so a naive datetime emits a Z-less <time> and the
    consumer then raises TypeError mixing naive and aware datetimes.
    """
    if not points:
        raise ValueError("no points to write")
    for p in points:
        if p.time.tzinfo is None:
            raise ValueError("track point timestamps must be timezone-aware")

    gpx = gpxpy.gpx.GPX()
    gpx.creator = creator
    track = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(track)
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)
    for p in points:
        segment.points.append(
            gpxpy.gpx.GPXTrackPoint(p.lat, p.lon, time=p.time.astimezone(dt.timezone.utc))
        )
    return gpx.to_xml()


def validate_gpx(xml: str) -> list[TrackPoint]:
    """Parse GPX back with gpxpy and reject anything that would break pacing."""
    parsed = gpxpy.parse(xml)
    raw = [p for t in parsed.tracks for s in t.segments for p in s.points]
    if not raw:
        raise ValueError("GPX contains no track points")
    for p in raw:
        if p.time is None:
            raise ValueError(
                "GPX point is missing a parsable timestamp -- the route would "
                "replay instantly with no pacing"
            )
        if p.time.tzinfo is None:
            raise ValueError("GPX point has a naive timestamp; expected a Z suffix")
    points = [TrackPoint(p.latitude, p.longitude, p.time) for p in raw]
    for a, b in zip(points, points[1:]):
        if b.time <= a.time:
            raise ValueError(
                "GPX timestamps are not monotonic -- the route would teleport"
            )
    return points
