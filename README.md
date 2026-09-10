# adsb-live

Realtime RF waterfall for a FlightAware / RTL-SDR receiver tuned around
**1090 MHz** (ADS-B). Shows the raw spectrum so you can verify the antenna,
gain, and RF environment, and — with `--decode` — pipes the same live I/Q
into a child `dump1090` process to demodulate ADS-B messages and display a
live aircraft table next to the waterfall.

<img alt="PSD on top, scrolling waterfall below" src="docs/screenshot.png" width="640"/>

## Requirements

- macOS, Linux, or Windows
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (this project is managed with uv)
- A FlightAware Pro Stick / Pro Stick Plus (or any RTL2832U dongle) plugged in

The `pyrtlsdr[lib]` dependency ships prebuilt `librtlsdr` binaries via
`pyrtlsdrlib`, so no `brew install` is needed for the common case. If the
bundled library fails to load on your system, install the native one:

```bash
brew install librtlsdr        # macOS
sudo apt install librtlsdr0   # Debian / Ubuntu
```

> **Only one program can hold the dongle at a time.** Stop `dump1090`,
> `piaware`, SDR#, GQRX, or anything else that opens the RTL-SDR before
> starting `adsb-live`, otherwise the reader thread will report a device-busy
> error.

## Install / update

```bash
uv sync
```

## Run

```bash
uv run adsb-live
```

Common overrides:

```bash
# Explicit tuning + fixed gain (49.6 dB is the R820T2 max, best for ADS-B)
uv run adsb-live --freq 1090e6 --rate 2.4e6 --gain 49.6 --fft-size 4096

# Pin the color scale instead of using auto-calibration
uv run adsb-live --vmin -85 --vmax -25

# List all supported tuner gain steps for the connected dongle
uv run adsb-live --list-gains

# Try the app without hardware attached
uv run adsb-live --demo
```

All flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--freq` | `1090e6` | Center frequency (Hz) |
| `--rate` | `2.4e6` | Sample rate (Hz) |
| `--gain` | `max` | `max`, `agc`, or a value in dB (snapped to nearest step) |
| `--fft-size` | `256` | FFT size per hop (power of two); small = short hops = bright bursts |
| `--hops-per-read` | `16` | FFT hops per USB call (larger = less driver overhead) |
| `--device-index` | `0` | RTL-SDR device index |
| `--history` | `500` | Waterfall rows kept on screen |
| `--time-window` | `5.0` | Seconds of history the waterfall spans (max-hold folds hops) |
| `--refresh-ms` | `33` | UI refresh period (~30 Hz) |
| `--vmin` / `--vmax` | auto | Pin color-scale range in dB (disables auto-calibration) |
| `--list-gains` | off | Print supported tuner gains and exit |
| `--demo` | off | Use a synthetic signal source |
| `--decode` | off | Also demodulate ADS-B via a child `dump1090` process |
| `--dump1090-path` | auto | Explicit path to `dump1090` (defaults to `$PATH`) |
| `--lat` / `--lon` | none | Receiver location for range/bearing and surface positions |
| `--stale-after` | `60.0` | Drop tracks idle for this many seconds |
| `--decode-poll-interval` | `1.0` | Seconds between `aircraft.json` re-reads |
| `--record` | off | Save decoded aircraft snapshots to a SQLite session file (requires `--decode`) |
| `--replay` | off | Replay a previously recorded session file; drives the aircraft views without a dongle or `dump1090` |

> `--gain max` (the default) picks the highest supported tuner gain (typically
> 49.6 dB on an R820T2). This is what FlightAware recommends for ADS-B. The
> older `--gain auto` alias still works and is treated as `max`; use
> `--gain agc` if you specifically want the tuner's built-in automatic gain
> (usually worse for weak ADS-B bursts).

## Decoding ADS-B messages

Pass `--decode` to demodulate real Mode-S / ADS-B messages alongside the
waterfall. `adsb-live` keeps exclusive ownership of the dongle and tees the
raw UC8 I/Q byte stream into a child `dump1090 --ifile - --iformat UC8`
process, then polls the child's `aircraft.json` output. The main window
grows an aircraft table (ICAO, callsign, altitude, speed, heading, position,
distance/bearing when `--lat`/`--lon` is set, RSSI, and age).

Requirements:

```bash
brew install dump1090                     # macOS (dump1090-fa)
sudo apt install dump1090-fa              # Debian / Ubuntu
```

Example:

```bash
uv run adsb-live --decode --lat 40.0150 --lon -105.2705
```

Notes:

- `--decode` requires `--rate 2.4e6` (dump1090's UC8 pipeline assumes 2.4 MHz).
- `--decode` cannot be combined with `--demo` (synthetic noise contains no
  Mode-S).
- Only one process can hold the dongle. `adsb-live --decode` replaces (does
  not coexist with) a separately running `dump1090`, `piaware`, or SDR#.
- The child process's `aircraft.json` is written to a private temp directory
  and cleaned up on exit; nothing is left behind.

### Aircraft map

When `--decode` is combined with `--lat` and `--lon`, a receiver-centered
polar map appears between the waterfall and the aircraft table. It is a
plain radar-scope view — no map tiles, no network dependency — with the
receiver at the origin, north up, east right, distances in nautical miles.

- **Range rings** at 10, 25, 50, 100, and 200 nm. The outer view snaps to
  the smallest ring that contains every currently tracked aircraft (floor
  50 nm, ceiling 250 nm).
- **Aircraft dots** are colored by altitude (blue < 5k ft, cyan < 15k,
  green < 25k, yellow < 35k, orange < 45k, red above; grey when on the
  ground or altitude is unknown).
- **Trails** show each aircraft's recent path. Trail length is bounded
  per aircraft; the default (`120` samples) is roughly two minutes of
  live positions.
- **Selection is bidirectional**: clicking a dot selects the matching row
  in the aircraft table, and vice versa.

Without `--lat`/`--lon` the map is hidden and the status bar shows
`map=off (needs --lat/--lon)`.

### Selected-aircraft detail pane

Below the top row (waterfall, map, table) sits a resizable strip that
plots **altitude**, **ground speed**, and **vertical rate** vs. time
for whichever aircraft is currently selected. Click a row in the
table or a dot on the map to switch which aircraft is shown; the X
axes on the three plots are linked so panning stays in sync.

- History depth defaults to about 20 minutes at 1 Hz (controlled by
  `TrackStore.telemetry_size`, currently a compile-time constant).
- The header line shows the latest altitude / speed / vertical-rate
  values plus the total time span currently plotted.
- Preserved telemetry from field-level merges keeps the plot flat
  through transient weak-signal dropouts instead of introducing
  spurious gaps.
- Drag the horizontal divider up for more waterfall, or down for
  more plot area.

## Recording and replaying sessions

Pass `--record PATH` alongside `--decode` to save every decoded aircraft
snapshot into a SQLite file (~1 Hz, whatever `--decode-poll-interval`
is). Later, replay it without the dongle or `dump1090`:

```bash
# Live: record while you fly the app.
uv run adsb-live --decode --lat 40.0150 --lon -105.2705 \
    --record ~/adsb-sessions/2026-09-09.db

