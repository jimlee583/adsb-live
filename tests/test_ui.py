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
        store: TrackStore | None = None,
    ) -> None:
        self.store = store if store is not None else TrackStore(stale_after_s=60.0)
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
    """--decode with --lat/--lon: map and detail widgets are created."""
    from adsb_live.aircraft_detail import AircraftDetailWidget
    from adsb_live.map_view import AircraftMapWidget
    from adsb_live.ui import WaterfallWindow

    source = _make_source()
    decoder = _StubDecoder(receiver_lat=40.015, receiver_lon=-105.2705)
    window = WaterfallWindow(source=source, rows=source.rows, decoder=decoder)
    try:
        assert isinstance(window._aircraft_map, AircraftMapWidget)
        assert isinstance(window._aircraft_detail, AircraftDetailWidget)
        # Detail pane starts empty until an aircraft is selected.
        assert window._aircraft_detail.selected_icao is None
        status = window._status_text(rows_per_sec=0.0)
        assert "map=off" not in status
        assert "decoder=" in status
    finally:
        window.close()


def test_waterfall_window_builds_with_decoder_without_receiver(qt_app) -> None:
    """--decode without --lat/--lon: no map, but detail pane still present."""
    from adsb_live.aircraft_detail import AircraftDetailWidget
    from adsb_live.ui import WaterfallWindow

    source = _make_source()
    decoder = _StubDecoder(receiver_lat=None, receiver_lon=None)
    window = WaterfallWindow(source=source, rows=source.rows, decoder=decoder)
    try:
        assert window._aircraft_map is None
        # Detail pane doesn't need a receiver location.
        assert isinstance(window._aircraft_detail, AircraftDetailWidget)
        status = window._status_text(rows_per_sec=0.0)
        assert "map=off (needs --lat/--lon)" in status
    finally:
        window.close()


def test_waterfall_window_builds_with_replay_source(qt_app, tmp_path) -> None:
    """--replay path: WaterfallWindow shows a ``replay=`` status and hosts the
    aircraft views driven by the recorded ``TrackStore``."""

    from adsb_live import (
        AircraftTrack,
        ReplaySource,
        SessionRecorder,
    )
    from adsb_live.aircraft_detail import AircraftDetailWidget
    from adsb_live.map_view import AircraftMapWidget
    from adsb_live.ui import WaterfallWindow

    session_path = tmp_path / "session.db"
    recorder = SessionRecorder(
        session_path,
        receiver_lat=40.015,
        receiver_lon=-105.2705,
        stale_after_s=60.0,
        poll_interval_s=1.0,
        app_version="test",
        clock=lambda: 100.0,
        wall_clock=lambda: 0.0,
    )
    recorder.write_snapshot((
        AircraftTrack(
            icao="A12345",
            last_seen=100.5,
            callsign="TEST",
            latitude=40.02,
            longitude=-105.25,
            altitude_ft=30_000.0,
        ),
    ))
    recorder.close()

    source = _make_source()
    replay = ReplaySource(session_path)  # not started -- pure construction test
    window = WaterfallWindow(source=source, rows=source.rows, decoder=replay)
    try:
        assert isinstance(window._aircraft_map, AircraftMapWidget)
        assert isinstance(window._aircraft_detail, AircraftDetailWidget)
        status = window._status_text(rows_per_sec=0.0)
        assert "replay=" in status
        assert "session.db" in status
        assert "map=off" not in status
    finally:
        window.close()
        replay.stop()


def test_waterfall_window_detail_pane_reflects_selection(qt_app) -> None:
    """Selecting an aircraft via the coordinator drives the detail pane."""
    from adsb_live.tracks import AircraftTrack
    from adsb_live.ui import WaterfallWindow

    # Pin the store clock so the seeded tracks are not treated as
    # already-stale relative to the real ``time.monotonic()``.
    now = {"value": 102.0}
    store = TrackStore(stale_after_s=600.0, clock=lambda: now["value"])
    for i in range(3):
        store.upsert(
            AircraftTrack(
                icao="A12345",
                last_seen=100.0 + i,
                latitude=40.02,
                longitude=-105.25,
                altitude_ft=30_000.0 + 50 * i,
                ground_speed_kt=420.0,
                vertical_rate_fpm=48.0,
            )
        )

    source = _make_source()
    decoder = _StubDecoder(
        receiver_lat=40.015, receiver_lon=-105.2705, store=store
    )

    window = WaterfallWindow(source=source, rows=source.rows, decoder=decoder)
    try:
        # Force the table to refresh so the row exists in the proxy model.
        window._on_table_tick()

        # Drive the coordinator from the map side; this hits the
        # row-lookup + detail-pane switch code path in one shot.
        assert window._selection_coordinator is not None
        window._selection_coordinator._on_map_selected("A12345")

        assert window._aircraft_detail is not None
        assert window._aircraft_detail.selected_icao == "A12345"
        drawn = window._aircraft_detail.refresh(now=102.0)
        assert drawn == 3
    finally:
        window.close()
