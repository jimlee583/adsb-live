import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import adsb_live.dump1090 as dump1090
from adsb_live import (
    Dump1090FormatError,
    Dump1090JsonSource,
    FileSnapshotReader,
    HttpSnapshotReader,
    TrackStore,
    parse_aircraft_json,
)


FIXTURES = Path(__file__).parent / "fixtures" / "dump1090"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_normal_snapshot_normalizes_aircraft_and_ground_state() -> None:
    snapshot = parse_aircraft_json(
        _fixture("aircraft-normal.json"), observed_at=1000.0
    )

    assert snapshot.generated_at == 1788807000.25
    assert snapshot.message_count == 12000
    assert snapshot.rejected_records == 1
    assert [track.icao for track in snapshot.tracks] == ["A1B2C3", "C0FFEE"]

    airborne, ground = snapshot.tracks
    assert airborne.callsign == "UAL123"
    assert airborne.last_seen == pytest.approx(999.6)
    assert airborne.altitude_ft == 32000
    assert airborne.on_ground is False
    assert airborne.latitude == pytest.approx(39.8561)
    assert airborne.longitude == pytest.approx(-104.6737)
    assert airborne.ground_speed_kt == pytest.approx(451.2)
    assert airborne.track_deg == pytest.approx(87.5)
    assert airborne.vertical_rate_fpm == -640
    assert airborne.squawk == "1200"
    assert airborne.category == "A3"
    assert airborne.signal_db == pytest.approx(-18.4)

    assert ground.callsign == "N123AB"
    assert ground.altitude_ft is None
    assert ground.on_ground is True


def test_parse_snapshot_accepts_missing_optional_fields() -> None:
    snapshot = parse_aircraft_json(
        {"aircraft": [{"hex": "abc123"}]}, observed_at=10.0
    )

    assert snapshot.generated_at is None
    assert snapshot.message_count is None
    assert snapshot.rejected_records == 0
    track = snapshot.tracks[0]
    assert track.icao == "ABC123"
    assert track.last_seen == 10.0
    assert track.callsign is None
    assert track.latitude is None
    assert track.altitude_ft is None
    assert track.on_ground is None


def test_parse_snapshot_skips_malformed_records_without_losing_valid_ones() -> None:
    snapshot = parse_aircraft_json(
        _fixture("aircraft-malformed.json"), observed_at=100.0
    )

    assert snapshot.rejected_records == 4
    assert len(snapshot.tracks) == 1
    track = snapshot.tracks[0]
    assert track.icao == "ABC123"
    assert track.last_seen == pytest.approx(98.5)
    assert track.altitude_ft == 18000
    assert track.on_ground is False
    assert track.vertical_rate_fpm == 256


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{", "invalid JSON"),
        (b"[]", "root must be an object"),
        (b"{}", "aircraft must be an array"),
        (b'{"aircraft": {}}', "aircraft must be an array"),
        (b'{"aircraft": [], "messages": 1.5}', "non-negative integer"),
    ],
)
def test_parse_snapshot_rejects_malformed_documents(
    payload: bytes, message: str
) -> None:
    with pytest.raises(Dump1090FormatError, match=message):
        parse_aircraft_json(payload, observed_at=10.0)


def test_file_reader_returns_snapshot_bytes(tmp_path: Path) -> None:
    path = tmp_path / "aircraft.json"
    path.write_bytes(_fixture("aircraft-normal.json"))

    assert FileSnapshotReader(path)() == _fixture("aircraft-normal.json")


def test_http_reader_fetches_snapshot_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("aircraft-normal.json")
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return payload

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(dump1090, "urlopen", fake_urlopen)

    assert HttpSnapshotReader("http://receiver/data/aircraft.json", 3.5)() == payload
    assert captured["timeout"] == 3.5
    assert captured["request"].full_url == "http://receiver/data/aircraft.json"


class SequenceReader:
    def __init__(self, values: list[bytes | BaseException]) -> None:
        self._values: Iterator[bytes | BaseException] = iter(values)

    def __call__(self) -> bytes:
        value = next(self._values)
        if isinstance(value, BaseException):
            raise value
        return value


