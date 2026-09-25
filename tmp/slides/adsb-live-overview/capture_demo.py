"""Grab a real demo-mode screenshot of the adsb-live window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from adsb_live.sdr import DemoSource, DropOldestQueue
from adsb_live.ui import WaterfallWindow


def main() -> int:
    out = Path(sys.argv[1]).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    rows = DropOldestQueue(maxsize=512)
    source = DemoSource(
        center_freq=1090e6,
        sample_rate=2.4e6,
        fft_size=256,
        rows=rows,
    )
    source.start()
    window = WaterfallWindow(
        source=source,
        rows=rows,
        num_time_rows=500,
        refresh_ms=33,
        time_window_s=5.0,
        decoder=None,
    )
    window.resize(1280, 820)
    window.show()
    window.raise_()
    window.activateWindow()

    def capture() -> None:
        pixmap = window.grab()
        if not pixmap.save(str(out), "PNG"):
            raise SystemExit(f"failed to save screenshot to {out}")
        source.stop()
        source.join(timeout=2.0)
        app.quit()

    QTimer.singleShot(6500, capture)
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
