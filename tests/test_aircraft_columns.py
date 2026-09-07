"""Hardware-free tests for the aircraft table row formatters."""

from __future__ import annotations

import math

import pytest

from adsb_live.aircraft_columns import (
    DASH,
    build_columns,
    haversine_nm,
    initial_bearing_deg,
    render_row,
)
from adsb_live.tracks import AircraftTrack


def _track(**overrides: object) -> AircraftTrack:
    defaults: dict[str, object] = {
        "icao": "A12345",
        "last_seen": 1000.0,
    }
    defaults.update(overrides)
    return AircraftTrack(**defaults)  # type: ignore[arg-type]


def test_columns_without_receiver_omit_distance_and_bearing() -> None:
    columns = build_columns(receiver=None)
    labels = [column.label for column in columns]
    assert "Dist (nm)" not in labels
    assert "Brg" not in labels
    assert labels[0] == "ICAO"
    assert labels[-1] == "Age"


def test_columns_with_receiver_include_distance_and_bearing() -> None:
    columns = build_columns(receiver=(40.0, -105.0))
    labels = [column.label for column in columns]
    assert "Dist (nm)" in labels
    assert "Brg" in labels


def test_render_row_formats_optional_fields_with_dash() -> None:
    columns = build_columns(receiver=None)
    row = render_row(_track(), columns, now=1005.0)
    row_by_label = dict(zip([c.label for c in columns], row))
    assert row_by_label["ICAO"] == "A12345"
    assert row_by_label["Callsign"] == DASH
    assert row_by_label["Alt (ft)"] == DASH
    assert row_by_label["Squawk"] == DASH
    assert row_by_label["Lat, Lon"] == DASH
    assert row_by_label["Age"] == "5s"


def test_render_row_formats_populated_fields() -> None:
    columns = build_columns(receiver=None)
    track = _track(
        callsign="UAL123",
        altitude_ft=35000,
        ground_speed_kt=450,
        track_deg=270,
        vertical_rate_fpm=1024,
        squawk="1200",
        latitude=40.5,
        longitude=-104.5,
        signal_db=-15.25,
    )
    row = render_row(track, columns, now=1005.0)
    labels = [c.label for c in columns]
    row_by_label = dict(zip(labels, row))
    assert row_by_label["Callsign"] == "UAL123"
    assert row_by_label["Alt (ft)"] == "35,000"
    assert row_by_label["GS (kt)"] == "450"
    assert row_by_label["Trk"] == "270"
    assert row_by_label["VS (fpm)"] == "+1024"
    assert row_by_label["Squawk"] == "1200"
    assert row_by_label["Lat, Lon"] == "40.5000, -104.5000"
    assert row_by_label["RSSI"] == "-15.2"


def test_render_row_shows_ground_for_on_ground_track() -> None:
    columns = build_columns(receiver=None)
    track = _track(on_ground=True)
    row = render_row(track, columns, now=1001.0)
    labels = [c.label for c in columns]
    row_by_label = dict(zip(labels, row))
    assert row_by_label["Alt (ft)"] == "GND"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0.0, "0s"),
        (12.4, "12s"),
        (59.9, "60s"),
        (120.0, "2.0m"),
        (3600.0, "1.0h"),
    ],
)
def test_age_formatting(delta: float, expected: str) -> None:
    columns = build_columns(receiver=None)
    row = render_row(_track(last_seen=1000.0), columns, now=1000.0 + delta)
    labels = [c.label for c in columns]
    assert dict(zip(labels, row))["Age"] == expected


def test_haversine_matches_known_distance() -> None:
    # KDEN (39.8617, -104.6731) to KLAX (33.9425, -118.4081) is ~752 nm.
    distance = haversine_nm(39.8617, -104.6731, 33.9425, -118.4081)
    assert 740.0 < distance < 770.0


def test_bearing_east_and_north() -> None:
    east = initial_bearing_deg(0.0, 0.0, 0.0, 1.0)
    north = initial_bearing_deg(0.0, 0.0, 1.0, 0.0)
    assert math.isclose(east, 90.0, abs_tol=1e-6)
    assert math.isclose(north, 0.0, abs_tol=1e-6)


def test_distance_and_bearing_appear_only_when_receiver_and_position_known() -> None:
    columns = build_columns(receiver=(40.0, -105.0))
    labels = [c.label for c in columns]

    # No aircraft position -> dashes for distance/bearing.
    row = render_row(_track(), columns, now=1001.0, receiver=(40.0, -105.0))
    row_by_label = dict(zip(labels, row))
    assert row_by_label["Dist (nm)"] == DASH
    assert row_by_label["Brg"] == DASH

    # With a position -> real numbers.
    track = _track(latitude=40.5, longitude=-104.5)
    row = render_row(track, columns, now=1001.0, receiver=(40.0, -105.0))
    row_by_label = dict(zip(labels, row))
    assert row_by_label["Dist (nm)"] != DASH
    assert row_by_label["Brg"] != DASH


def test_sort_keys_are_numeric_for_numeric_columns() -> None:
    columns = build_columns(receiver=None)
    by_name = {c.name: c for c in columns}
    ctx_low = _ctx(_track(altitude_ft=1000, ground_speed_kt=100), now=1000.0)
    ctx_high = _ctx(_track(altitude_ft=35000, ground_speed_kt=500), now=1000.0)
    assert by_name["altitude_ft"].sort_key(ctx_low) == 1000
    assert by_name["altitude_ft"].sort_key(ctx_high) == 35000
    assert by_name["ground_speed_kt"].sort_key(ctx_low) == 100


def _ctx(track: AircraftTrack, *, now: float):
    from adsb_live.aircraft_columns import RowContext

    return RowContext(track=track, now=now)
