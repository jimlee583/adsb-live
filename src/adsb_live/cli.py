"""Command-line entry point: ``uv run adsb-live``."""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Sequence

from .sdr import DemoSource, DropOldestQueue, RtlSdrSource


def _parse_gain(value: str) -> object:
    key = value.strip().lower()
    if key in ("max", "auto", "agc"):
        return key
    try:
        return float(value)
    except ValueError as exc:  # pragma: no cover - argparse handles display
        raise argparse.ArgumentTypeError(
            "gain must be 'max', 'agc', or a number in dB, "
            f"got {value!r}"
        ) from exc


def _parse_positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0.0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return parsed


def _parse_pow2_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0 or (parsed & (parsed - 1)) != 0:
        raise argparse.ArgumentTypeError(
            f"fft size must be a positive power of two, got {value!r}"
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adsb-live",
        description=(
            "Realtime RF waterfall from a FlightAware / RTL-SDR receiver "
            "tuned around 1090 MHz."
        ),
    )
    parser.add_argument(
        "--freq",
        type=_parse_positive_float,
        default=1090e6,
        help="Center frequency in Hz (default: 1090e6).",
    )
    parser.add_argument(
        "--rate",
        type=_parse_positive_float,
        default=2.4e6,
        help="Sample rate in Hz (default: 2.4e6).",
    )
    parser.add_argument(
        "--gain",
        type=_parse_gain,
        default="max",
        help=(
            "Tuner gain: 'max' (default, best for ADS-B), 'agc' for tuner "
            "auto-gain, or a specific value in dB (snapped to the nearest "
            "supported step)."
        ),
    )
    parser.add_argument(
        "--fft-size",
        type=_parse_pow2_int,
        default=256,
        help=(
            "FFT size per hop; must be a power of two. Small values (128-512) "
            "make short ADS-B bursts pop in the waterfall because each burst "
            "fills a whole hop instead of being averaged with quiet samples "
            "(default: 256, i.e. ~107 us per hop at 2.4 MSPS)."
        ),
    )
    parser.add_argument(
        "--hops-per-read",
        type=int,
        default=16,
        help=(
            "Number of FFT hops read from the USB dongle per call. Larger "
            "values reduce driver overhead (default: 16)."
        ),
    )
    parser.add_argument(
        "--device-index",
        type=int,
        default=0,
        help="RTL-SDR device index (default: 0).",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=500,
        help="Number of waterfall rows to keep on screen (default: 500).",
    )
    parser.add_argument(
        "--time-window",
        type=float,
        default=5.0,
        help=(
            "Seconds of history the waterfall should span. Combined with "
            "--history, this sets how many FFT hops max-fold into a single "
            "displayed row (default: 5.0)."
        ),
    )
    parser.add_argument(
        "--refresh-ms",
        type=int,
        default=33,
        help="UI refresh period in milliseconds (default: 33 = ~30 Hz).",
    )
    parser.add_argument(
        "--vmin",
        type=float,
        default=None,
        help=(
            "Pin the color-scale minimum in dB (disables auto-calibration). "
            "Requires --vmax."
        ),
    )
    parser.add_argument(
        "--vmax",
        type=float,
        default=None,
        help="Pin the color-scale maximum in dB. Requires --vmin.",
    )
    parser.add_argument(
        "--list-gains",
        action="store_true",
        help="Open the dongle, print its supported gain steps, and exit.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use a synthetic signal source instead of the RTL-SDR dongle.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_gains:
        from .sdr import list_supported_gains

        try:
            gains = list_supported_gains(device_index=args.device_index)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to query gains: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print("Supported tuner gains (dB):")
        print("  " + ", ".join(f"{g:g}" for g in gains))
        return 0

    if (args.vmin is None) != (args.vmax is None):
        print("--vmin and --vmax must be provided together.", file=sys.stderr)
        return 2

    levels_db = None
    if args.vmin is not None and args.vmax is not None:
        if args.vmax <= args.vmin:
            print("--vmax must be greater than --vmin.", file=sys.stderr)
            return 2
        levels_db = (args.vmin, args.vmax)

    # Import Qt lazily so ``adsb-live --help`` works in headless environments.
    from pyqtgraph.Qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    rows_queue = DropOldestQueue(maxsize=512)

    if args.demo:
        source = DemoSource(
            center_freq=args.freq,
            sample_rate=args.rate,
            fft_size=args.fft_size,
            rows=rows_queue,
        )
    else:
        source = RtlSdrSource(
            center_freq=args.freq,
            sample_rate=args.rate,
            gain=args.gain,
            fft_size=args.fft_size,
            rows=rows_queue,
            device_index=args.device_index,
            hops_per_read=args.hops_per_read,
        )

    source.start()

    from .ui import WaterfallWindow

    window = WaterfallWindow(
        source=source,
        rows=rows_queue,
        num_time_rows=args.history,
        refresh_ms=args.refresh_ms,
        levels_db=levels_db,
        time_window_s=args.time_window,
    )
    window.show()

    # Ctrl-C in the launching terminal should close the app cleanly.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    try:
        exit_code = app.exec()
    finally:
        source.stop()
        source.join(timeout=2.0)
    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
