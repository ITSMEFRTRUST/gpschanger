"""Tests for the in-process location driver.

Nothing here touches a real device: the driver takes an injectable sim_factory,
so these drive a fake that records (lat, lon, monotonic) for every set().

Timings are deliberately tiny (tens of ms) so the suite stays fast. The timing
assertions are one-sided -- a point must never fire EARLY, since firing early is
the only way pacing can silently lie about speed. Late is allowed, because a
loaded CI box will always be a bit late.
"""
import datetime as dt
import time
from contextlib import asynccontextmanager

import pytest

from gpschanger.device import DeviceError, LocationDriver
from gpschanger.pacer import TrackPoint

STEP = 0.05
T0 = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def make_points(n=4, step=STEP):
    return [
        TrackPoint(37.0 + i * 0.001, -122.0, T0 + dt.timedelta(seconds=i * step))
        for i in range(n)
    ]


class FakeSim:
    """Stand-in for pymobiledevice3's LocationSimulation."""

    def __init__(self, fail_at=None):
        self.calls = []
        self.clears = 0
        self.fail_at = fail_at

    async def set(self, latitude, longitude):
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("simulated DTX failure")
        self.calls.append((latitude, longitude, time.monotonic()))

    async def clear(self):
        self.clears += 1


def factory_for(sim):
    @asynccontextmanager
    async def factory():
        yield sim

    return factory


def driver_for(sim, hold_interval_s=0.05):
    return LocationDriver(sim_factory=factory_for(sim), hold_interval_s=hold_interval_s)


def wait_for_state(driver, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if driver.status()["state"] == state:
            return True
        time.sleep(0.01)
    return False


def test_walk_visits_every_point_in_order():
    sim = FakeSim()
    d = driver_for(sim)
    points = make_points()
    d.start(points)
    assert wait_for_state(d, "holding"), "never reached holding"
    d.stop()

    visited = [(lat, lon) for lat, lon, _ in sim.calls[: len(points)]]
    assert visited == [(p.lat, p.lon) for p in points]


def test_points_are_never_sent_early():
    sim = FakeSim()
    d = driver_for(sim)
    points = make_points(n=5)
    d.start(points)
    assert wait_for_state(d, "holding")
    d.stop()

    first = sim.calls[0][2]
    for i, (_, _, sent) in enumerate(sim.calls[: len(points)]):
        expected = i * STEP
        assert sent - first >= expected - 0.02, f"point {i} fired early"


def test_holds_at_b_by_resending_the_final_point():
    """The whole reason this driver exists: a single set() is one-shot on iOS 27."""
    sim = FakeSim()
    d = driver_for(sim, hold_interval_s=0.03)
    points = make_points()
    d.start(points)
    assert wait_for_state(d, "holding")

    n_at_hold = len(sim.calls)
    time.sleep(0.2)
    d.stop()

    extra = sim.calls[n_at_hold:]
    assert len(extra) >= 2, f"only {len(extra)} keepalive sends; B would decay"
    last = points[-1]
    assert all((lat, lon) == (last.lat, last.lon) for lat, lon, _ in extra)


def test_status_reports_walking_then_holding():
    sim = FakeSim()
    d = driver_for(sim)
    assert d.status()["state"] == "idle"
    d.start(make_points(n=8, step=0.08))
    assert wait_for_state(d, "walking")
    s = d.status()
    assert s["total"] == 8
    assert 0 <= s["index"] < 8
    assert wait_for_state(d, "holding")
    assert d.status()["index"] == 7
    d.stop()


def test_stop_returns_to_idle_and_clears_once():
    sim = FakeSim()
    d = driver_for(sim)
    d.start(make_points())
    assert wait_for_state(d, "holding")
    d.stop()
    assert d.status()["state"] == "idle"
    assert sim.clears == 1


def test_stop_is_idempotent():
    sim = FakeSim()
    d = driver_for(sim)
    d.start(make_points())
    assert wait_for_state(d, "holding")
    d.stop()
    d.stop()
    assert sim.clears == 1


def test_start_while_running_raises():
    sim = FakeSim()
    d = driver_for(sim)
    d.start(make_points(n=10, step=0.08))
    assert wait_for_state(d, "walking")
    with pytest.raises(DeviceError):
        d.start(make_points())
    d.stop()


def test_start_rejects_an_empty_route():
    d = driver_for(FakeSim())
    with pytest.raises(DeviceError):
        d.start([])


def test_a_failing_set_surfaces_as_an_error_and_returns_to_idle():
    sim = FakeSim(fail_at=2)
    d = driver_for(sim)
    d.start(make_points(n=6))
    assert wait_for_state(d, "idle"), "driver did not give up after the failure"
    status = d.status()
    assert status["error"] is not None
    assert isinstance(d.last_error, DeviceError)


def test_a_failed_run_can_be_restarted():
    sim = FakeSim(fail_at=2)
    d = driver_for(sim)
    d.start(make_points(n=6))
    assert wait_for_state(d, "idle")

    good = FakeSim()
    d2 = driver_for(good)
    d2.start(make_points())
    assert wait_for_state(d2, "holding")
    d2.stop()
    assert d2.status()["error"] is None
