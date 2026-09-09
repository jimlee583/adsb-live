"""PyQtGraph window: live PSD on top, scrolling waterfall below."""

from __future__ import annotations

import time
from typing import Optional

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from .aircraft_columns import Column, RowContext, build_columns
from .decoder import LiveDecoder
from .map_view import AircraftMapWidget
from .sdr import DropOldestQueue, _BaseSource
from .tracks import AircraftTrack, TrackStore


# Starting range for the color scale before auto-calibration kicks in.
INITIAL_LEVELS_DB = (-100.0, 0.0)

# Auto-calibration collects this many hops before locking the color scale.
CALIBRATION_HOPS = 400


class _SelectionCoordinator(QtCore.QObject):
    """Keep the aircraft table and the map showing the same selected ICAO.

    Signals travel in both directions:

    * Table row selected --> resolve ICAO from column 0 --> map highlight.
    * Map click --> table row selection --> table sync.

    The ``_applying`` guard breaks the loop so a programmatic change on
    one side does not bounce back into the other.
    """

    def __init__(
        self,
        *,
        view: QtWidgets.QTableView,
        proxy: QtCore.QSortFilterProxyModel,
        model: "AircraftTableModel",
        map_widget: Optional[AircraftMapWidget],
    ) -> None:
        super().__init__(view)
        self._view = view
        self._proxy = proxy
        self._model = model
        self._map = map_widget
        self._applying = False
        view.selectionModel().currentRowChanged.connect(self._on_row_changed)
        if map_widget is not None:
            map_widget.aircraftSelected.connect(self._on_map_selected)

    def _on_row_changed(
        self, current: QtCore.QModelIndex, _previous: QtCore.QModelIndex
    ) -> None:
        if self._applying or self._map is None:
            return
        icao = self._icao_from_proxy_index(current)
        self._applying = True
        try:
            self._map.set_selected_icao(icao)
        finally:
            self._applying = False

    def _on_map_selected(self, icao: str) -> None:
        if self._applying:
            return
        self._applying = True
        try:
            if not icao:
                self._view.clearSelection()
                return
            row = self._find_row_for_icao(icao)
            if row is None:
                self._view.clearSelection()
                return
            proxy_index = self._proxy.index(row, 0)
            self._view.setCurrentIndex(proxy_index)
            self._view.selectRow(row)
        finally:
            self._applying = False

    def _icao_from_proxy_index(
        self, proxy_index: QtCore.QModelIndex
    ) -> str | None:
        if not proxy_index.isValid():
            return None
        source = self._proxy.mapToSource(proxy_index)
        icao_col = 0  # build_columns always puts ICAO first.
        value = self._model.data(
            self._model.index(source.row(), icao_col),
            QtCore.Qt.ItemDataRole.DisplayRole,
        )
        return str(value) if value else None

    def _find_row_for_icao(self, icao: str) -> int | None:
        target = icao.strip().upper()
        for proxy_row in range(self._proxy.rowCount()):
            proxy_index = self._proxy.index(proxy_row, 0)
            value = self._proxy.data(
                proxy_index, QtCore.Qt.ItemDataRole.DisplayRole
            )
            if value and str(value).strip().upper() == target:
                return proxy_row
        return None


