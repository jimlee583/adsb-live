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
# Explicit tuning + fixed gain
uv run adsb-live --freq 1090e6 --rate 2.4e6 --gain 40 --fft-size 4096

# Try the app without hardware attached
uv run adsb-live --demo
```

All flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--freq` | `1090e6` | Center frequency (Hz) |
| `--rate` | `2.4e6` | Sample rate (Hz) |
| `--gain` | `auto` | Tuner gain in dB, or `auto` |
| `--fft-size` | `2048` | FFT size per hop (power of two) |
| `--device-index` | `0` | RTL-SDR device index |
| `--history` | `400` | Waterfall rows kept on screen |
| `--refresh-ms` | `33` | UI refresh period (~30 Hz) |
| `--demo` | off | Use a synthetic signal source |

## What you should see

- **Top panel** — instantaneous power spectral density (blue) with a slowly
  decaying peak-hold trace (orange, dashed). The vertical dotted line marks
  the center frequency.
- **Bottom panel** — scrolling waterfall, newest row at the top, viridis
  colormap. Bright transient blobs near the center are ADS-B bursts from
  nearby aircraft.

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
