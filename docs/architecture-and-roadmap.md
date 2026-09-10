# Architecture and Roadmap

## 1. Current application

`adsb-live` is a local desktop RF waterfall and optional Mode-S / ADS-B
decoder for a FlightAware or RTL-SDR receiver tuned near 1090 MHz. It
first exists to show raw spectrum energy so an operator can verify the
antenna, gain, and RF environment. With `--decode` it additionally
demodulates ADS-B messages and shows a live aircraft table.

At runtime, the application:

- opens an RTL-SDR receiver or a synthetic demo source;
- reads complex I/Q samples at 2.4 MSPS by default;
- computes Hann-windowed FFTs and converts them to dB power rows;
- displays an instantaneous power spectrum and a decaying peak-hold trace;
- renders a scrolling, max-hold waterfall;
- removes the receiver's center-frequency DC spike from the display;
- automatically calibrates display levels after approximately 400 FFT hops;
- reports live spectrum statistics and receiver configuration;
- when `--decode` is passed, tees the raw UC8 byte stream to a child
  `dump1090` process, ingests its `aircraft.json`, and displays a live
  aircraft table alongside the waterfall;
- when `--decode` is combined with `--lat`/`--lon`, also renders a
  receiver-centered polar map with range rings, altitude-colored
  aircraft dots, and per-aircraft trails sourced from the bounded
  position history maintained by `TrackStore`.

The application currently has no external geographic map tiles and no
network API. Live aircraft state (including trails and telemetry) lives
in the in-memory `TrackStore`; when the CLI is passed `--record`, each
successful poll is also appended to a SQLite session file for later
`--replay`.

## 2. Architecture

The application is a single-process Python desktop program. It does not yet
have separate web frontend and backend services.

### Data-ingestion layer

`src/adsb_live/sdr.py` provides two threaded sample sources:

- `RtlSdrSource` opens a physical RTL-SDR through `pyrtlsdr`. When an
  optional `iq_sink` (a `DropOldestByteQueue`) is passed in, it also
  publishes the raw UC8 byte block for each USB read before converting it
  to complex I/Q. This is the tap point the decoder consumes.
- `DemoSource` generates synthetic noise, tones, and short pulses for
  development without hardware.

Both sources inherit from `_BaseSource`. Samples are divided into FFT-sized
hops, transformed by `SpectrumComputer`, and emitted as `SpectrumRow`
instances.

### Decoder pipeline

`src/adsb_live/decoder.py` supervises an external `dump1090` process:

- `Dump1090Process` spawns `dump1090 --ifile - --iformat UC8 --write-json`,
  writes byte blocks from the `iq_sink` into the child's stdin, drains its
  stderr into a bounded ring buffer, and restarts with linear backoff if
  it exits unexpectedly.
- `LiveDecoder` bundles the child process, a private `aircraft.json`
  tempdir, a `Dump1090JsonSource` poller, and a shared `TrackStore`. The
  CLI constructs one per `--decode` invocation and hands it to the UI.

`src/adsb_live/dump1090.py` handles JSON parsing and polling (both a file
reader and an HTTP reader are supplied). Aircraft records become
`AircraftTrack` values which are upserted into a `TrackStore`.

### Processing layer

`src/adsb_live/spectrum.py` contains `SpectrumComputer`. It:

1. validates that the FFT size is a positive power of two;
2. applies a Hann window;
3. computes and shifts the FFT;
4. normalizes magnitude by the window gain;
5. converts magnitude to dB;
6. produces the matching frequency axis.

This processing produces relative spectrum levels. It is not calibrated RF
power in dBm.

### In-process transport

`DropOldestQueue` connects the source thread to the Qt UI thread. The queue is
bounded and discards its oldest entry when full. This prevents a slow UI from
blocking radio acquisition, at the cost of losing old rows under load.

### Frontend

`src/adsb_live/ui.py` implements a PySide6 desktop window with PyQtGraph. A Qt
timer drains batches from the queue, updates spectrum plots, and folds multiple
FFT hops into waterfall rows. When a `LiveDecoder` is passed in, the window
also mounts an `AircraftTableModel` in a `QSortFilterProxyModel` and refreshes
it on a separate ~1 Hz timer, decoupled from the 33 ms spectrum tick. Column
definitions and row-formatting helpers live in `src/adsb_live/aircraft_columns.py`,
which is Qt-free so it can be exercised in tests without a display.

