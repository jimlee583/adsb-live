# Architecture and Roadmap

## 1. Current application

`adsb-live` is a local desktop RF waterfall for a FlightAware or RTL-SDR
receiver tuned near 1090 MHz. Its current purpose is to show raw spectrum
energy so an operator can verify the antenna, gain, and RF environment.

At runtime, the application:

- opens an RTL-SDR receiver or a synthetic demo source;
- reads complex I/Q samples at 2.4 MSPS by default;
- computes Hann-windowed FFTs and converts them to dB power rows;
- displays an instantaneous power spectrum and a decaying peak-hold trace;
- renders a scrolling, max-hold waterfall;
- removes the receiver's center-frequency DC spike from the display;
- automatically calibrates display levels after approximately 400 FFT hops;
- reports live spectrum statistics and receiver configuration.

The application does not currently decode Mode-S or ADS-B messages. It
therefore has no aircraft list, geographic map, network API, or persistent
aircraft history.

## 2. Architecture

The application is a single-process Python desktop program. It does not yet
have separate web frontend and backend services.

### Data-ingestion layer

`src/adsb_live/sdr.py` provides two threaded sample sources:

- `RtlSdrSource` opens a physical RTL-SDR through `pyrtlsdr`.
- `DemoSource` generates synthetic noise, tones, and short pulses for
  development without hardware.

Both sources inherit from `_BaseSource`. Samples are divided into FFT-sized
hops, transformed by `SpectrumComputer`, and emitted as `SpectrumRow`
instances.

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
FFT hops into waterfall rows.

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

Live aircraft data does not currently enter the system. The receiver supplies
raw I/Q samples rather than decoded aircraft records.

The current hardware path is:

1. `RtlSdrSource` opens the selected RTL-SDR device.
2. It configures sample rate, center frequency, and tuner gain.
3. It repeatedly calls `read_samples()`.
4. `_emit_hops()` divides each read into FFT-sized blocks.
5. `SpectrumComputer.compute()` creates one spectrum row per block.
6. The rows enter `DropOldestQueue`.
7. `WaterfallWindow` drains and renders them.

Only one process can normally own an RTL-SDR device. A future decoder cannot
open the same receiver independently while this application is using it.
Practical integration options are:

- decode Mode-S directly from the same in-process I/Q stream;
- let `dump1090` own the receiver and ingest its Beast, SBS, or JSON output;
- introduce an external I/Q multiplexer and have both consumers subscribe.

The second option is likely the fastest route to reliable aircraft data,
because mature decoders already handle Mode-S demodulation, CPR position
decoding, and message validation.

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

There is no geographic map in the current application.

The existing spectrum display has two linked PyQtGraph plots:

- The upper plot draws the latest PSD and a slowly decaying peak-hold curve.
- The lower plot uses an `ImageItem` with the viridis color map to display a
  scrolling waterfall.

The waterfall stores a two-dimensional NumPy array whose rows represent time
and whose columns represent frequency. New rows are placed at index zero and
the Y axis is inverted, so the newest data appears at the top.

Several FFT hops are combined using a per-bin maximum before a row is
displayed. This preserves short ADS-B bursts that would otherwise disappear
if quiet hops were averaged with them.

A future aircraft map should be a separate view driven by `AircraftTrack`
positions. It should not be implemented as an extension of the waterfall
image.

## 6. Tests and deployment

### Tests

The repository currently has no test directory, test suite, or test-runner
configuration. There is also no configured linting or static type-checking
workflow.

The first unit tests should cover:

- `SpectrumComputer` input validation, frequency bins, and tone placement;
- `DropOldestQueue` ordering and overflow behavior;
- CLI validation for gains, positive values, powers of two, and color levels;
- source lifecycle and error propagation;
- deterministic spectrum production by `DemoSource`;
- max-hold and DC-notch behavior extracted from UI state into testable helpers.

Qt smoke tests can run with an offscreen platform and the demo source. Tests
that require an actual RTL-SDR should be explicitly marked as hardware tests
and excluded from normal CI.

### Deployment

Deployment is local and developer-oriented:

```bash
uv sync
uv run adsb-live
```

The project uses Python 3.12 or newer, Hatchling for packaging, and `uv.lock`
for dependency locking. `pyrtlsdr[lib]` normally supplies a compatible
`librtlsdr`; systems where that fails require a native library installation.

There is currently no:

- continuous-integration workflow;
- Docker image or Compose configuration;
- system service definition;
- signed desktop bundle or installer;
- release automation;
- headless server deployment.

## 7. Recommended next features

### Feature 1: decoded ADS-B ingestion and tracking

Add a decoder integration, preferably by first ingesting a mature decoder's
Beast, SBS, or JSON output. Normalize messages into `AircraftTrack` records,
merge updates by ICAO address, perform stale-track expiry, and surface decoder
health.

This feature provides the data foundation for the aircraft table, geographic
map, persistence, and browser dashboard.

### Feature 2: live aircraft table

Add a Qt table showing ICAO address, callsign, altitude, speed, heading,
distance, bearing, signal level, and age. Include sorting, filtering, stale
indicators, and selection.

The table can be developed against a mock `TrackStore` while decoder work is
still in progress.

### Feature 3: geographic aircraft map

Add a separate map view with aircraft icons, heading, trails, selection,
receiver position, and range rings. Selecting an aircraft should synchronize
the map and table.

The basic map UI can be developed using fake tracks. Real integration depends
on the shared `AircraftTrack` contract and decoded positions.

### Feature 4: shared event layer and browser dashboard

Introduce a small event or snapshot interface for spectrum and aircraft data,
then add a local HTTP and WebSocket service. A browser dashboard could expose
the waterfall, aircraft table, and map to other devices without allowing a
second process to open the receiver.

The transport can begin with spectrum data, but aircraft endpoints should use
the same `TrackStore` as the desktop views.

### Feature 5: automated quality and delivery pipeline

Add pytest, formatting, linting, type checking, and CI for supported Python
versions. Use demo mode for hardware-independent smoke tests. Add build and
release checks once application packaging is selected.

This work protects the current RF display while the data model and UI grow.

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
