"""Offscreen smoke tests for :class:`~adsb_live.ui.WaterfallWindow`.

These do not spin the Qt event loop; they just build the window and read
the status text. The goal is to lock in the constructor's dependency
order so bugs like ``AttributeError: '_aircraft_map'`` during status-bar
init cannot regress.
"""

from __future__ import annotations

import os

import pytest

from adsb_live.decoder import Dump1090ProcessHealth
from adsb_live.dump1090 import Dump1090SourceHealth
from adsb_live.sdr import DemoSource, DropOldestByteQueue, DropOldestQueue
from adsb_live.tracks import TrackStore


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pyqtgraph.Qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class _StubProcess:
    """Minimal stand-in for :class:`adsb_live.decoder.Dump1090Process`.

    ``WaterfallWindow`` reads ``receiver_lat``/``receiver_lon`` while
    wiring up the aircraft view, and ``health`` while rendering the
    decoder status line.
    """

    def __init__(
        self,
        *,
        receiver_lat: float | None,
        receiver_lon: float | None,
    ) -> None:
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.health = Dump1090ProcessHealth()


class _StubPoller:
    def __init__(self) -> None:
        self.health = Dump1090SourceHealth()


class _StubDecoder:
    """Just enough surface for the ``WaterfallWindow`` constructor."""

    def __init__(
        self,
        *,
        receiver_lat: float | None,
        receiver_lon: float | None,
    ) -> None:
        self.store = TrackStore(stale_after_s=60.0)
        self.iq_sink = DropOldestByteQueue(maxsize=64)
        self.process = _StubProcess(
            receiver_lat=receiver_lat, receiver_lon=receiver_lon
        )
        self.poller = _StubPoller()

    def stop(self, timeout: float = 3.0) -> None:
        # Called by ``WaterfallWindow.closeEvent`` on teardown.
        return None


def _make_source() -> DemoSource:
    return DemoSource(
        center_freq=1_090_000_000.0,
        sample_rate=2_400_000.0,
        fft_size=1024,
        rows=DropOldestQueue(maxsize=8),
    )


def test_waterfall_window_builds_with_decoder_and_receiver(qt_app) -> None:
    """--decode with --lat/--lon: a map widget is created and shown."""
    from adsb_live.map_view import AircraftMapWidget
    from adsb_live.ui import WaterfallWindow

    source = _make_source()
    decoder = _StubDecoder(receiver_lat=40.015, receiver_lon=-105.2705)
    window = WaterfallWindow(source=source, rows=source.rows, decoder=decoder)
    try:
        assert isinstance(window._aircraft_map, AircraftMapWidget)
        status = window._status_text(rows_per_sec=0.0)
        assert "map=off" not in status
        assert "decoder=" in status
    finally:
        window.close()


def test_waterfall_window_builds_with_decoder_without_receiver(qt_app) -> None:
    """--decode without --lat/--lon: no map, and status flags it."""
    from adsb_live.ui import WaterfallWindow

    source = _make_source()
    decoder = _StubDecoder(receiver_lat=None, receiver_lon=None)
    window = WaterfallWindow(source=source, rows=source.rows, decoder=decoder)
    try:
        assert window._aircraft_map is None
        status = window._status_text(rows_per_sec=0.0)
        assert "map=off (needs --lat/--lon)" in status
    finally:
        window.close()