When receiver coordinates are known, the window swaps its two-pane
splitter for a three-pane `[graphics, map, table]` layout and mounts an
`AircraftMapWidget` from `src/adsb_live/map_view.py`. The map is a
`pg.PlotWidget` in receiver-centered nautical-mile coordinates: aircraft
dots are colored by altitude, trails are drawn from
`TrackStore.histories()`, and the outer view snaps to the smallest of a
fixed set of range rings that contains every visible aircraft. A
`_SelectionCoordinator` keeps the table's current row, the map's
highlighted aircraft, and the detail pane's plotted history in sync,
using an `_applying` guard to break the callback loop. `map_view.py`
also exposes pure helpers (`polar_xy`, `altitude_color`,
`auto_range_nm`) that are testable without Qt.

Underneath the top row sits a resizable `AircraftDetailWidget` strip
from `src/adsb_live/aircraft_detail.py`. When an aircraft is selected
(from the table or by clicking the map) it plots three linked
time-series curves for that ICAO: altitude, ground speed, and vertical
rate. The data comes from `TrackStore.telemetry(icao)`, a per-ICAO
`TelemetrySample` ring buffer that appends one sample per snapshot;
the widget itself holds no state beyond the currently selected ICAO.
`aircraft_detail.py` also exposes a Qt-free `telemetry_series` helper
that reshapes samples into three plot-ready NumPy arrays and skips
`None` gaps on a per-series basis.

### Application entry point

`src/adsb_live/cli.py` owns argument parsing, source selection, Qt startup, and
shutdown. The installed command is:

```text
adsb-live = adsb_live.cli:main
```

The main data flow is:

```text
RTL-SDR or DemoSource
        |
        v
background source thread
        |
        v
SpectrumComputer
        |
        v
DropOldestQueue[SpectrumRow]
        |
        v
Qt timer / WaterfallWindow
        |
        v
PSD and waterfall display
```

## 3. How live aircraft data enters the system

`adsb-live` keeps exclusive ownership of the RTL-SDR device and multiplexes
the raw byte stream so both the waterfall and the decoder run in the same
invocation. The `--decode` data path is:

1. `RtlSdrSource` opens the selected RTL-SDR device and configures sample
   rate, center frequency, and tuner gain.
2. It repeatedly calls `sdr.read_bytes(2 * chunk)`, receiving interleaved
   `uint8` UC8 samples.
3. Each raw block is published to the `iq_sink` (a `DropOldestByteQueue`)
   for the decoder. If the decoder falls behind, the oldest block is
   dropped so the reader thread never blocks.
4. `sdr.packed_bytes_to_iq()` converts the same block to complex I/Q,
   `_emit_hops()` splits it into FFT-sized blocks, and `SpectrumComputer`
   produces one `SpectrumRow` per hop, which flows into `DropOldestQueue`
   and the waterfall.
5. `Dump1090Process` consumes the byte queue and writes each block to its
   child `dump1090 --ifile - --iformat UC8` on stdin.
6. The child periodically writes `aircraft.json` into a private tempdir.
7. `Dump1090JsonSource` polls that file, normalizes each record into an
   `AircraftTrack`, and upserts it into the shared `TrackStore`.
8. `AircraftTableModel` reads a `TrackStore.snapshot()` on a ~1 Hz timer
   and refreshes the table view.

Only one process can normally own an RTL-SDR device. A future in-process
decoder or a networked receiver would replace or augment step 5, but the
byte-queue tap between steps 3 and 4 is the stable integration point.

## 4. Important models and APIs

### Existing models

`SpectrumRow` is the only domain data record. It contains:

- `timestamp`: monotonic acquisition time;
- `center_freq`: receiver center frequency in Hz;
- `sample_rate`: samples per second;
- `power_db`: a float32 NumPy array with one dB value per FFT bin.

`DropOldestQueue` exposes:

- `put(item)` to enqueue a row without blocking;
- `get_batch(max_items=None)` to drain available rows.

`SpectrumComputer` exposes:

- `compute(samples)` to produce a dB spectrum row;
- `frequency_axis(center_freq, sample_rate)` to produce corresponding Hz bins.

