# adsb-live

Realtime RF waterfall for a FlightAware / RTL-SDR receiver tuned around
**1090 MHz** (ADS-B). The first version shows raw spectrum energy so you can
visually confirm the antenna, gain, and RF environment. Decoded aircraft
messages are intentionally out of scope for now.

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

> `--gain max` (the default) picks the highest supported tuner gain (typically
> 49.6 dB on an R820T2). This is what FlightAware recommends for ADS-B. The
> older `--gain auto` alias still works and is treated as `max`; use
> `--gain agc` if you specifically want the tuner's built-in automatic gain
> (usually worse for weak ADS-B bursts).

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
  cli.py        # argparse + start SDR + Qt app
  sdr.py        # RTL-SDR reader thread, demo source, drop-oldest queue
  spectrum.py   # Hann-windowed FFT -> dB power row
  ui.py         # PyQtGraph window: PSD + scrolling waterfall
```

## Roadmap

- Decoded ADS-B message stream (Mode-S / dump1090 integration)
- Aircraft table and map view
- Optional browser dashboard sharing the same reader
