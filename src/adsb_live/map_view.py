"""Polar receiver-centered aircraft map view.

The module provides both pure helpers for projection, colouring, and
adaptive range selection, and the ``AircraftMapWidget`` PyQtGraph widget
that renders live aircraft and their trails around the receiver.

Pure helpers can be exercised in tests without a Qt event loop; the
widget itself needs an offscreen Qt platform.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Optional

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from .aircraft_columns import haversine_nm, initial_bearing_deg
from .tracks import TrackStore


# --- pure helpers -----------------------------------------------------------

#: Rings drawn on the map. All rings above the current auto range are hidden.
RANGE_RINGS_NM: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0, 200.0)

#: Snap points for the outer view radius, small to large.
_RANGE_SNAP: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0, 150.0, 200.0, 250.0)


def polar_xy(
    receiver: tuple[float, float], lat: float, lon: float
) -> tuple[float, float]:
    """Project ``(lat, lon)`` into receiver-centered Cartesian nautical miles.

    Returns ``(x_east_nm, y_north_nm)``. Bearing is measured from north;
    ``sin(bearing)`` gives the east component and ``cos(bearing)`` gives
    the north component, so **north is +y and east is +x** -- the natural
    radar-scope orientation.
    """
    r_lat, r_lon = receiver
    distance = haversine_nm(r_lat, r_lon, lat, lon)
    bearing = math.radians(initial_bearing_deg(r_lat, r_lon, lat, lon))
    return distance * math.sin(bearing), distance * math.cos(bearing)


# 6-band altitude palette, low to high.
_ALT_BANDS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (5_000.0, (110, 180, 255)),   # blue      0-5k
    (15_000.0, (110, 240, 220)),  # cyan      5-15k
    (25_000.0, (140, 230, 130)),  # green     15-25k
    (35_000.0, (240, 220, 100)),  # yellow    25-35k
    (45_000.0, (240, 160, 90)),   # orange    35-45k
)
_ALT_HIGH: tuple[int, int, int] = (240, 100, 100)     # red    45k+
_ALT_GROUND: tuple[int, int, int] = (170, 170, 170)   # grey   on-ground / unknown


def altitude_color(
    altitude_ft: float | None, *, on_ground: bool | None = None
) -> tuple[int, int, int]:
    """Return an ``(R, G, B)`` tuple for an altitude in feet."""
    if on_ground:
        return _ALT_GROUND
    if altitude_ft is None or not math.isfinite(altitude_ft):
        return _ALT_GROUND
    for threshold, colour in _ALT_BANDS:
        if altitude_ft < threshold:
            return colour
    return _ALT_HIGH


def auto_range_nm(
    distances: Iterable[float],
    *,
    floor_nm: float = 50.0,
    ceiling_nm: float = 250.0,
) -> float:
    """Return the smallest snap-point that contains all given distances.

    Empty inputs return ``floor_nm``. Distances beyond ``ceiling_nm`` are
    clamped so the outer ring never exceeds the ceiling; that keeps a
    single distant echo from stretching the whole view.
    """
    peak = 0.0
    for distance in distances:
        if not math.isfinite(distance):
            continue
        if distance > peak:
            peak = distance
    if peak <= 0.0:
        return floor_nm
    peak = min(peak, ceiling_nm)
    for snap in _RANGE_SNAP:
        if snap >= peak:
            return max(snap, floor_nm)
    return ceiling_nm


# --- Qt widget --------------------------------------------------------------


_CARDINAL_OFFSETS: tuple[tuple[str, float, float, tuple[float, float]], ...] = (
    ("N", 0.0, 1.0, (0.5, 1.0)),
    ("S", 0.0, -1.0, (0.5, 0.0)),
    ("E", 1.0, 0.0, (0.0, 0.5)),
    ("W", -1.0, 0.0, (1.0, 0.5)),
)


class AircraftMapWidget(pg.PlotWidget):
    """PyQtGraph radar-scope view centred on the receiver.

    Emits :attr:`aircraftSelected` with an ICAO string when the user
    clicks a scatter point (empty string when clicking empty space).
    Call :meth:`refresh` on a 1 Hz timer to redraw from the shared
    :class:`~adsb_live.tracks.TrackStore`.
    """

    aircraftSelected = QtCore.Signal(str)

    def __init__(
        self,
        *,
        store: TrackStore,
        receiver: tuple[float, float],
        parent: Optional[QtWidgets.QWidget] = None,
        max_range_nm: float = 250.0,
    ) -> None:
        super().__init__(parent=parent)
        self._store = store
        self._receiver = receiver
        self._max_range_nm = float(max_range_nm)
        self._selected_icao: str | None = None
        self._applying_selection = False
        self._trail_items: dict[str, pg.PlotDataItem] = {}
        self._label_items: dict[str, pg.TextItem] = {}
        self._current_range_nm: float | None = None

        self.setBackground("#101014")
        plot = self.getPlotItem()
        plot.setAspectLocked(True)
        plot.showAxis("left", False)
        plot.showAxis("bottom", False)
        plot.hideButtons()
        plot.setMenuEnabled(False)
        plot.setTitle("")

        # Range rings.
        self._rings: list[QtWidgets.QGraphicsEllipseItem] = []
        self._ring_labels: list[pg.TextItem] = []
        for nm in RANGE_RINGS_NM:
            ring = QtWidgets.QGraphicsEllipseItem(-nm, -nm, 2 * nm, 2 * nm)
            ring.setPen(
                pg.mkPen(
                    color=(90, 90, 100),
                    width=1,
                    style=QtCore.Qt.PenStyle.DashLine,
                )
            )
            plot.addItem(ring)
            self._rings.append(ring)
            label = pg.TextItem(
                f"{int(nm)} nm", color=(120, 120, 130), anchor=(0.0, 1.0)
            )
            label.setPos(0.0, nm)
            plot.addItem(label)
            self._ring_labels.append(label)

        # Cardinal-direction labels; positioned when the range is applied.
        self._cardinals: list[pg.TextItem] = []
        for text, _dx, _dy, anchor in _CARDINAL_OFFSETS:
            item = pg.TextItem(text, color=(180, 200, 220), anchor=anchor)
            self._cardinals.append(item)
            plot.addItem(item)

        # Receiver marker at the origin.
        receiver_marker = pg.ScatterPlotItem(
            x=[0.0],
            y=[0.0],
            symbol="+",
            size=16,
            pen=pg.mkPen(color=(220, 220, 240), width=2),
            brush=pg.mkBrush(0, 0, 0, 0),
        )
        plot.addItem(receiver_marker)

        # Aircraft dots and selection overlay.
        self._aircraft_scatter = pg.ScatterPlotItem(
            size=12, pen=pg.mkPen(color=(20, 20, 24), width=1)
        )
        self._aircraft_scatter.sigClicked.connect(self._on_scatter_clicked)
        plot.addItem(self._aircraft_scatter)

        self._selection_scatter = pg.ScatterPlotItem(
            size=22,
            symbol="o",
            pen=pg.mkPen(color=(255, 255, 255), width=2),
            brush=pg.mkBrush(0, 0, 0, 0),
        )
        plot.addItem(self._selection_scatter)

        # Initial view.
        self._apply_range(self._max_range_nm)

    # -- public API ---------------------------------------------------

    def refresh(self, *, now: float | None = None) -> int:
        """Re-render from the store. Returns the count of drawn aircraft."""

        tracks = {t.icao: t for t in self._store.snapshot(now=now)}
        histories = self._store.histories(now=now)

        # Aircraft positions in view-space nm.
        placed: list[tuple[str, float, float]] = []
        distances: list[float] = []
        spots: list[dict] = []
        selected_spot: dict | None = None
        for icao in sorted(tracks):
            track = tracks[icao]
            if track.latitude is None or track.longitude is None:
                continue
            x, y = polar_xy(self._receiver, track.latitude, track.longitude)
            distances.append(math.hypot(x, y))
            placed.append((icao, x, y))
            spots.append(
                {
                    "pos": (x, y),
                    "brush": pg.mkBrush(
                        *altitude_color(
                            track.altitude_ft, on_ground=track.on_ground
                        )
                    ),
                    "symbol": "t1",
                    "data": icao,
                }
            )
            if icao == self._selected_icao:
                selected_spot = {"pos": (x, y)}

        new_range = auto_range_nm(distances, ceiling_nm=self._max_range_nm)
        if self._current_range_nm != new_range:
            self._apply_range(new_range)

        # Trails.
        placed_icaos = {icao for icao, _, _ in placed}
        for icao in list(self._trail_items):
            if icao not in placed_icaos:
                self._remove_trail(icao)
        for icao, _, _ in placed:
            history = histories.get(icao)
            if history is None or len(history) < 2:
                self._remove_trail(icao)
                continue
            xs = np.empty(len(history), dtype=np.float32)
            ys = np.empty(len(history), dtype=np.float32)
            for i, sample in enumerate(history):
                xs[i], ys[i] = polar_xy(
                    self._receiver, sample.latitude, sample.longitude
                )
            colour = altitude_color(
                history[-1].altitude_ft,
                on_ground=tracks[icao].on_ground,
            )
            pen = pg.mkPen(color=(*colour, 160), width=1.2)
            trail = self._trail_items.get(icao)
            if trail is None:
                trail = pg.PlotDataItem(xs, ys, pen=pen, antialias=True)
                self._trail_items[icao] = trail
                self.getPlotItem().addItem(trail)
            else:
                trail.setData(xs, ys, pen=pen)

        # Labels next to each dot.
        current_labels: set[str] = set()
        for icao, x, y in placed:
            self._ensure_label(icao, x, y, tracks[icao].callsign or icao)
            current_labels.add(icao)
        for stale in list(self._label_items):
            if stale not in current_labels:
                item = self._label_items.pop(stale)
                self.getPlotItem().removeItem(item)

        self._aircraft_scatter.setData(spots=spots)
        self._selection_scatter.setData(
            spots=[selected_spot] if selected_spot is not None else []
        )
        return len(placed)

    def set_selected_icao(self, icao: str | None) -> None:
        """Highlight one aircraft. Called by the selection coordinator."""
        normalized = icao if icao else None
        if normalized == self._selected_icao:
            return
        self._selected_icao = normalized
        self._applying_selection = True
        try:
            self.refresh()
        finally:
            self._applying_selection = False

    @property
    def selected_icao(self) -> str | None:
        return self._selected_icao

    # -- helpers ------------------------------------------------------

    def _apply_range(self, nm: float) -> None:
        self._current_range_nm = nm
        pad = nm * 1.1
        self.getPlotItem().setRange(
            xRange=(-pad, pad), yRange=(-pad, pad), padding=0
        )
        for ring, ring_nm, label in zip(
            self._rings, RANGE_RINGS_NM, self._ring_labels
        ):
            visible = ring_nm <= nm + 1e-6
            ring.setVisible(visible)
            label.setVisible(visible)
        for item, (_text, dx, dy, _anchor) in zip(
            self._cardinals, _CARDINAL_OFFSETS
        ):
            item.setPos(dx * nm, dy * nm)

    def _remove_trail(self, icao: str) -> None:
        trail = self._trail_items.pop(icao, None)
        if trail is not None:
            self.getPlotItem().removeItem(trail)

    def _ensure_label(self, icao: str, x: float, y: float, text: str) -> None:
        item = self._label_items.get(icao)
        if item is None:
            item = pg.TextItem(text, color=(210, 210, 220), anchor=(0.0, 1.0))
            self._label_items[icao] = item
            self.getPlotItem().addItem(item)
        else:
            item.setText(text)
        # Small offset so the label sits above/right of the marker.
        item.setPos(x + 0.8, y + 0.8)

    def _on_scatter_clicked(
        self, _plot: pg.ScatterPlotItem, points: list
    ) -> None:
        if not points:
            self._emit_selection("")
            return
        icao = points[0].data()
        if not icao:
            return
        self._emit_selection(str(icao))

    def _emit_selection(self, icao: str) -> None:
        if self._applying_selection:
            return
        self.aircraftSelected.emit(icao)
