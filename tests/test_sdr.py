import time

import numpy as np

from adsb_live.sdr import (
    DemoSource,
    DropOldestByteQueue,
    DropOldestQueue,
    RtlSdrSource,
    SpectrumRow,
)


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


def test_drop_oldest_byte_queue_preserves_order() -> None:
    q = DropOldestByteQueue(maxsize=4)
    q.put(b"one")
    q.put(b"two")
    q.put(b"three")

    assert q.get(timeout=0.01) == b"one"
    assert q.get(timeout=0.01) == b"two"
    assert q.get(timeout=0.01) == b"three"
    assert q.get(timeout=0.01) is None
    assert q.dropped == 0


def test_drop_oldest_byte_queue_discards_oldest_and_counts() -> None:
    q = DropOldestByteQueue(maxsize=2)
    q.put(b"a")
    q.put(b"b")
    q.put(b"c")
    q.put(b"d")

    assert q.get(timeout=0.01) == b"c"
    assert q.get(timeout=0.01) == b"d"
    assert q.dropped == 2


class _FakeSdr:
    """Tiny stand-in for pyrtlsdr's RtlSdr used by ``_process_bytes`` tests."""

    def __init__(self) -> None:
        self.iq_calls: list[bytes] = []

    def packed_bytes_to_iq(self, raw: bytes) -> np.ndarray:
        self.iq_calls.append(bytes(raw))
        arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        arr = (arr - 127.5) / 127.5
        i = arr[0::2]
        q = arr[1::2]
        return (i + 1j * q).astype(np.complex64)


def _make_rtl_source(iq_sink: DropOldestByteQueue | None) -> RtlSdrSource:
    return RtlSdrSource(
        center_freq=1090e6,
        sample_rate=2.4e6,
        gain="max",
        fft_size=256,
        rows=DropOldestQueue(maxsize=32),
        iq_sink=iq_sink,
    )


def test_process_bytes_tees_raw_block_to_iq_sink_and_emits_hops() -> None:
    sink = DropOldestByteQueue(maxsize=8)
    source = _make_rtl_source(iq_sink=sink)
    sdr = _FakeSdr()

    # 2 hops worth of interleaved UC8 samples.
    raw = bytes(range(256)) * 4

    source._process_bytes(sdr, raw)

    assert sink.get(timeout=0.01) == raw
    assert sdr.iq_calls == [raw]
    batch = source.rows.get_batch()
    assert len(batch) == raw.__len__() // (2 * source.fft_size)
    assert all(row.power_db.shape == (source.fft_size,) for row in batch)


def test_process_bytes_without_sink_still_emits_hops() -> None:
    source = _make_rtl_source(iq_sink=None)
    sdr = _FakeSdr()
    raw = bytes(range(256)) * 4

    source._process_bytes(sdr, raw)

    assert sdr.iq_calls == [raw]
    assert source.rows.get_batch()


def test_process_bytes_drops_short_reads() -> None:
    sink = DropOldestByteQueue(maxsize=4)
    source = _make_rtl_source(iq_sink=sink)
    sdr = _FakeSdr()

    source._process_bytes(sdr, b"")
    source._process_bytes(sdr, b"\x00" * 4)

    assert sink.get(timeout=0.01) is None
    assert sdr.iq_calls == []
    assert source.rows.get_batch() == []
