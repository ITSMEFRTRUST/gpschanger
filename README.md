# gpschanger

Make a USB-connected iPhone report a fake location — and actually *move*
between two points along real roads at a believable pace.

Click A and B on a map, pick **Walk** or **Drive**, set a speed range. The app
finds a real road route, then feeds the phone one position per second: braking
for traffic lights, waiting at them, pulling away again. When it reaches B it
stays there until you stop it.

Linux. No account, no subscription, no API keys.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/ITSMEFRTRUST/gpschanger
cd gpschanger
python3 -m venv venv
./venv/bin/pip install -e ".[dev]"
pipx install pymobiledevice3
```

### 2. Set up the iPhone (once)

Plug it in over USB and confirm it is seen:

```bash
pymobiledevice3 usbmux list
```

Turn on Developer Mode. If the phone has a passcode, the CLI can't do this for
you — it can only reveal the toggle:

```bash
pymobiledevice3 amfi reveal-developer-mode
```

Then on the phone: **Settings → Privacy & Security → Developer Mode → on**, and
let it restart.

Finally, mount the developer disk image:

```bash
pymobiledevice3 mounter auto-mount
```

> Re-run `auto-mount` after every phone reboot. The app also does it for you
> each time you press Start.

### 3. Run

```bash
./venv/bin/python -m gpschanger.server
```

Open **http://127.0.0.1:8770/**.

---

## Using it

1. **Find your area** — search a street in the box top-left, or just pan
2. **Click once** on the map to drop point **A**
3. **Click again** to drop point **B**
4. **Pick a mode** — Walk or Drive — and a speed range
5. **Go** — draws the road route it will follow
6. **Start walking** — the phone begins moving (first run takes ~4s to open the tunnel)
7. **Stop** — the phone returns to its real GPS

Unplugging the cable or quitting the app also returns it to real GPS,
immediately.

---

## Modes

| Mode  | Routing       | Default speed | Speed changes every |
|-------|---------------|---------------|---------------------|
| Walk  | `routed-foot` | 4–6 km/h      | 3 s                 |
| Drive | `routed-car`  | 40–90 km/h    | 8 s                 |

Speed is re-rolled inside your range rather than held constant, because a
perfectly constant speed is the easiest way to spot a fake.

### Traffic (drive mode)

Drive mode slows and stops at the traffic controls actually on your route, so
it doesn't glide through every junction at 70 km/h.

| Control        | Stops  | Waits  | If it doesn't stop |
|----------------|--------|--------|--------------------|
| Traffic light  | 60%    | 8–40 s | rolls at 25 km/h   |
| Crossing       | 5%     | 2–6 s  | 30 km/h            |
| Stop sign      | never  | —      | rolls at 25 km/h   |
| Give way       | never  | —      | 20 km/h            |
| Mini-roundabout| —      | —      | ignored            |
| Turns          | never  | —      | 50–60 km/h         |

Lights are deliberately probabilistic: one that is *always* red looks as fake
as one that is never red. Gentle turns are ignored — you don't lift off for a
lane-width curve.

This is one driving style, not road law. It's a table at the top of
`gpschanger/pacer.py` — edit it.

**Result:** a 5.4 km city route plans about 9 stops and averages 33 km/h,
versus the 65 km/h a constant-speed run would show.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `usbmux list` is empty | Unlock the phone and tap **Trust** |
| `InvalidServiceError` | Developer disk image isn't mounted — run `mounter auto-mount` |
| `Cannot enable developer-mode when passcode is set` | Expected. Use `reveal-developer-mode` and toggle it on the phone |
| Location snaps back after a second | Something is only sending one position. This app re-sends at 1 Hz; see the note below |
| Routing fails with an error | The public OSRM instance is down or rate-limiting (1 req/s) |
| Map is blank | WebGL unavailable — switch to a raster basemap in the layer picker |

---

## How it works

```
Leaflet + MapLibre UI
        │
      Flask ──► OSRM       find the road route
            ──► Overpass   which junctions have lights / stop signs
            ──► pacer      route + speed range → one position per second
            ──► device     one USB tunnel, positions sent at 1 Hz
```

`pacer.py` holds all the interesting logic — the speed and braking model — and
is pure Python with no network or device dependency, so it's the easy place to
experiment.

```bash
./venv/bin/pytest        # 116 tests, no phone or network required
```

Routes are also written to `routes/` as GPX for inspection; nothing reads them
back.

---

## What this does **not** fake

Only the phone's reported GPS position, on the one phone plugged in. Everything
else still tells the truth:

- **Your IP address** — anything locating you by IP sees your real connection,
  directly contradicting the fake GPS
- **Motion sensors** — the accelerometer, gyroscope and barometer correctly
  report a phone lying still while it claims to be doing 60 km/h
- **Nearby Wi-Fi networks**, which iOS still sees
- **Your other devices**, which compute their own real position

These are structural, not bugs waiting to be fixed.

---

## Notes if you're modifying it

A few findings that cost real time to discover:

- **On iOS 27, setting a location is one-shot.** The device acknowledges it and
  the dot moves, then CoreLocation reverts within seconds — *even with the
  process alive and the channel open*. Holding a position means re-sending it
  at ~1 Hz. This is why `simulate-location play` can't hold at the destination:
  it sends the last point and returns.
- **Use one tunnel per run.** Opening the userspace tunnel takes 3–4 seconds,
  so moving and holding share a single tunnel and channel.
- **Braking needs a discrete-time-safe limit**, not `sqrt(2·a·d)`. The textbook
  curve is only evaluated once per tick, so at 1 Hz the car overshoots it and
  the final sample before a stop dumps ~7 m/s in one second — an obvious tell.
- **Ask Overpass by node ID, not by geometry.** OSRM's `annotations=nodes` gives
  the exact nodes on the route; an `around:` query on the polyline is far slower
  and matches junctions on parallel streets.
- **Each OSRM profile has its own host.** `routed-foot` serves `foot`,
  `routed-car` serves `driving`. Never use `router.project-osrm.org` — it
  accepts foot profiles and silently returns car routing.
- **Leaflet can report longitudes beyond ±180** once the map is panned across a
  world copy, which OSRM rejects. Fold them back.
- **Pin `@maplibre/maplibre-gl-leaflet` ≥ 0.1.4.** Older versions don't keep the
  GL canvas in sync with Leaflet.
- **Don't measure braking from the output points.** The straight line between
  two 1-second samples is shorter than the curve through a corner, so a few
  samples per route look like hard braking that never happened.

Routing, geocoding and traffic data come from FOSSGIS OSRM, Photon and
Overpass — free, community-run services with a 1 request/second courtesy limit.
Please keep the identifying `USER_AGENT`.

---

## Legal

For research and for testing your own applications. Falsifying your location
breaks the terms of service of many apps — social networks and location-based
games in particular detect it and act on accounts. What you do with it is on
you.

## Licence

GPL-3.0-or-later, because it links `pymobiledevice3`, which is GPL-3.0-or-later.
