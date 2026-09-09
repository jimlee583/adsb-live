"""Selected-aircraft detail pane.

The module exposes a Qt-free reshape helper (:func:`telemetry_series`)
alongside a PyQtGraph widget (:class:`AircraftDetailWidget`) that plots
altitude, ground speed, and vertical rate for the currently selected
aircraft. The widget draws its data from the shared
:class:`~adsb_live.tracks.TrackStore` so no per-widget bookkeeping or
sampling thread is required.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple, Optional

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from .tracks import TelemetrySample, TrackStore


# --- pure helpers -----------------------------------------------------------


class TelemetrySeries(NamedTuple):
    """Reshaped telemetry ready to hand straight to a PyQtGraph curve.

    ``t_s`` is seconds relative to ``now`` (0 at ``now``, negative going
    back in time). Each of the value arrays has the same length as
    ``t_s`` but with the corresponding index dropped whenever the
    original :class:`TelemetrySample` field was ``None`` -- so the three
    arrays are typically shorter than ``t_s`` when a series has gaps.
    """

    t_altitude_s: np.ndarray
    altitude_ft: np.ndarray
    t_speed_s: np.ndarray
    ground_speed_kt: np.ndarray
    t_vrate_s: np.ndarray
    vertical_rate_fpm: np.ndarray


def telemetry_series(
    samples: Iterable[TelemetrySample], *, now: float
) -> TelemetrySeries:
    """Reshape a sequence of :class:`TelemetrySample` for plotting.

    ``None`` values are filtered per-series so a plot skips gaps without
    drawing them as ``0``. The X axis for each series is expressed in
    seconds relative to ``now`` (negative for the past), so the newest
    sample sits at or near ``0``.
    """

    ts: list[float] = []
    alt_t: list[float] = []
    alt_v: list[float] = []
    spd_t: list[float] = []
    spd_v: list[float] = []
    vr_t: list[float] = []
    vr_v: list[float] = []

    for sample in samples:
        dt = sample.timestamp - now
        ts.append(dt)
        if sample.altitude_ft is not None:
            alt_t.append(dt)
            alt_v.append(float(sample.altitude_ft))
        if sample.ground_speed_kt is not None:
            spd_t.append(dt)
            spd_v.append(float(sample.ground_speed_kt))
        if sample.vertical_rate_fpm is not None:
            vr_t.append(dt)
            vr_v.append(float(sample.vertical_rate_fpm))

    return TelemetrySeries(
        t_altitude_s=np.asarray(alt_t, dtype=np.float64),
        altitude_ft=np.asarray(alt_v, dtype=np.float64),
        t_speed_s=np.asarray(spd_t, dtype=np.float64),
        ground_speed_kt=np.asarray(spd_v, dtype=np.float64),
        t_vrate_s=np.asarray(vr_t, dtype=np.float64),
        vertical_rate_fpm=np.asarray(vr_v, dtype=np.float64),
    )


# --- Qt widget --------------------------------------------------------------


_ALT_COLOR = (150, 210, 255)   # blueish
_SPD_COLOR = (150, 230, 160)   # green
_VRATE_COLOR = (240, 200, 120)  # amber


class AircraftDetailWidget(QtWidgets.QWidget):
    """Bottom-strip detail pane: altitude, ground speed, vertical rate.

    The widget is state-light: on every :meth:`refresh` it reads
    :meth:`TrackStore.telemetry` for the currently selected ICAO and
    replaces the three plot curves. The X axes of the three plots are
    linked so panning/zooming stays in sync.
    """

    def __init__(
        self,
        *,
        store: TrackStore,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent=parent)
        self._store = store
        self._selected_icao: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._header = QtWidgets.QLabel(self._empty_header_text())
        self._header.setStyleSheet(
            "font-family: Menlo, Consolas, monospace; font-size: 12px;"
            " padding: 2px 4px;"
        )
        layout.addWidget(self._header)

        self._graphics = pg.GraphicsLayoutWidget()
        self._graphics.setBackground("#101014")
        layout.addWidget(self._graphics, stretch=1)

        self._alt_plot = self._make_plot("Altitude (ft)")
        self._spd_plot = self._make_plot("Ground speed (kt)")
        self._vr_plot = self._make_plot("Vertical rate (fpm)")

        self._graphics.addItem(self._alt_plot, row=0, col=0)
        self._graphics.addItem(self._spd_plot, row=0, col=1)
        self._graphics.addItem(self._vr_plot, row=0, col=2)

        # Link X axes so panning any single plot moves them all together.
        self._spd_plot.setXLink(self._alt_plot)
        self._vr_plot.setXLink(self._alt_plot)

        # Baseline reference at zero vertical rate.
        self._vr_zero = pg.InfiniteLine(
            pos=0.0,
            angle=0,
            pen=pg.mkPen(color=(120, 120, 130), width=1,
                         style=QtCore.Qt.PenStyle.DotLine),
        )
        self._vr_plot.addItem(self._vr_zero)

        self._alt_curve = self._alt_plot.plot(pen=pg.mkPen(_ALT_COLOR, width=1.5))
        self._spd_curve = self._spd_plot.plot(pen=pg.mkPen(_SPD_COLOR, width=1.5))
        self._vr_curve = self._vr_plot.plot(pen=pg.mkPen(_VRATE_COLOR, width=1.5))

        self.setMinimumHeight(180)

    # -- public API ---------------------------------------------------

    def set_selected_icao(self, icao: str | None) -> None:
        """Swap the aircraft whose telemetry is drawn."""

        normalized = icao.strip().upper() if icao else None
        if normalized == self._selected_icao:
            return
        self._selected_icao = normalized
        self.refresh()

    @property
    def selected_icao(self) -> str | None:
        return self._selected_icao

    def refresh(self, *, now: float | None = None) -> int:
        """Re-read telemetry for the selected aircraft. Returns the
        number of samples plotted on the altitude curve (the densest of
        the three)."""

        if self._selected_icao is None:
            self._header.setText(self._empty_header_text())
            self._alt_curve.setData([], [])
            self._spd_curve.setData([], [])
            self._vr_curve.setData([], [])
            return 0

        samples = self._store.telemetry(self._selected_icao)
        if not samples:
            self._header.setText(
                f"Selected: {self._selected_icao}   (no telemetry yet)"
            )
            self._alt_curve.setData([], [])
            self._spd_curve.setData([], [])
            self._vr_curve.setData([], [])
            return 0

        resolved_now = samples[-1].timestamp if now is None else float(now)
        series = telemetry_series(samples, now=resolved_now)

        self._alt_curve.setData(series.t_altitude_s, series.altitude_ft)
        self._spd_curve.setData(series.t_speed_s, series.ground_speed_kt)
        self._vr_curve.setData(series.t_vrate_s, series.vertical_rate_fpm)

        latest = samples[-1]
        alt_str = _format_optional(latest.altitude_ft, "{:,.0f} ft")
        spd_str = _format_optional(latest.ground_speed_kt, "{:,.0f} kt")
        vr_str = _format_optional(latest.vertical_rate_fpm, "{:+,.0f} fpm")
        span_s = samples[-1].timestamp - samples[0].timestamp
        self._header.setText(
            f"Selected: {self._selected_icao}   "
            f"alt={alt_str}   spd={spd_str}   vrate={vr_str}   "
            f"history={span_s:.0f}s ({len(samples)} samples)"
        )
        return int(series.t_altitude_s.size)

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _make_plot(title: str) -> pg.PlotItem:
        plot = pg.PlotItem()
        plot.setTitle(title, color="#c8c8d0", size="10pt")
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setLabel("bottom", "Time (s ago)")
        plot.getAxis("bottom").enableAutoSIPrefix(False)
        plot.getAxis("left").enableAutoSIPrefix(False)
        # Font for tick labels: mildly smaller so three plots fit.
        tick_font = QtGui.QFont("Menlo", 9)
        plot.getAxis("bottom").setStyle(tickFont=tick_font)
        plot.getAxis("left").setStyle(tickFont=tick_font)
        plot.setMouseEnabled(x=True, y=True)
        return plot

    @staticmethod
    def _empty_header_text() -> str:
        return "Select an aircraft (in the table or on the map) to see its history."


def _format_optional(value: float | None, fmt: str) -> str:
    return "-" if value is None else fmt.format(value)
