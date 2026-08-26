"""PyQtGraph window: live PSD on top, scrolling waterfall below."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from .sdr import DropOldestQueue, _BaseSource


DEFAULT_LEVELS_DB = (-60.0, 20.0)


class WaterfallWindow(QtWidgets.QMainWindow):
    """Main application window."""

    def __init__(
        self,
        *,
        source: _BaseSource,
        rows: DropOldestQueue,
        num_time_rows: int = 400,
        refresh_ms: int = 33,
        levels_db: tuple[float, float] = DEFAULT_LEVELS_DB,
    ) -> None:
        super().__init__()
        self._source = source
        self._rows_queue = rows
        self._num_time_rows = int(num_time_rows)
        self._fft_size = source.fft_size
        self._sample_rate = source.sample_rate
        self._center_freq = source.center_freq
        self._hop_period_s = self._fft_size / self._sample_rate
        self._peak_hold: Optional[np.ndarray] = None
        self._peak_decay_db = 0.5  # per UI tick

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
        self._psd_plot.setLabel("bottom", "Frequency", units="MHz")
        self._psd_plot.setLabel("left", "Power", units="dB")
        self._psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self._psd_plot.setYRange(levels_db[0], levels_db[1])
        self._psd_plot.setXRange(f_lo_mhz, f_hi_mhz, padding=0)
        self._psd_plot.setMouseEnabled(x=True, y=True)

        self._psd_curve = self._psd_plot.plot(
            self._freq_axis_mhz,
            np.full(self._fft_size, levels_db[0], dtype=np.float32),
            pen=pg.mkPen(color=(180, 220, 255), width=1),
            name="Instantaneous",
        )
        self._peak_curve = self._psd_plot.plot(
            self._freq_axis_mhz,
            np.full(self._fft_size, levels_db[0], dtype=np.float32),
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
        self._wf_plot.setLabel("bottom", "Frequency", units="MHz")
        self._wf_plot.setLabel("left", "Time (s ago)")
        self._wf_plot.setXLink(self._psd_plot)
        self._wf_plot.invertY(True)
        self._wf_plot.setMouseEnabled(x=True, y=True)

        self._waterfall_buffer = np.full(
            (self._num_time_rows, self._fft_size), levels_db[0], dtype=np.float32
        )

        self._wf_image = pg.ImageItem(axisOrder="row-major")
        cmap = pg.colormap.get("viridis")
        self._wf_image.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self._wf_image.setLevels(list(levels_db))
        self._wf_image.setImage(
            self._waterfall_buffer, autoLevels=False, autoDownsample=True
        )
        self._wf_image.setRect(
            QtCore.QRectF(
                f_lo_mhz,
                0.0,
                f_span_mhz,
                self._num_time_rows * self._hop_period_s,
            )
        )
        self._wf_plot.addItem(self._wf_image)
        self._wf_plot.setYRange(0.0, self._num_time_rows * self._hop_period_s, padding=0)

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
    def _status_text(self, rows_per_sec: float) -> str:
        source_kind = type(self._source).__name__
        gain = getattr(self._source, "configured_gain", "?")
        return (
            f"source={source_kind}   center={self._center_freq / 1e6:.3f} MHz   "
            f"rate={self._sample_rate / 1e6:.3f} MSPS   fft={self._fft_size}   "
            f"gain={gain}   hops/s={rows_per_sec:.0f}"
        )

    def _on_tick(self) -> None:
        if self._source.error is not None:
            self._show_error(self._source.error)
            self._timer.stop()
            return

        batch = self._rows_queue.get_batch()
        if batch:
            n_new = len(batch)
            self._rows_since_status += n_new

            if n_new >= self._num_time_rows:
                for i, row in enumerate(batch[-self._num_time_rows :]):
                    self._waterfall_buffer[i, :] = row.power_db
            else:
                self._waterfall_buffer = np.roll(
                    self._waterfall_buffer, n_new, axis=0
                )
                # Newest row lives at index 0 (top of the inverted-y plot).
                for i, row in enumerate(reversed(batch)):
                    self._waterfall_buffer[i, :] = row.power_db

            self._wf_image.setImage(
                self._waterfall_buffer, autoLevels=False, autoDownsample=True
            )

            latest = batch[-1].power_db
            self._psd_curve.setData(self._freq_axis_mhz, latest)

            if self._peak_hold is None:
                self._peak_hold = latest.copy()
            else:
                self._peak_hold = np.maximum(
                    self._peak_hold - self._peak_decay_db, latest
                )
            self._peak_curve.setData(self._freq_axis_mhz, self._peak_hold)

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