The source abstraction exposes common configuration, a `stop()` operation,
and an `error` property. `RtlSdrSource` additionally exposes its configured
gain.

### Existing external API

The command-line interface is the only external API. Its major options include
center frequency, sample rate, tuner gain, FFT size, device index, waterfall
history, time window, refresh rate, fixed color levels, gain discovery, and
demo mode.

There is no REST, WebSocket, gRPC, OpenAPI, database, or formal plugin API.

### Recommended future models

Before adding multiple aircraft views, define a stable shared record such as:

```text
AircraftTrack
  icao
  callsign
  latitude
  longitude
  altitude_ft
  ground_speed_kt
  track_deg
  vertical_rate_fpm
  squawk
  category
  signal_db
  first_seen
  last_seen
```

Add a `TrackStore` responsible for upsert, snapshot, and stale-track expiry.
The aircraft table, map, and browser API should all consume this store rather
than maintaining independent tracking state.

## 5. Rendering

The window can host up to three panes in the top row plus a
resizable detail strip below:

```text
[ spectrum + waterfall ]  [ polar aircraft map ]  [ aircraft table ]
[ selected aircraft detail (altitude / ground speed / vertical rate) ]
```

The top row and detail strip are wired together with a vertical
`QSplitter` so users can drag the divider up (more waterfall) or down
(more history plot area).

The spectrum display has two linked PyQtGraph plots:

- The upper plot draws the latest PSD and a slowly decaying peak-hold curve.
- The lower plot uses an `ImageItem` with the viridis color map to display a
  scrolling waterfall.

The waterfall stores a two-dimensional NumPy array whose rows represent time
and whose columns represent frequency. New rows are placed at index zero and
the Y axis is inverted, so the newest data appears at the top.

Several FFT hops are combined using a per-bin maximum before a row is
displayed. This preserves short ADS-B bursts that would otherwise disappear
if quiet hops were averaged with them.

The polar aircraft map is a plain `pg.PlotWidget` with the receiver at
`(0, 0)`, north up, east right, distances in nautical miles. It does not
render map tiles and has no network dependency. Range rings (10, 25, 50,
100, 200 nm) auto-hide above the current outer range. Trails come from
`TrackStore.histories()`, so history bookkeeping stays in the store and
the view is stateless between ticks aside from cached Qt items.

## 6. Tests and deployment

### Tests

The `tests/` directory contains a hardware-free pytest suite that runs on
GitHub Actions with Python 3.12. Coverage:

- `test_spectrum.py`: FFT input validation, frequency bins, tone placement.
- `test_sdr.py`: `DropOldestQueue`/`DropOldestByteQueue` ordering and
  overflow, `DemoSource` lifecycle, and the `RtlSdrSource._process_bytes`
  byte-tap contract (using a fake `sdr` object).
- `test_dump1090.py`: `aircraft.json` parsing, HTTP/file readers, and the
  polling source with fixture snapshots.
- `test_tracks.py`: `AircraftTrack` validation, `TrackStore` upsert,
  stale-expiry, field-level merge semantics (partial-snapshot
  preservation, per-field aging, position-dropout trail continuity,
  `first_seen` collapse across merges), and per-ICAO `TelemetrySample`
  ring-buffer semantics.
- `test_decoder.py`: `Dump1090Process` argv construction, PATH
  resolution, restart accounting, stderr capture, and `LiveDecoder`
  tempdir lifecycle, all driven against a small Python fake instead of
  the real `dump1090` binary.
- `test_aircraft_columns.py`: pure column formatting, age/distance
  rendering, and sort keys.
- `test_map_view.py`: pure helpers (`polar_xy` orientation,
  `altitude_color` bands, `auto_range_nm` snapping) and an offscreen-Qt
  smoke test that constructs `AircraftMapWidget`, refreshes it against
  an in-memory `TrackStore`, and exercises bidirectional selection.
- `test_aircraft_detail.py`: pure `telemetry_series` reshape helper
  (X-axis-shift-to-now behavior and per-series `None`-gap dropping)
  plus offscreen-Qt smoke tests that construct
  `AircraftDetailWidget`, seed a `TrackStore` with telemetry, and
  exercise the selection / empty / missing / switch-selection paths.
- `test_ui.py`: offscreen `WaterfallWindow` construction paths --
  decoder with and without receiver coordinates, and that
  `_SelectionCoordinator` drives the detail pane's `selected_icao`.