class AircraftTableModel(QtCore.QAbstractTableModel):
    """Qt table model backed by an :class:`~adsb_live.tracks.TrackStore` snapshot.

    The model does not observe the store directly; instead, the owning
    window calls :meth:`refresh` on a low-frequency timer to swap in a
    fresh snapshot. Refresh cost is proportional to the number of active
    aircraft (typically < 200) so this is cheap.
    """

    def __init__(
        self,
        *,
        store: TrackStore,
        columns: tuple[Column, ...],
        receiver: tuple[float, float] | None = None,
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._columns = columns
        self._receiver = receiver
        self._tracks: tuple[AircraftTrack, ...] = ()
        self._now = time.monotonic()

    # -- QAbstractTableModel API ---------------------------------------
    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._tracks)

    def columnCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._columns)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: QtCore.Qt.Orientation,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == QtCore.Qt.Orientation.Horizontal:
            if 0 <= section < len(self._columns):
                return self._columns[section].label
        return None

    def data(
        self,
        index: QtCore.QModelIndex,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self._tracks):
            return None
        if col < 0 or col >= len(self._columns):
            return None
        column = self._columns[col]
        ctx = RowContext(
            track=self._tracks[row], now=self._now, receiver=self._receiver
        )
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return column.display(ctx)
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return int(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
        if role == QtCore.Qt.ItemDataRole.UserRole:
            # Sort role: expose the raw comparable key so the proxy sorts
            # numerically rather than lexicographically.
            key = column.sort_key(ctx)
            if key is None:
                # None sorts last regardless of ascending/descending.
                return float("inf")
            return key
        return None

    # -- refresh -------------------------------------------------------
    def refresh(self, *, now: float | None = None) -> int:
        """Reload from the store. Returns the current aircraft count."""
        snapshot = self._store.snapshot()
        self._now = time.monotonic() if now is None else float(now)
        self.beginResetModel()
        self._tracks = snapshot
        self.endResetModel()
        return len(snapshot)

    @property
    def columns(self) -> tuple[Column, ...]:
        return self._columns


class WaterfallWindow(QtWidgets.QMainWindow):
    """Main application window.

    The waterfall is built with **max-hold aggregation**: many incoming FFT
    hops collapse into one displayed row by taking the per-bin maximum. Short
    ADS-B bursts therefore keep their full peak power in the display no matter
    how many quiet hops surround them, while the row rate stays low enough
    that seconds of history fit on screen.
    """

    def __init__(
        self,
        *,
        source: _BaseSource,
        rows: DropOldestQueue,
        num_time_rows: int = 500,
        refresh_ms: int = 33,
        levels_db: Optional[tuple[float, float]] = None,
        time_window_s: float = 5.0,
        decoder: Optional[LiveDecoder] = None,
        table_refresh_ms: int = 1000,
    ) -> None:
        super().__init__()
        self._source = source
        self._rows_queue = rows
        self._decoder = decoder
        self._num_time_rows = int(num_time_rows)
        self._fft_size = source.fft_size
        self._sample_rate = source.sample_rate
        self._center_freq = source.center_freq
        self._hop_period_s = self._fft_size / self._sample_rate
        self._aircraft_count = 0
        # Primed before the status label is built so ``_status_text`` /
        # ``_decoder_status_text`` can safely read it during construction.
        # Replaced with an ``AircraftMapWidget`` further down when the
        # decoder is present *and* a receiver lat/lon is configured.
        self._aircraft_map: AircraftMapWidget | None = None

        # How many FFT hops max-fold into one displayed waterfall row.
        hop_rate = 1.0 / self._hop_period_s
        target_rows_per_s = max(1.0, self._num_time_rows / max(time_window_s, 0.1))
        self._hops_per_display_row = max(1, int(round(hop_rate / target_rows_per_s)))
        self._display_row_period_s = (
            self._hops_per_display_row * self._hop_period_s
        )

        # DC-notch: LO leakage puts a strong spike right at center_freq. Cover
        # a few bins on each side of DC with the median of the neighbours so
        # it doesn't dominate the display or calibration.
        self._dc_half_bins = max(2, self._fft_size // 128)

        self._peak_hold: Optional[np.ndarray] = None
        # Fade slowly so short bursts leave a visible trace.
        self._peak_decay_db = 0.4  # per UI tick

        # Rolling accumulator for max-hold aggregation.
        self._acc_row: Optional[np.ndarray] = None
        self._acc_count = 0

        # Auto-calibration: if the caller pinned levels via CLI we skip it.
        self._levels: tuple[float, float]
        self._auto_levels = levels_db is None
        self._calibration_hops: list[np.ndarray] = []
        self._levels = levels_db if levels_db is not None else INITIAL_LEVELS_DB

        # Live dB stats for the status bar.
        self._latest_min = float("nan")
        self._latest_med = float("nan")
        self._latest_max = float("nan")

        self.setWindowTitle("adsb-live \u2014 1090 MHz waterfall")
        self.resize(1100, 780)

        central = QtWidgets.QWidget(self)
        vbox = QtWidgets.QVBoxLayout(central)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)

        self._status_label = QtWidgets.QLabel(self._status_text(rows_per_sec=0.0))
        self._status_label.setStyleSheet(
            "font-family: Menlo, Consolas, monospace; font-size: 12px;"
        )
        vbox.addWidget(self._status_label)

        self._graphics = pg.GraphicsLayoutWidget(show=False)
        self._graphics.setBackground("#101014")

        if self._decoder is not None:
            receiver = None
            lat = self._decoder.process.receiver_lat
            lon = self._decoder.process.receiver_lon
            if lat is not None and lon is not None:
                receiver = (lat, lon)
            self._receiver = receiver
            columns = build_columns(receiver=receiver)
            self._aircraft_model = AircraftTableModel(
                store=self._decoder.store,
                columns=columns,
                receiver=receiver,
                parent=self,
            )
            self._aircraft_proxy = QtCore.QSortFilterProxyModel(self)
            self._aircraft_proxy.setSourceModel(self._aircraft_model)
            self._aircraft_proxy.setSortRole(QtCore.Qt.ItemDataRole.UserRole)
            self._aircraft_view = QtWidgets.QTableView()
            self._aircraft_view.setModel(self._aircraft_proxy)
            self._aircraft_view.setSortingEnabled(True)
            self._aircraft_view.sortByColumn(
                0, QtCore.Qt.SortOrder.AscendingOrder
            )
            self._aircraft_view.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            )
            self._aircraft_view.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
            )
            self._aircraft_view.setAlternatingRowColors(True)
            self._aircraft_view.verticalHeader().setVisible(False)
            header = self._aircraft_view.horizontalHeader()
            header.setSectionResizeMode(
                QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
            header.setStretchLastSection(True)
            self._aircraft_view.setFont(
                QtGui.QFont("Menlo", 11)
            )

            splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
            splitter.addWidget(self._graphics)
            if receiver is not None:
                self._aircraft_map = AircraftMapWidget(
                    store=self._decoder.store,
                    receiver=receiver,
                    parent=self,
                )
                splitter.addWidget(self._aircraft_map)
            splitter.addWidget(self._aircraft_view)
            if self._aircraft_map is not None:
                splitter.setStretchFactor(0, 3)
                splitter.setStretchFactor(1, 2)
                splitter.setStretchFactor(2, 2)
            else:
                splitter.setStretchFactor(0, 3)
                splitter.setStretchFactor(1, 2)
            splitter.setChildrenCollapsible(False)
            vbox.addWidget(splitter, stretch=1)

            self._selection_coordinator = _SelectionCoordinator(
                view=self._aircraft_view,
                proxy=self._aircraft_proxy,
                model=self._aircraft_model,
                map_widget=self._aircraft_map,
            )

            self._table_timer = QtCore.QTimer(self)
            self._table_timer.setTimerType(QtCore.Qt.TimerType.CoarseTimer)
            self._table_timer.timeout.connect(self._on_table_tick)
            self._table_timer.start(int(table_refresh_ms))
        else:
            self._receiver = None
            self._aircraft_model = None
            self._aircraft_proxy = None
            self._aircraft_view = None
            self._aircraft_map = None
            self._selection_coordinator = None
            vbox.addWidget(self._graphics, stretch=1)

        self.setCentralWidget(central)

        freq_axis_hz = source.spectrum.frequency_axis(
            self._center_freq, self._sample_rate
        )
        self._freq_axis_mhz = (freq_axis_hz / 1e6).astype(np.float64)
        f_lo_mhz = float(self._freq_axis_mhz[0])
        f_hi_mhz = float(self._freq_axis_mhz[-1])
        f_span_mhz = f_hi_mhz - f_lo_mhz

        # --- PSD plot -------------------------------------------------------
        self._psd_plot = self._graphics.addPlot(row=0, col=0)
        self._configure_freq_axis(self._psd_plot.getAxis("bottom"))
        self._psd_plot.setLabel("left", "Power (dB)")
        self._psd_plot.getAxis("left").enableAutoSIPrefix(False)
        self._psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self._psd_plot.setYRange(self._levels[0], self._levels[1])
        self._psd_plot.setXRange(f_lo_mhz, f_hi_mhz, padding=0)
        self._psd_plot.setMouseEnabled(x=True, y=True)

        self._psd_curve = self._psd_plot.plot(
            self._freq_axis_mhz,
            np.full(self._fft_size, self._levels[0], dtype=np.float32),
            pen=pg.mkPen(color=(180, 220, 255), width=1),
            name="Instantaneous",
        )
        self._peak_curve = self._psd_plot.plot(
            self._freq_axis_mhz,
            np.full(self._fft_size, self._levels[0], dtype=np.float32),
            pen=pg.mkPen(color=(255, 190, 90), width=1, style=QtCore.Qt.PenStyle.DashLine),
            name="Peak hold",
        )
        center_line = pg.InfiniteLine(
            pos=self._center_freq / 1e6,
            angle=90,
            pen=pg.mkPen(color=(120, 120, 130), width=1, style=QtCore.Qt.PenStyle.DotLine),
        )
        self._psd_plot.addItem(center_line)

        # --- Waterfall ------------------------------------------------------
        self._wf_plot = self._graphics.addPlot(row=1, col=0)
        self._configure_freq_axis(self._wf_plot.getAxis("bottom"))
        self._wf_plot.setLabel("left", "Time (s ago)")
        self._wf_plot.getAxis("left").enableAutoSIPrefix(False)
        self._wf_plot.setXLink(self._psd_plot)
        self._wf_plot.invertY(True)
        self._wf_plot.setMouseEnabled(x=True, y=True)

        self._waterfall_buffer = np.full(
            (self._num_time_rows, self._fft_size),
            self._levels[0],
            dtype=np.float32,
        )

        self._wf_image = pg.ImageItem(axisOrder="row-major")
        cmap = pg.colormap.get("viridis")
        self._wf_image.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self._wf_image.setLevels(list(self._levels))
        self._wf_image.setImage(
            self._waterfall_buffer, autoLevels=False, autoDownsample=True
        )
        window_seconds = self._num_time_rows * self._display_row_period_s
        self._wf_image.setRect(
            QtCore.QRectF(f_lo_mhz, 0.0, f_span_mhz, window_seconds)
        )
        self._wf_plot.addItem(self._wf_image)
        self._wf_plot.setYRange(0.0, window_seconds, padding=0)

        # Layout weighting: PSD gets ~1/3, waterfall ~2/3.
        self._graphics.ci.layout.setRowStretchFactor(0, 1)
        self._graphics.ci.layout.setRowStretchFactor(1, 2)

        # --- Timer ----------------------------------------------------------
        self._rows_since_status = 0
        self._last_status_ts = QtCore.QElapsedTimer()
        self._last_status_ts.start()

        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(int(refresh_ms))

    # ------------------------------------------------------------------
    @staticmethod
    def _configure_freq_axis(axis: pg.AxisItem) -> None:
        """Set a fixed MHz label without pyqtgraph's SI auto-prefixing."""
        axis.enableAutoSIPrefix(False)
        axis.setLabel(text="Frequency (MHz)")

    def _status_text(self, rows_per_sec: float) -> str:
        source_kind = type(self._source).__name__
        gain = getattr(self._source, "configured_gain", "?")
        vmin, vmax = self._levels
        cal_state = "auto" if self._auto_levels else "fixed"
        if self._auto_levels and self._calibration_hops:
            cal_state = f"calibrating {len(self._calibration_hops)}/{CALIBRATION_HOPS}"
        window_s = self._num_time_rows * self._display_row_period_s
        if not np.isnan(self._latest_med):
            live = (
                f"   dB: min={self._latest_min:6.1f}  med={self._latest_med:6.1f}  "
                f"max={self._latest_max:6.1f}"
            )
        else:
            live = ""
        base = (
            f"source={source_kind}   center={self._center_freq / 1e6:.3f} MHz   "
            f"rate={self._sample_rate / 1e6:.3f} MSPS   fft={self._fft_size}   "
            f"gain={gain}   hops/s={rows_per_sec:.0f}   "
            f"window={window_s:.1f}s ({self._hops_per_display_row}x max-hold)   "
            f"levels=[{vmin:.1f}, {vmax:.1f}] {cal_state}{live}"
        )
        if self._decoder is None:
            return base
        return base + "\n" + self._decoder_status_text()

    def _decoder_status_text(self) -> str:
        decoder = self._decoder
        assert decoder is not None
        proc_health = decoder.process.health
        poll_health = decoder.poller.health
        proc_state = "running" if proc_health.running else "stopped"
        if proc_health.restarts:
            proc_state += f" (restarts={proc_health.restarts})"
        bytes_written = proc_health.bytes_written
        if bytes_written >= 1_000_000:
            bytes_str = f"{bytes_written / 1e6:.1f} MB"
        elif bytes_written >= 1_000:
            bytes_str = f"{bytes_written / 1e3:.0f} kB"
        else:
            bytes_str = f"{bytes_written} B"

        # Counts we can actually reason about:
        #   messages: dump1090's own count of valid Mode-S frames decoded
        #             (from aircraft.json). If this stays at 0 while
        #             snapshots grow, the receiver isn't seeing any ADS-B.
        #   positions: aircraft in the store with a known lat/lon right now.
        messages = poll_health.last_message_count
        messages_str = "?" if messages is None else f"{messages:,}"
        positions = sum(
            1
            for t in decoder.store.snapshot()
            if t.latitude is not None and t.longitude is not None
        )

        dropped = decoder.iq_sink.dropped
        # Only surface *real* errors, not dump1090's informational stderr.
        # ``poll_health.last_error`` is set when the poller itself failed
        # (e.g. aircraft.json missing or malformed).
        err_tail = (
            f"   err={poll_health.last_error}"
            if poll_health.last_error
            else ""
        )
        map_note = (
            "" if self._aircraft_map is not None else "   map=off (needs --lat/--lon)"
        )
        return (
            f"decoder={proc_state}   aircraft={self._aircraft_count}   "
            f"positions={positions}   messages={messages_str}   "
            f"snapshots={poll_health.snapshots_processed}   "
            f"stdin={bytes_str}   sink-drops={dropped}{map_note}{err_tail}"
        )

    def _on_table_tick(self) -> None:
        if self._aircraft_model is None:
            return
        self._aircraft_count = self._aircraft_model.refresh()
        if self._aircraft_map is not None:
            self._aircraft_map.refresh()
        self._maybe_update_status(force=True)

    def _apply_levels(self, vmin: float, vmax: float) -> None:
        self._levels = (float(vmin), float(vmax))
        self._wf_image.setLevels(list(self._levels))
        self._psd_plot.setYRange(vmin, vmax)

    def _dc_notch(self, db: np.ndarray) -> np.ndarray:
        """Replace the DC-leakage bins with the median of their neighbours."""
        n = db.shape[0]
        dc = n // 2
        half = self._dc_half_bins
        if half <= 0 or n <= 4 * half:
            return db
        left = db[max(0, dc - 4 * half) : dc - half]
        right = db[dc + half + 1 : min(n, dc + 4 * half + 1)]
        neigh = np.concatenate([left, right]) if left.size or right.size else db
        fill = float(np.median(neigh))
        out = db.copy()
        out[dc - half : dc + half + 1] = fill
        return out

    def _maybe_calibrate(self, hop: np.ndarray) -> None:
        if not self._auto_levels:
            return
        self._calibration_hops.append(hop.copy())
        if len(self._calibration_hops) >= CALIBRATION_HOPS:
            stack = np.stack(self._calibration_hops, axis=0)
            noise = float(np.percentile(stack, 30))
            peak = float(np.percentile(stack, 99.95))
            headroom = max(20.0, peak - noise + 3.0)
            self._apply_levels(noise - 4.0, noise + headroom)
            self._auto_levels = False
            self._calibration_hops.clear()

    def _push_display_row(self, row: np.ndarray) -> None:
        self._waterfall_buffer = np.roll(self._waterfall_buffer, 1, axis=0)
        # Newest row lives at index 0 (top of the inverted-y plot).
        self._waterfall_buffer[0, :] = row

    def _on_tick(self) -> None:
        if self._source.error is not None:
            self._show_error(self._source.error)
            self._timer.stop()
            return

        batch = self._rows_queue.get_batch()
        if not batch:
            self._maybe_update_status()
            return

        self._rows_since_status += len(batch)

        display_rows_added = 0
        for spec in batch:
            hop = self._dc_notch(spec.power_db)
            self._maybe_calibrate(hop)

            if self._acc_row is None:
                self._acc_row = hop.copy()
                self._acc_count = 1
            else:
                np.maximum(self._acc_row, hop, out=self._acc_row)
                self._acc_count += 1

            if self._acc_count >= self._hops_per_display_row:
                self._push_display_row(self._acc_row)
                display_rows_added += 1
                self._acc_row = None
                self._acc_count = 0

        if display_rows_added > 0:
            self._wf_image.setImage(
                self._waterfall_buffer, autoLevels=False, autoDownsample=True
            )

        # PSD line reflects the latest raw hop, with the DC bins notched.
        latest = self._dc_notch(batch[-1].power_db)
        self._psd_curve.setData(self._freq_axis_mhz, latest)

        if self._peak_hold is None:
            self._peak_hold = latest.copy()
        else:
            self._peak_hold = np.maximum(
                self._peak_hold - self._peak_decay_db, latest
            )
        self._peak_curve.setData(self._freq_axis_mhz, self._peak_hold)

        self._latest_min = float(latest.min())
        self._latest_max = float(latest.max())
        self._latest_med = float(np.median(latest))

        self._maybe_update_status()

    def _maybe_update_status(self, *, force: bool = False) -> None:
        elapsed_ms = self._last_status_ts.elapsed()
        if force or elapsed_ms >= 500:
            rate = self._rows_since_status * 1000.0 / max(elapsed_ms, 1)
            self._status_label.setText(self._status_text(rows_per_sec=rate))
            self._rows_since_status = 0
            self._last_status_ts.restart()

    def _show_error(self, exc: BaseException) -> None:
        QtWidgets.QMessageBox.critical(
            self,
            "SDR error",
            f"The SDR source failed:\n\n{type(exc).__name__}: {exc}",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt signature)
        try:
            self._source.stop()
            if self._source.is_alive():
                self._source.join(timeout=2.0)
            if self._decoder is not None:
                self._decoder.stop()
        finally:
            super().closeEvent(event)
