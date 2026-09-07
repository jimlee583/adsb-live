"""Pure formatting helpers for the aircraft table view.

Kept free of Qt imports so the row-formatting logic can be exercised in
tests without spinning up a Qt application.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tracks import AircraftTrack

_EARTH_RADIUS_NM = 3440.065  # nautical miles


def haversine_nm(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two points in nautical miles."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    c = 2.0 * math.asin(min(1.0, math.sqrt(a)))
    return _EARTH_RADIUS_NM * c


def initial_bearing_deg(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Initial great-circle bearing from point 1 to point 2, in degrees."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


@dataclass(frozen=True, slots=True)
class Column:
    """One aircraft-table column.

    ``sort_key`` returns a value used for click-to-sort. ``display``
    returns the string shown in the cell. Missing data is represented by
    ``None`` in the sort key and by ``em dash`` in the display.
    """

    name: str
    label: str
    numeric: bool
    sort_key: Callable[["RowContext"], Any]
    display: Callable[["RowContext"], str]


@dataclass(frozen=True, slots=True)
class RowContext:
    """Everything a column formatter needs to render one row."""

    track: AircraftTrack
    now: float
    receiver: tuple[float, float] | None = None


DASH = "\u2014"


def _fmt_optional(value: float | None, fmt: str) -> str:
    if value is None:
        return DASH
    return format(value, fmt)


def _fmt_altitude(track: AircraftTrack) -> str:
    if track.on_ground:
        return "GND"
    if track.altitude_ft is None:
        return DASH
    return f"{track.altitude_ft:,.0f}"


def _altitude_sort(track: AircraftTrack) -> float | None:
    if track.on_ground:
        return 0.0
    return track.altitude_ft


def _receiver_relative(ctx: RowContext) -> tuple[float, float] | None:
    if ctx.receiver is None or ctx.track.latitude is None or ctx.track.longitude is None:
        return None
    lat, lon = ctx.receiver
    distance = haversine_nm(lat, lon, ctx.track.latitude, ctx.track.longitude)
    bearing = initial_bearing_deg(lat, lon, ctx.track.latitude, ctx.track.longitude)
    return distance, bearing


def _age_seconds(ctx: RowContext) -> float:
    return max(0.0, ctx.now - ctx.track.last_seen)


def _fmt_age(seconds: float) -> str:
    if seconds < 60.0:
        return f"{seconds:.0f}s"
    if seconds < 3600.0:
        return f"{seconds / 60.0:.1f}m"
    return f"{seconds / 3600.0:.1f}h"


def _fmt_lat_lon(track: AircraftTrack) -> str:
    if track.latitude is None or track.longitude is None:
        return DASH
    return f"{track.latitude:.4f}, {track.longitude:.4f}"


def build_columns(receiver: tuple[float, float] | None) -> tuple[Column, ...]:
    """Return the ordered column list, hiding distance/bearing without a receiver."""

    columns: list[Column] = [
        Column(
            name="icao",
            label="ICAO",
            numeric=False,
            sort_key=lambda ctx: ctx.track.icao,
            display=lambda ctx: ctx.track.icao,
        ),
        Column(
            name="callsign",
            label="Callsign",
            numeric=False,
            sort_key=lambda ctx: ctx.track.callsign or "",
            display=lambda ctx: ctx.track.callsign or DASH,
        ),
        Column(
            name="altitude_ft",
            label="Alt (ft)",
            numeric=True,
            sort_key=lambda ctx: _altitude_sort(ctx.track),
            display=lambda ctx: _fmt_altitude(ctx.track),
        ),
        Column(
            name="ground_speed_kt",
            label="GS (kt)",
            numeric=True,
            sort_key=lambda ctx: ctx.track.ground_speed_kt,
            display=lambda ctx: _fmt_optional(ctx.track.ground_speed_kt, ".0f"),
        ),
        Column(
            name="track_deg",
            label="Trk",
            numeric=True,
            sort_key=lambda ctx: ctx.track.track_deg,
            display=lambda ctx: _fmt_optional(ctx.track.track_deg, ".0f"),
        ),
        Column(
            name="vertical_rate_fpm",
            label="VS (fpm)",
            numeric=True,
            sort_key=lambda ctx: ctx.track.vertical_rate_fpm,
            display=lambda ctx: _fmt_optional(ctx.track.vertical_rate_fpm, "+.0f"),
        ),
        Column(
            name="squawk",
            label="Squawk",
            numeric=False,
            sort_key=lambda ctx: ctx.track.squawk or "",
            display=lambda ctx: ctx.track.squawk or DASH,
        ),
        Column(
            name="lat_lon",
            label="Lat, Lon",
            numeric=False,
            sort_key=lambda ctx: (
                ctx.track.latitude if ctx.track.latitude is not None else -1e9
            ),
            display=lambda ctx: _fmt_lat_lon(ctx.track),
        ),
    ]

    if receiver is not None:
        columns.append(
            Column(
                name="distance_nm",
                label="Dist (nm)",
                numeric=True,
                sort_key=lambda ctx: (
                    _receiver_relative(ctx)[0]
                    if _receiver_relative(ctx) is not None
                    else None
                ),
                display=lambda ctx: (
                    f"{_receiver_relative(ctx)[0]:.1f}"
                    if _receiver_relative(ctx) is not None
                    else DASH
                ),
            )
        )
        columns.append(
            Column(
                name="bearing_deg",
                label="Brg",
                numeric=True,
                sort_key=lambda ctx: (
                    _receiver_relative(ctx)[1]
                    if _receiver_relative(ctx) is not None
                    else None
                ),
                display=lambda ctx: (
                    f"{_receiver_relative(ctx)[1]:.0f}"
                    if _receiver_relative(ctx) is not None
                    else DASH
                ),
            )
        )

    columns.append(
        Column(
            name="signal_db",
            label="RSSI",
            numeric=True,
            sort_key=lambda ctx: ctx.track.signal_db,
            display=lambda ctx: _fmt_optional(ctx.track.signal_db, ".1f"),
        )
    )
    columns.append(
        Column(
            name="age_s",
            label="Age",
            numeric=True,
            sort_key=lambda ctx: _age_seconds(ctx),
            display=lambda ctx: _fmt_age(_age_seconds(ctx)),
        )
    )

    return tuple(columns)


def render_row(
    track: AircraftTrack,
    columns: tuple[Column, ...],
    *,
    now: float,
    receiver: tuple[float, float] | None = None,
) -> tuple[str, ...]:
    """Return the display strings for one aircraft row."""
    ctx = RowContext(track=track, now=now, receiver=receiver)
    return tuple(column.display(ctx) for column in columns)