def test_poll_once_updates_store_and_source_health() -> None:
    store = TrackStore(stale_after_s=60.0, clock=lambda: 1000.0)
    source = Dump1090JsonSource(
        reader=lambda: _fixture("aircraft-normal.json"),
        store=store,
        clock=lambda: 1000.0,
    )

    snapshot = source.poll_once()

    assert len(store.snapshot()) == 2
    assert source.health.last_success_at == 1000.0
    assert source.health.last_error is None
    assert source.health.consecutive_failures == 0
    assert source.health.snapshots_processed == 1
    assert source.health.rejected_records == snapshot.rejected_records == 1
    assert source.health.last_message_count == snapshot.message_count


def test_poll_once_populates_message_count_from_snapshot() -> None:
    payloads = iter([
        json.dumps({"now": 1.0, "messages": 42, "aircraft": []}),
        json.dumps({"now": 2.0, "messages": 100, "aircraft": []}),
    ])
    source = Dump1090JsonSource(
        reader=lambda: next(payloads),
        store=TrackStore(stale_after_s=60.0, clock=lambda: 2.0),
        clock=lambda: 2.0,
    )

    source.poll_once()
    assert source.health.last_message_count == 42

    source.poll_once()
    assert source.health.last_message_count == 100


def test_poll_once_expires_aircraft_older_than_store_limit() -> None:
    now = 100.0
    store = TrackStore(stale_after_s=60.0, clock=lambda: now)
    source = Dump1090JsonSource(
        reader=lambda: json.dumps(
            {"messages": 1, "aircraft": [{"hex": "abc123", "seen": 60.1}]}
        ),
        store=store,
        clock=lambda: now,
    )

    source.poll_once()

    assert store.snapshot() == ()


def test_source_health_recovers_after_a_failed_snapshot() -> None:
    times = iter([100.0, 101.0])
    reader = SequenceReader(
        [b"{", b'{"messages": 2, "aircraft": [{"hex": "abc123"}]}']
    )
    source = Dump1090JsonSource(
        reader=reader,
        store=TrackStore(stale_after_s=60.0, clock=lambda: 101.0),
        clock=lambda: next(times),
    )

    with pytest.raises(Dump1090FormatError):
        source.poll_once()
    assert source.health.last_success_at is None
    assert source.health.consecutive_failures == 1
    assert "Dump1090FormatError" in (source.health.last_error or "")

    source.poll_once()
    assert source.health.last_success_at == 101.0
    assert source.health.last_error is None
    assert source.health.consecutive_failures == 0
    assert source.health.snapshots_processed == 1


def test_source_detects_decoder_restart_and_continues_ingesting() -> None:
    times = iter([1000.0, 1001.0])
    source = Dump1090JsonSource(
        reader=SequenceReader(
            [_fixture("restart-before.json"), _fixture("restart-after.json")]
        ),
        store=TrackStore(stale_after_s=60.0, clock=lambda: 1001.0),
        clock=lambda: next(times),
    )

    source.poll_once()
    source.poll_once()

    assert source.health.decoder_restarts == 1
    assert source.health.snapshots_processed == 2
    assert source.store.snapshot()[0].callsign == "AFTER"


def test_polling_thread_reports_running_state_and_stops_cleanly() -> None:
    reader_called = threading.Event()

    def reader() -> bytes:
        reader_called.set()
        return b'{"messages": 1, "aircraft": []}'

    source = Dump1090JsonSource(
        reader=reader,
        store=TrackStore(),
        poll_interval_s=0.01,
    )

    source.start()
    try:
        assert reader_called.wait(timeout=1.0)
    finally:
        source.stop()
        source.join(timeout=1.0)

    assert not source.is_alive()
    assert source.health.running is False
    assert source.health.last_success_at is not None
    assert source.health.snapshots_processed >= 1


@pytest.mark.parametrize(
    "reader",
    [
        lambda: HttpSnapshotReader("file:///tmp/aircraft.json"),
        lambda: HttpSnapshotReader("http://receiver/aircraft.json", 0.0),
    ],
)
def test_http_reader_rejects_invalid_configuration(
    reader: object,
) -> None:
    with pytest.raises(ValueError):
        reader()


@pytest.mark.parametrize("poll_interval_s", [0.0, -1.0, float("inf")])
def test_source_rejects_invalid_poll_interval(poll_interval_s: float) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        Dump1090JsonSource(
            reader=lambda: b'{"aircraft": []}',
            store=TrackStore(),
            poll_interval_s=poll_interval_s,
        )
