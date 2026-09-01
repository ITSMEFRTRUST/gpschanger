# tests/test_gpx.py
import datetime as dt
import gpxpy
import pytest
from gpschanger.gpx import build_gpx, validate_gpx
from gpschanger.pacer import TrackPoint

START = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
PTS = [
    TrackPoint(37.3317000, -122.0302000, START),
    TrackPoint(37.3317280, -122.0301559, START + dt.timedelta(seconds=1)),
    TrackPoint(37.3317559, -122.0301119, START + dt.timedelta(seconds=2, milliseconds=500)),
]


def test_gpxpy_can_parse_what_we_write():
    parsed = gpxpy.parse(build_gpx(PTS))
    pts = [p for t in parsed.tracks for s in t.segments for p in s.points]
    assert len(pts) == 3


def test_all_parsed_timestamps_are_present_and_aware():
    """Guards the silent killer: compact ISO 8601 parses to time=None."""
    parsed = gpxpy.parse(build_gpx(PTS))
    pts = [p for t in parsed.tracks for s in t.segments for p in s.points]
    assert all(p.time is not None for p in pts)
    assert all(p.time.tzinfo is not None for p in pts)


def test_emits_literal_Z_suffix():
    assert "Z</time>" in build_gpx(PTS)


def test_subsecond_precision_survives_the_round_trip():
    parsed = gpxpy.parse(build_gpx(PTS))
    pts = [p for t in parsed.tracks for s in t.segments for p in s.points]
    assert (pts[2].time - pts[1].time).total_seconds() == pytest.approx(1.5)


def test_coordinates_survive_the_round_trip():
    parsed = gpxpy.parse(build_gpx(PTS))
    pts = [p for t in parsed.tracks for s in t.segments for p in s.points]
    assert pts[0].latitude == pytest.approx(37.3317, abs=1e-7)
    assert pts[0].longitude == pytest.approx(-122.0302, abs=1e-7)


def test_validate_accepts_our_own_output():
    assert len(validate_gpx(build_gpx(PTS))) == 3


def test_validate_rejects_missing_timestamps():
    xml = build_gpx(PTS).replace("<time>2026-09-01T12:00:01Z</time>", "")
    with pytest.raises(ValueError, match="timestamp"):
        validate_gpx(xml)


def test_validate_rejects_non_monotonic_times():
    bad = [PTS[0], PTS[2], PTS[1]]
    with pytest.raises(ValueError, match="monotonic"):
        validate_gpx(build_gpx(bad))


def test_validate_rejects_empty_track():
    with pytest.raises(ValueError):
        validate_gpx('<?xml version="1.0"?><gpx version="1.1" '
                     'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                     '</trkseg></trk></gpx>')
