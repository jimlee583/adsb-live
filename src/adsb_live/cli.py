"""Command-line entry point: ``uv run adsb-live``."""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Sequence

from .sdr import DemoSource, DropOldestQueue, RtlSdrSource


def _parse_gain(value: str) -> object:
    if value.lower() == "auto":
        return "auto"
    try:
        return float(value)
    except ValueError as exc:  # pragma: no cover - argparse handles display
        raise argparse.ArgumentTypeError(
            f"gain must be 'auto' or a number in dB, got {value!r}"
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
        default="auto",
        help="Tuner gain in dB, or 'auto' (default: auto).",
    )
    parser.add_argument(
        "--fft-size",
        type=_parse_pow2_int,
        default=2048,
        help="FFT size per hop; must be a power of two (default: 2048).",
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
        default=400,
        help="Number of waterfall rows to keep on screen (default: 400).",
    )
    parser.add_argument(
        "--refresh-ms",
        type=int,
        default=33,
        help="UI refresh period in milliseconds (default: 33 = ~30 Hz).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use a synthetic signal source instead of the RTL-SDR dongle.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

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
        )

    source.start()

    from .ui import WaterfallWindow

    window = WaterfallWindow(
        source=source,
        rows=rows_queue,
        num_time_rows=args.history,
        refresh_ms=args.refresh_ms,
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
