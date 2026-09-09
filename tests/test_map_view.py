"""Tests for the polar aircraft map helpers and widget."""

from __future__ import annotations

import math
import os

import pytest

from adsb_live.map_view import (
    RANGE_RINGS_NM,
    altitude_color,
    auto_range_nm,
    polar_xy,
)


# --- polar_xy ---------------------------------------------------------------


def test_polar_xy_north_is_positive_y() -> None:
    x, y = polar_xy((40.0, -105.0), 41.0, -105.0)
    assert abs(x) < 1e-6
    assert y > 0
    # 1 degree of latitude is ~60 nautical miles.
    assert 59.0 < y < 61.0


def test_polar_xy_east_is_positive_x() -> None:
    # 1 degree of longitude at ~40N is ~46 nm.
    x, y = polar_xy((40.0, -105.0), 40.0, -104.0)
    assert x > 0
    assert abs(y) < 1.0  # small great-circle latitude drift, but not zero
    assert 44.0 < x < 48.0


def test_polar_xy_south_is_negative_y() -> None:
    x, y = polar_xy((40.0, -105.0), 39.0, -105.0)
    assert abs(x) < 1e-6
    assert y < 0


def test_polar_xy_west_is_negative_x() -> None:
    x, _y = polar_xy((40.0, -105.0), 40.0, -106.0)
    assert x < 0


# --- altitude_color --------------------------------------------------------


@pytest.mark.parametrize(
    ("altitude", "expected_band"),
    [
        (0, "low"),
        (4_999, "low"),
        (5_000, "cyan"),
        (14_999, "cyan"),
        (15_000, "green"),
        (24_999, "green"),
        (25_000, "yellow"),
        (34_999, "yellow"),
        (35_000, "orange"),
        (44_999, "orange"),
        (45_000, "high"),
        (60_000, "high"),
    ],
)
def test_altitude_color_bands(altitude: float, expected_band: str) -> None:
    palette = {
        "low": (110, 180, 255),
        "cyan": (110, 240, 220),
        "green": (140, 230, 130),
        "yellow": (240, 220, 100),
        "orange": (240, 160, 90),
        "high": (240, 100, 100),
    }
    assert altitude_color(altitude) == palette[expected_band]


def test_altitude_color_ground_and_missing_are_grey() -> None:
    grey = (170, 170, 170)
    assert altitude_color(10_000, on_ground=True) == grey
    assert altitude_color(None) == grey
    assert altitude_color(math.nan) == grey


# --- auto_range_nm ----------------------------------------------------------


def test_auto_range_defaults_to_floor_when_empty() -> None:
    assert auto_range_nm([]) == 50.0
    assert auto_range_nm([]) == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("distances", "expected"),
    [
        ([5.0], 50.0),   # inside floor
        ([25.0], 50.0),  # snap-point covered by floor
        ([49.9], 50.0),
        ([80.0], 100.0),
        ([120.0], 150.0),
        ([151.0], 200.0),
        ([210.0], 250.0),
        ([9999.0], 250.0),  # clamped to ceiling
    ],
)
def test_auto_range_snaps_upward(distances: list[float], expected: float) -> None:
    assert auto_range_nm(distances) == expected


def test_auto_range_ignores_infinities() -> None:
    assert auto_range_nm([float("nan"), float("inf"), 40.0]) == 50.0


def test_range_rings_are_monotonic() -> None:
    assert RANGE_RINGS_NM == tuple(sorted(RANGE_RINGS_NM))


# --- Offscreen widget smoke test -------------------------------------------


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pyqtgraph.Qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_aircraft_map_widget_draws_positions_and_selects(qt_app) -> None:
    from adsb_live.map_view import AircraftMapWidget
    from adsb_live.tracks import AircraftTrack, TrackStore

    store = TrackStore(stale_after_s=60.0, clock=lambda: 100.0)
    store.upsert(
        AircraftTrack(
            icao="A12345",
            last_seen=99.0,
            latitude=40.0,
            longitude=-104.9,
            altitude_ft=35000,
        )
    )
    store.upsert(
        AircraftTrack(
            icao="A12345",
            last_seen=100.0,
            latitude=40.02,
            longitude=-104.9,
            altitude_ft=35100,
        )
    )
    store.upsert(
        AircraftTrack(
            icao="BEEF00",
            last_seen=100.0,
            latitude=39.5,
            longitude=-105.5,
            altitude_ft=2500,
        )
    )

    widget = AircraftMapWidget(store=store, receiver=(40.0, -105.0))
    try:
        drawn = widget.refresh(now=100.0)
        assert drawn == 2

        # Two aircraft dots (both placements have positions).
        assert len(widget._aircraft_scatter.points()) == 2

        # Trails: only the aircraft with two distinct positions gets a trail.
        assert set(widget._trail_items) == {"A12345"}
        # Labels for both aircraft.
        assert set(widget._label_items) == {"A12345", "BEEF00"}

        emissions: list[str] = []
        widget.aircraftSelected.connect(emissions.append)

        # Programmatic selection must not re-emit.
        widget.set_selected_icao("A12345")
        assert widget.selected_icao == "A12345"
        assert emissions == []

        # Fake a scatter click; the widget re-emits the ICAO.
        class _P:
            def __init__(self, icao: str) -> None:
                self._icao = icao

            def data(self) -> str:
                return self._icao

        widget._on_scatter_clicked(widget._aircraft_scatter, [_P("BEEF00")])
        assert emissions == ["BEEF00"]

        # Clicking empty space clears selection.
        widget._on_scatter_clicked(widget._aircraft_scatter, [])
        assert emissions[-1] == ""
    finally:
        widget.close()


def test_aircraft_map_widget_drops_stale_trails(qt_app) -> None:
    from adsb_live.map_view import AircraftMapWidget
    from adsb_live.tracks import AircraftTrack, TrackStore

    now = {"value": 100.0}
    store = TrackStore(stale_after_s=10.0, clock=lambda: now["value"])
    store.upsert(
        AircraftTrack(
            icao="A12345",
            last_seen=99.0,
            latitude=40.0,
            longitude=-104.9,
        )
    )
    store.upsert(
        AircraftTrack(
            icao="A12345",
            last_seen=100.0,
            latitude=40.02,
            longitude=-104.9,
        )
    )

    widget = AircraftMapWidget(store=store, receiver=(40.0, -105.0))
    try:
        widget.refresh(now=100.0)
        assert set(widget._trail_items) == {"A12345"}

        # Advance clock past the stale window.
        now["value"] = 200.0
        widget.refresh(now=200.0)
        assert widget._trail_items == {}
        assert widget._label_items == {}
    finally:
        widget.close()