- `test_cli.py`: CLI validation paths (levels, `--decode` compatibility,
  `--lat`/`--lon` pairing, and the `--record` / `--replay` mutual
  exclusions) that never import Qt, plus a hint check for `--decode`
  without a receiver.
- `test_session.py`: `SessionRecorder` schema and JSON round-trip, the
  ``Dump1090JsonSource.on_poll`` hook (called on success, skipped on
  parse failure, and safe against a raising sink), and `ReplaySource`
  end-to-end -- timestamp rebase onto a fresh monotonic clock, freeze
  behavior after the final snapshot, receiver override, and health
  reporting.

Qt smoke tests can run with an offscreen platform and the demo source but
are not part of the CI suite today. Tests that require an actual RTL-SDR
or an installed `dump1090` binary must be marked as hardware tests and
excluded from CI.

### Deployment

Deployment is local and developer-oriented:

```bash
uv sync
uv run adsb-live
```

The project uses Python 3.12 or newer, Hatchling for packaging, and `uv.lock`
for dependency locking. `pyrtlsdr[lib]` normally supplies a compatible
`librtlsdr`; systems where that fails require a native library installation.

GitHub Actions (`.github/workflows/tests.yml`) runs `uv run pytest` on
push and PR against Python 3.12 on `ubuntu-latest`.

There is currently no:

- Docker image or Compose configuration;
- system service definition;
- signed desktop bundle or installer;
- release automation;
- headless server deployment.

## 7. Recommended next features

### Feature 1: decoded ADS-B ingestion and tracking — done

Implemented in `src/adsb_live/decoder.py` (child `dump1090` supervisor and
`LiveDecoder`), consumed by the CLI `--decode` flag and surfaced in the UI
status bar.

### Feature 2: live aircraft table — done

Implemented as `AircraftTableModel` in `src/adsb_live/ui.py` with pure
formatting helpers in `src/adsb_live/aircraft_columns.py`. Columns include
ICAO, callsign, altitude, ground speed, track, vertical rate, squawk,
position, distance/bearing (when `--lat`/`--lon` is set), RSSI, and age.

### Feature 3: geographic aircraft map — polar view done

Implemented as `AircraftMapWidget` in `src/adsb_live/map_view.py` with a
bounded `PositionSample` history maintained inside `TrackStore`. The
current view is a receiver-centered polar radar scope with range rings,
altitude-colored dots, per-aircraft trails, and bidirectional
table/map selection wired through a small `_SelectionCoordinator`.

Real geographic map tiles remain future work: the natural upgrade path
is a `QWebEngineView` running Leaflet against the same `store.histories()`
snapshot over a local WebSocket, which would coexist with the polar view
as a low-latency default.

### Feature 4: shared event layer and browser dashboard

Introduce a small event or snapshot interface for spectrum and aircraft data,
then add a local HTTP and WebSocket service. A browser dashboard could expose
the waterfall, aircraft table, and map to other devices without allowing a
second process to open the receiver.

The transport can begin with spectrum data, but aircraft endpoints should use
the same `TrackStore` as the desktop views.

### Feature 5: automated quality and delivery pipeline

pytest and CI are in place. Formatting, linting, type checking, and release
packaging are still outstanding. Use demo mode and the fake-decoder
fixture pattern for hardware-independent smoke tests. Add build and
release checks once application packaging is selected.

This work protects the current RF display while the data model and UI grow.

### Feature 6: `TrackStore` field-level merges — done

`TrackStore.upsert` now merges each incoming `AircraftTrack` with the
previously stored one instead of replacing it wholesale. An incoming
`None` on any optional field means "not observed in this snapshot" and
preserves the previously observed value; a non-`None` value wins
outright and refreshes its per-field observed-at timestamp. Preserved
values age out after `field_stale_after_s` (default 30 s) so a callsign
observed once minutes ago cannot linger indefinitely. `first_seen`
collapses to the earliest observation across all merges.

Position history bookkeeping is unchanged: a preserved lat/lon from a
previous snapshot is *not* appended to the trail again, so trails
continue as a single point through weak-signal position dropouts
instead of accumulating duplicates. A module-level guard
(`_assert_mergeable_fields_cover_track`) fails import if a new optional
field is added to `AircraftTrack` without being listed in
`_MERGEABLE_FIELDS`, preventing silent regressions to snapshot-replace
semantics for new columns.

