"""PyQtGraph window: live PSD on top, scrolling waterfall below."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from .sdr import DropOldestQueue, _BaseSource


# Starting range for the color scale before auto-calibration kicks in.
INITIAL_LEVELS_DB = (-100.0, 0.0)

# Auto-calibration collects this many hops before locking the color scale.
CALIBRATION_HOPS = 400


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
    ) -> None:
        super().__init__()
        self._source = source
        self._rows_queue = rows
        self._num_time_rows = int(num_time_rows)
        self._fft_size = source.fft_size
        self._sample_rate = source.sample_rate
        self._center_freq = source.center_freq
        self._hop_period_s = self._fft_size / self._sample_rate

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
        return (
            f"source={source_kind}   center={self._center_freq / 1e6:.3f} MHz   "
            f"rate={self._sample_rate / 1e6:.3f} MSPS   fft={self._fft_size}   "
            f"gain={gain}   hops/s={rows_per_sec:.0f}   "
            f"window={window_s:.1f}s ({self._hops_per_display_row}x max-hold)   "
            f"levels=[{vmin:.1f}, {vmax:.1f}] {cal_state}{live}"
        )

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

    def _maybe_update_status(self) -> None:
        elapsed_ms = self._last_status_ts.elapsed()
        if elapsed_ms >= 500:
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
            self._source.join(timeout=2.0)
        finally:
            super().closeEvent(event)
