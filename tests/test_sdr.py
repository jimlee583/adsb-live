import time

import numpy as np

from adsb_live.sdr import DemoSource, DropOldestQueue, SpectrumRow


def _row(timestamp: float) -> SpectrumRow:
    return SpectrumRow(
        timestamp=timestamp,
        center_freq=1090e6,
        sample_rate=2.4e6,
        power_db=np.array([timestamp], dtype=np.float32),
    )


def test_drop_oldest_queue_preserves_order_and_batch_limit() -> None:
    rows = DropOldestQueue(maxsize=3)
    rows.put(_row(1.0))
    rows.put(_row(2.0))
    rows.put(_row(3.0))

    assert [row.timestamp for row in rows.get_batch(max_items=2)] == [1.0, 2.0]
    assert [row.timestamp for row in rows.get_batch()] == [3.0]
    assert rows.get_batch() == []


def test_drop_oldest_queue_discards_oldest_row_when_full() -> None:
    rows = DropOldestQueue(maxsize=2)
    rows.put(_row(1.0))
    rows.put(_row(2.0))
    rows.put(_row(3.0))

    assert [row.timestamp for row in rows.get_batch()] == [2.0, 3.0]


def test_demo_source_emits_spectrum_rows_and_stops_cleanly() -> None:
    rows = DropOldestQueue(maxsize=32)
    source = DemoSource(
        center_freq=1090e6,
        sample_rate=25_600.0,
        fft_size=256,
        rows=rows,
    )

    source.start()
    deadline = time.monotonic() + 1.0
    batch: list[SpectrumRow] = []
    try:
        while not batch and time.monotonic() < deadline:
            batch = rows.get_batch()
            if not batch:
                time.sleep(0.01)
    finally:
        source.stop()
        source.join(timeout=1.0)

    assert not source.is_alive()
    assert source.error is None
    assert source.configured_gain == "demo"
    assert batch

    row = batch[-1]
    assert row.center_freq == 1090e6
    assert row.sample_rate == 25_600.0
    assert row.power_db.shape == (256,)
    assert row.power_db.dtype == np.float32
    assert np.all(np.isfinite(row.power_db))