# Replay: no hardware required. Aircraft table, map, and detail pane
# come back with the tracks, trails, and telemetry you recorded.
uv run adsb-live --replay ~/adsb-sessions/2026-09-09.db
```

Notes:

- `--record` refuses to overwrite an existing file.
- `--record` requires `--decode`; there is nothing to record without live
  decoding.
- `--replay` is incompatible with `--decode`, `--record`, and `--demo`.
- Replay uses a synthetic RF source for the spectrum panel so the
  waterfall keeps scrolling. Aircraft data comes entirely from the file.
- Receiver coordinates are read from the session metadata. Pass
  `--lat`/`--lon` to override (useful when replaying a recording that was
  made without a fixed location).
- After the last snapshot plays, replay freezes on the final aircraft
  state and re-applies it with fresh timestamps so the window keeps
  showing tracks while you look at them. The status bar switches from
  `replay=... (playing)` to `replay=... (done)`.

## What you should see

- **Top panel** — instantaneous power spectral density (blue) with a slowly
  decaying peak-hold trace (orange, dashed). The vertical dotted line marks
  the center frequency. A few bins around center are notched out to hide the
  tuner's DC / LO-leakage spike.
- **Bottom panel** — scrolling waterfall of the last `--time-window` seconds,
  newest row at the top, viridis colormap. Each row is the **per-bin maximum**
  across many raw FFT hops, so a single 120 µs ADS-B burst still lights up a
  full-brightness pixel instead of being averaged away.
- **Status bar** — live `dB: min=… med=… max=…` readout, current color
  levels, and the max-hold ratio (e.g. `47x max-hold`). If bursts are
  present, you should see short bright dashes scattered across the middle of
  the waterfall roughly every fraction of a second, and `max` should
  regularly spike ~15+ dB above `med`.

On startup the color scale is `-100` to `0` dB; after ~400 hops the app
auto-calibrates it to the actual noise floor and peak level. Pass `--vmin`
and `--vmax` to lock the scale to values you like.

Scroll-wheel to zoom, click-drag to pan. The waterfall's frequency axis is
linked to the PSD's.

## Project layout

```
src/adsb_live/
  cli.py                 # argparse + start SDR + Qt app
  sdr.py                 # RTL-SDR reader thread, demo source, drop-oldest queues
  spectrum.py            # Hann-windowed FFT -> dB power row
  decoder.py             # dump1090 child-process supervisor + LiveDecoder
  dump1090.py            # aircraft.json parser + polling ingestion thread
  tracks.py              # AircraftTrack + TrackStore (position + telemetry history)
  session.py             # SQLite SessionRecorder + ReplaySource (record / replay)
  aircraft_columns.py    # Pure column definitions and row formatters (no Qt)
  map_view.py            # Polar projection + AircraftMapWidget
  aircraft_detail.py     # Selected-aircraft altitude/speed/vrate plots
  ui.py                  # PyQtGraph window: PSD + waterfall + map + table + detail
```

## Roadmap

- Real geographic tiles behind the polar map (currently only the polar
  receiver-centered view is implemented)
- Optional browser dashboard sharing the same reader
- Post-hoc analysis tools that read the SQLite session format directly
  (density plots, callsign heatmaps, per-day reports)