### Feature 7: selected-aircraft detail pane — done

`AircraftDetailWidget` in `src/adsb_live/aircraft_detail.py` mounts a
resizable strip beneath the top row that plots altitude, ground speed,
and vertical rate for the currently selected aircraft. Data comes from
`TrackStore.telemetry(icao)`, a per-ICAO `TelemetrySample` ring buffer
(default `telemetry_size=1200` samples ≈ 20 minutes at 1 Hz) that
appends one sample per `TrackStore.upsert`. Because the buffer is
populated from the *merged* track state, plots stay flat through
transient dropouts instead of introducing spurious gaps. The widget
itself holds no state beyond the currently selected ICAO -- a
Qt-free `telemetry_series` helper reshapes samples into three
plot-ready NumPy arrays and skips `None` gaps per series.
`_SelectionCoordinator` now routes selections from both the table and
the map to the detail pane in addition to the map highlight.

### Feature 8: session recording and replay — done

`src/adsb_live/session.py` adds a stdlib-only SQLite session format,
`SessionRecorder`, and `ReplaySource`. The CLI grows two flags:

- `--record PATH` (requires `--decode`): after each successful
  `Dump1090JsonSource.poll_once`, the recorder appends
  `store.snapshot()` to a SQLite file. Each row stores
  ``elapsed_s`` (monotonic offset from recorder start) and a JSON
  array of the merged tracks; timestamps are persisted as offsets so
  they can be remapped onto a fresh monotonic clock during replay.
  Refuses to overwrite an existing file.
- `--replay PATH`: opens the file and constructs a `ReplaySource` that
  duck-types `LiveDecoder` for `WaterfallWindow` (same `store`,
  `process`, `poller`, `iq_sink`, and `stop()` surface). A background
  thread waits according to each snapshot's ``elapsed_s`` and upserts
  it into a fresh `TrackStore`, so the table, map, trails, telemetry,
  and detail pane all reproduce the recorded state. After the final
  snapshot the source re-applies it at a slow cadence with freshened
  ``last_seen`` values so tracks do not expire while the window is
  still open. Replay uses `DemoSource` for the RF panel; `--lat`/`--lon`
  override the recorded receiver location.

The recording hook enters through a new optional
``on_poll: Callable[[TrackStore], None]`` parameter on
`Dump1090JsonSource`; hook errors are swallowed so a broken sink cannot
kill live decoding.

## Parallel development plan

The work should begin with a short shared-contract phase:

1. Define `AircraftTrack`, update semantics, stale expiry, and `TrackStore`.
2. Define the interface between an ingestion adapter and the track store.
3. Add representative fake-track fixtures.

After those contracts are agreed, the following tracks can proceed
independently:

- **Track A — decoding and ingestion:** decoder adapter, parsing, normalization,
  and track updates.
- **Track B — aircraft table:** Qt model/view implementation using fake tracks.
- **Track C — map:** geographic rendering and selection using fake tracks.
- **Track D — quality pipeline:** unit tests, linting, type checking, and CI.
- **Track E — event layer:** event/snapshot abstraction and spectrum streaming.

Track D is fully independent and can begin immediately. Tracks B and C can
also start immediately if they depend only on the agreed model and fake
fixtures. Track E can stream spectrum rows before aircraft decoding exists,
but its aircraft API depends on the shared track contract.

Final integration should occur in this order:

1. connect Track A to the `TrackStore`;
2. replace fake data in the table and map with store snapshots;
3. synchronize table and map selection;
4. expose the same store through the browser API;
5. add end-to-end demo and decoder-fixture tests.

Do not assign separate workstreams that independently open the same physical
RTL-SDR. The receiver remains an exclusive resource, so all consumers must
share one acquisition owner or consume data from an external decoder.

## Suggested implementation sequence

For one developer:

1. tests and CI;
2. shared aircraft model and track store;
3. decoder ingestion;
4. aircraft table;
5. geographic map;
6. browser dashboard.

For multiple developers:

1. agree on the aircraft and track-store contracts;
2. develop decoder ingestion, table, map, and tests in parallel;
3. integrate real tracks into desktop views;
4. develop the browser transport and dashboard against the same store.
