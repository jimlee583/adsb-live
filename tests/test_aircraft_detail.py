"""Tests for the selected-aircraft detail pane."""

from __future__ import annotations

import os

import numpy as np
import pytest

from adsb_live.aircraft_detail import TelemetrySeries, telemetry_series
from adsb_live.tracks import TelemetrySample


# --- telemetry_series (pure helper) ----------------------------------------


def test_telemetry_series_shifts_x_to_seconds_before_now() -> None:
    samples = [
        TelemetrySample(
            timestamp=100.0,
            altitude_ft=30_000.0,
            ground_speed_kt=420.0,
            vertical_rate_fpm=0.0,
        ),
        TelemetrySample(
            timestamp=105.0,
            altitude_ft=30_050.0,
            ground_speed_kt=421.0,
            vertical_rate_fpm=32.0,
        ),
    ]

    series = telemetry_series(samples, now=105.0)

    # Newest sample lands at t=0; older samples land at negative offsets.
    assert list(series.t_altitude_s) == [-5.0, 0.0]
    assert list(series.altitude_ft) == [30_000.0, 30_050.0]
    assert list(series.t_speed_s) == [-5.0, 0.0]
    assert list(series.ground_speed_kt) == [420.0, 421.0]
    assert list(series.t_vrate_s) == [-5.0, 0.0]
    assert list(series.vertical_rate_fpm) == [0.0, 32.0]


def test_telemetry_series_drops_none_gaps_per_series() -> None:
    """Each series independently skips its ``None`` gaps so the plot
    does not draw them as zeros."""

    samples = [
        TelemetrySample(
            timestamp=100.0, altitude_ft=30_000.0,
            ground_speed_kt=420.0, vertical_rate_fpm=None,
        ),
        TelemetrySample(
            timestamp=101.0, altitude_ft=None,
            ground_speed_kt=421.0, vertical_rate_fpm=-16.0,
        ),
        TelemetrySample(
            timestamp=102.0, altitude_ft=30_100.0,
            ground_speed_kt=None, vertical_rate_fpm=64.0,
        ),
    ]

    series = telemetry_series(samples, now=102.0)

    # Altitude present at t=100, t=102 (t=101 dropped).
    assert list(series.t_altitude_s) == [-2.0, 0.0]
    assert list(series.altitude_ft) == [30_000.0, 30_100.0]
    # Ground speed present at t=100, t=101 (t=102 dropped).
    assert list(series.t_speed_s) == [-2.0, -1.0]
    assert list(series.ground_speed_kt) == [420.0, 421.0]
    # Vertical rate present at t=101, t=102 (t=100 dropped).
    assert list(series.t_vrate_s) == [-1.0, 0.0]
    assert list(series.vertical_rate_fpm) == [-16.0, 64.0]


def test_telemetry_series_returns_empty_arrays_for_empty_input() -> None:
    series = telemetry_series([], now=100.0)
    assert isinstance(series, TelemetrySeries)
    for arr in series:
        assert isinstance(arr, np.ndarray)
        assert arr.size == 0


# --- AircraftDetailWidget (offscreen Qt smoke test) ------------------------


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pyqtgraph.Qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_aircraft_detail_widget_empty_state_has_no_selection(qt_app) -> None:
    from adsb_live.aircraft_detail import AircraftDetailWidget
    from adsb_live.tracks import TrackStore

    store = TrackStore()
    widget = AircraftDetailWidget(store=store)
    try:
        assert widget.selected_icao is None
        # refresh() returns 0 with no selection.
        assert widget.refresh() == 0
        # Empty-state header must invite the user to pick something.
        assert "Select an aircraft" in widget._header.text()
    finally:
        widget.close()


def test_aircraft_detail_widget_draws_telemetry_for_selected_icao(qt_app) -> None:
    from adsb_live.aircraft_detail import AircraftDetailWidget
    from adsb_live.tracks import AircraftTrack, TrackStore

    store = TrackStore(stale_after_s=600.0, clock=lambda: 200.0)
    for i in range(5):
        store.upsert(
            AircraftTrack(
                icao="A12345",
                last_seen=100.0 + i,
                altitude_ft=30_000.0 + i * 50,
                ground_speed_kt=420.0 + i,
                vertical_rate_fpm=64.0,
            )
        )

    widget = AircraftDetailWidget(store=store)
    try:
        widget.set_selected_icao("a12345")
        assert widget.selected_icao == "A12345"

        drawn = widget.refresh(now=104.0)
        assert drawn == 5

        # Header surfaces the latest values.
        text = widget._header.text()
        assert "A12345" in text
        assert "alt=" in text and "spd=" in text and "vrate=" in text
        assert "history=4s" in text
        assert "(5 samples)" in text

        # Curves actually received data.
        alt_x, alt_y = widget._alt_curve.getData()
        assert len(alt_x) == 5
        assert alt_y[-1] == pytest.approx(30_200.0)
    finally:
        widget.close()


def test_aircraft_detail_widget_shows_no_telemetry_note_when_missing(qt_app) -> None:
    from adsb_live.aircraft_detail import AircraftDetailWidget
    from adsb_live.tracks import TrackStore

    store = TrackStore()
    widget = AircraftDetailWidget(store=store)
    try:
        # Aircraft never appeared in the store.
        widget.set_selected_icao("BEEF00")
        assert widget.refresh() == 0
        assert "no telemetry yet" in widget._header.text()
    finally:
        widget.close()


def test_aircraft_detail_widget_switching_selection_replaces_data(qt_app) -> None:
    from adsb_live.aircraft_detail import AircraftDetailWidget
    from adsb_live.tracks import AircraftTrack, TrackStore

    store = TrackStore(stale_after_s=600.0, clock=lambda: 200.0)
    for i in range(3):
        store.upsert(
            AircraftTrack(
                icao="AAAAAA", last_seen=100.0 + i, altitude_ft=30_000.0
            )
        )
    for i in range(2):
        store.upsert(
            AircraftTrack(
                icao="BBBBBB", last_seen=100.0 + i, altitude_ft=10_000.0
            )
        )

    widget = AircraftDetailWidget(store=store)
    try:
        widget.set_selected_icao("AAAAAA")
        widget.refresh(now=200.0)
        assert widget._alt_curve.getData()[1][-1] == pytest.approx(30_000.0)

        widget.set_selected_icao("BBBBBB")
        widget.refresh(now=200.0)
        assert widget._alt_curve.getData()[1][-1] == pytest.approx(10_000.0)

        # Clearing selection empties the plots and restores the empty header.
        widget.set_selected_icao(None)
        assert widget.refresh() == 0
        assert "Select an aircraft" in widget._header.text()
    finally:
        widget.close()
