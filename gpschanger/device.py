"""Drive pymobiledevice3 in-process to walk a route and then hold at its end.

Three facts drive this design. All were verified against a real handset
(iPhone 16 Pro, iOS 27.0 build 24A5424a). Do not "simplify" any of them away:

  * A single `simulateLocationWithLatitude:longitude:` is ONE-SHOT on iOS 27.
    The device ACKs it with OK and the blue dot moves, then CoreLocation
    reverts to the real GPS fix within seconds -- even with the process alive
    and the DTX channel still open. Holding a position therefore means
    re-sending that position forever, at roughly 1 Hz. This is why the stock
    `simulate-location play` cannot be used: play_gpx_file() sends the last
    point and returns, so B decays.
  * Establishing the userspace tunnel costs ~3-4 s, so one run shares ONE
    tunnel and ONE DTX channel. Walking and holding are the same send loop,
    which is what makes arrival at B seamless rather than a visible gap.
  * pymobiledevice3's API is async, so the driver owns an asyncio loop on a
    background thread and start() returns immediately.
"""
import asyncio
import contextlib
import subprocess
import threading
import time
from contextlib import asynccontextmanager

MOUNT_TIMEOUT_S = 300
HOLD_INTERVAL_S = 1.0
STOP_JOIN_TIMEOUT_S = 15.0

# How often a sleep wakes to notice a stop request. Sleeps are scheduled
# against absolute deadlines, so polling this often costs no accuracy.
_STOP_POLL_S = 0.02


class DeviceError(Exception):
    pass


@asynccontextmanager
async def _real_sim():
    """Open one userspace tunnel and one LocationSimulation channel."""
    # Imported lazily: pulls in the pure-Python PyTCP stack, which is a heavy
    # import and pointless for tests, which inject a fake instead.
    from pymobiledevice3.remote.userspace_tunnel import establish_userspace_rsd
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    rsd = await establish_userspace_rsd()
    async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as sim:
        yield sim


class LocationDriver:
    """Walks a list of pacer TrackPoints, then holds the last one.

    States: idle -> walking -> holding -> idle.
    """

    def __init__(self, sim_factory=None, binary: str = "pymobiledevice3",
                 hold_interval_s: float = HOLD_INTERVAL_S):
        self._sim_factory = sim_factory or _real_sim
        self._binary = binary
        self._hold_interval_s = hold_interval_s

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._state = "idle"
        self._index = 0
        self._total = 0
        self._pos: tuple[float | None, float | None] = (None, None)
        self.last_error: DeviceError | None = None

    def auto_mount(self) -> None:
        """Mount the Developer Disk Image.

        Mandatory: without it every simulate-location call fails with
        InvalidServiceError. Must be re-run after every device reboot. Stays a
        CLI call -- it is one-shot and needs no tunnel to reuse.
        """
        try:
            result = subprocess.run(
                [self._binary, "mounter", "auto-mount"],
                capture_output=True, text=True, timeout=MOUNT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeviceError(f"auto-mount failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DeviceError(f"auto-mount failed: {detail}")

    def start(self, points) -> None:
        """Begin walking `points` on a background thread. Returns immediately."""
        points = list(points)
        if not points:
            raise DeviceError("route is empty")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise DeviceError("a route is already running; stop it first")
            self._stop.clear()
            self.last_error = None
            self._state = "walking"
            self._index = 0
            self._total = len(points)
            self._pos = (points[0].lat, points[0].lon)
            self._thread = threading.Thread(
                target=self._run, args=(points,), name="gpschanger-driver", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop walking/holding and release the simulated location."""
        with self._lock:
            thread = self._thread
        self._stop.set()
        if thread is not None:
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)
        with self._lock:
            self._thread = None
            self._state = "idle"

    def status(self) -> dict:
        with self._lock:
            lat, lon = self._pos
            return {
                "state": self._state,
                "index": self._index,
                "total": self._total,
                "lat": lat,
                "lon": lon,
                "error": str(self.last_error) if self.last_error is not None else None,
            }

    def _run(self, points) -> None:
        try:
            asyncio.run(self._walk_and_hold(points))
        except Exception as exc:  # noqa: BLE001 - reported through status()
            error = exc if isinstance(exc, DeviceError) else DeviceError(
                f"location simulation failed: {exc}")
            with self._lock:
                self.last_error = error
                self._state = "idle"

    async def _walk_and_hold(self, points) -> None:
        base = points[0].time
        offsets = [(p.time - base).total_seconds() for p in points]
        last = points[-1]

        async with self._sim_factory() as sim:
            try:
                # Absolute schedule: every point is timed against the run's
                # start, so a slow set() cannot make the walk drift late.
                run_start = time.monotonic()
                for i, point in enumerate(points):
                    if not await self._sleep_until(run_start + offsets[i]):
                        return
                    await sim.set(point.lat, point.lon)
                    with self._lock:
                        self._index = i
                        self._pos = (point.lat, point.lon)
                        if i == len(points) - 1:
                            self._state = "holding"

                # Hold at B. Without this the position decays to the real GPS
                # fix within seconds -- see the module docstring.
                while True:
                    if not await self._sleep_until(time.monotonic() + self._hold_interval_s):
                        return
                    await sim.set(last.lat, last.lon)
            finally:
                with contextlib.suppress(Exception):
                    await sim.clear()

    async def _sleep_until(self, deadline: float) -> bool:
        """Sleep until `deadline`. False means a stop was requested."""
        while True:
            if self._stop.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(remaining, _STOP_POLL_S))
