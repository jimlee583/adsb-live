"""Hardware-free tests for the session recording / replay path."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from adsb_live import (
    AircraftTrack,
    RecordedSnapshot,
    ReplayHealth,
    ReplaySource,
    SessionFileError,
    SessionMetadata,
    SessionRecorder,
    TrackStore,
    load_snapshots,
    read_metadata,
)


# ---------------------------------------------------------------------------
# SessionRecorder
# ---------------------------------------------------------------------------


def _make_recorder(
    path: Path, *, clock_values: list[float] | None = None
) -> SessionRecorder:
    """Build a recorder with a controllable monotonic clock."""

    if clock_values is None:
        clock_values = [0.0]
    counter = {"i": 0}

    def clock() -> float:
        i = counter["i"]
        value = clock_values[min(i, len(clock_values) - 1)]
        counter["i"] = i + 1
        return value

    return SessionRecorder(
        path,
        receiver_lat=40.015,
        receiver_lon=-105.2705,
        stale_after_s=60.0,
        poll_interval_s=1.0,
        app_version="test",
        clock=clock,
        wall_clock=lambda: 1_700_000_000.0,
    )


def test_recorder_writes_metadata_and_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    # clock values: init (origin=100), then 100 (unused wall), then per snapshot
    recorder = _make_recorder(path, clock_values=[100.0, 101.5, 103.75])
    tracks = (
        AircraftTrack(
            icao="A12345",
            last_seen=101.0,
            first_seen=100.5,
            callsign="UAL1",
            latitude=40.0,
            longitude=-105.0,
            altitude_ft=33000.0,
        ),
        AircraftTrack(icao="B0FFEE", last_seen=101.4),
    )
    recorder.write_snapshot(tracks)
    recorder.write_snapshot(())  # empty snapshot is legal and preserved
    recorder.close()

    meta = read_metadata(path)
    assert meta.schema_version == 1
    assert meta.receiver_lat == pytest.approx(40.015)
    assert meta.receiver_lon == pytest.approx(-105.2705)
    assert meta.stale_after_s == pytest.approx(60.0)
    assert meta.poll_interval_s == pytest.approx(1.0)
    assert meta.origin_monotonic == pytest.approx(100.0)
    assert meta.app_version == "test"

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT elapsed_s, aircraft_json FROM snapshot ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[0][0] == pytest.approx(1.5)
    parsed = json.loads(rows[0][1])
    assert [entry["icao"] for entry in parsed] == ["A12345", "B0FFEE"]
    # Timestamps are stored as monotonic-offsets relative to origin.
    assert parsed[0]["last_seen"] == pytest.approx(1.0)
    assert parsed[0]["first_seen"] == pytest.approx(0.5)
    assert rows[1][0] == pytest.approx(3.75)
    assert json.loads(rows[1][1]) == []


def test_recorder_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    path.write_bytes(b"pre-existing")
    with pytest.raises(FileExistsError, match="already exists"):
        SessionRecorder(
            path,
            receiver_lat=None,
            receiver_lon=None,
            stale_after_s=60.0,
            poll_interval_s=1.0,
            app_version="test",
        )


def test_recorder_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    recorder = _make_recorder(path)
    recorder.close()
    recorder.close()  # no exception
    # Writing after close is a no-op, not a crash.
    recorder.write_snapshot(())
    assert recorder.snapshots_written == 0


# ---------------------------------------------------------------------------
# read_metadata / load_snapshots
# ---------------------------------------------------------------------------


def test_load_snapshots_rebases_timestamps_onto_provided_origin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.db"
    # origin=100.0, snapshots at t=101 (elapsed=1) and t=103 (elapsed=3)
    recorder = _make_recorder(path, clock_values=[100.0, 101.0, 103.0])
    recorder.write_snapshot((
        AircraftTrack(icao="A12345", last_seen=100.9, first_seen=100.9),
    ))
    recorder.write_snapshot((
        AircraftTrack(icao="A12345", last_seen=102.9, first_seen=100.9),
    ))
    recorder.close()

    replay_origin = 5_000.0
    meta, snaps = load_snapshots(path, origin=replay_origin)

    assert meta.origin_monotonic == pytest.approx(100.0)
    assert [snap.elapsed_s for snap in snaps] == pytest.approx([1.0, 3.0])
    # Timestamps rebased: original offset 0.9 -> replay_origin + 0.9.
    assert snaps[0].tracks[0].last_seen == pytest.approx(replay_origin + 0.9)
    assert snaps[1].tracks[0].last_seen == pytest.approx(replay_origin + 2.9)
    # first_seen preserved and rebased too.
    assert snaps[0].tracks[0].first_seen == pytest.approx(replay_origin + 0.9)
    assert snaps[1].tracks[0].first_seen == pytest.approx(replay_origin + 0.9)


def test_load_snapshots_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SessionFileError, match="not found"):
        load_snapshots(tmp_path / "does-not-exist.db")


def test_load_snapshots_rejects_corrupt_aircraft_json(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    recorder = _make_recorder(path, clock_values=[10.0, 11.0])
    recorder.write_snapshot(())
    recorder.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE snapshot SET aircraft_json = ? WHERE id = 1",
            ("{not-json",),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SessionFileError, match="corrupt aircraft_json"):
        load_snapshots(path)


def test_read_metadata_rejects_non_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "not-a-db.db"
    path.write_text("this is not sqlite")
    with pytest.raises(SessionFileError):
        read_metadata(path)


def test_read_metadata_rejects_unsupported_schema(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    recorder = _make_recorder(path)
    recorder.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE session SET schema_version = 99")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SessionFileError, match="unsupported session schema"):
        read_metadata(path)


# ---------------------------------------------------------------------------
# ReplaySource
# ---------------------------------------------------------------------------


class _ManualClock:
    """A monotonic-style clock that only moves when explicitly advanced."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, delta: float) -> None:
        with self._lock:
            self._t += delta


def _record_fixture(
    path: Path,
    *,
    origin: float = 100.0,
    snapshots: list[tuple[float, tuple[AircraftTrack, ...]]],
) -> None:
    """Write a session with the given snapshots at explicit offsets."""

    times = [origin]
    times.extend(origin + off for off, _tracks in snapshots)
    recorder = _make_recorder(path, clock_values=times)
    for _offset, tracks in snapshots:
        recorder.write_snapshot(tracks)
    recorder.close()


def test_replay_source_advances_store_in_real_time(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    _record_fixture(
        path,
        origin=100.0,
        snapshots=[
            (
                0.5,
                (
                    AircraftTrack(
                        icao="A12345",
                        last_seen=100.4,
                        latitude=40.0,
                        longitude=-105.0,
                        altitude_ft=30000.0,
                        ground_speed_kt=420.0,
                        vertical_rate_fpm=64.0,
                    ),
                ),
            ),
            (
                1.5,
                (
                    AircraftTrack(
                        icao="A12345",
                        last_seen=101.4,
                        latitude=40.01,
                        longitude=-105.01,
                        altitude_ft=30100.0,
                        ground_speed_kt=420.0,
                        vertical_rate_fpm=64.0,
                    ),
                ),
            ),
        ],
    )

    clock = _ManualClock(start=5_000.0)
    sleep_events: list[float] = []

    def instant_sleep(delta: float) -> bool:
        sleep_events.append(delta)
        # Instead of sleeping, advance the fake clock by the requested delta.
        clock.advance(delta)
        return False

    replay = ReplaySource(
        path,
        clock=clock.now,
        sleep=instant_sleep,
        freeze_on_end=False,
    )
    assert replay.snapshots_total == 2
    assert replay.duration_s == pytest.approx(1.5)
    assert replay.process.receiver_lat == pytest.approx(40.015)

    replay.start()
    replay.join(timeout=2.0)
    assert not replay.is_alive()

    snap = replay.store.snapshot(now=clock.now())
    assert [t.icao for t in snap] == ["A12345"]
    assert snap[0].latitude == pytest.approx(40.01)
    assert snap[0].altitude_ft == pytest.approx(30100.0)
    # last_seen was rebased onto the replay clock.
    assert snap[0].last_seen == pytest.approx(5_000.0 + 1.4)

    telemetry = replay.store.telemetry("A12345")
    assert len(telemetry) == 2
    assert telemetry[0].altitude_ft == pytest.approx(30000.0)
    assert telemetry[1].altitude_ft == pytest.approx(30100.0)
    positions = replay.store.history("A12345")
    assert len(positions) == 2

    assert replay.health.done is True
    assert replay.health.snapshots_processed == 2


def test_replay_source_freezes_after_final_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    _record_fixture(
        path,
        origin=200.0,
        snapshots=[
            (
                0.1,
                (
                    AircraftTrack(
                        icao="B0BCAB",
                        last_seen=200.0,
                        latitude=41.0,
                        longitude=-106.0,
                        altitude_ft=25000.0,
                    ),
                ),
            ),
        ],
    )

    clock = _ManualClock(start=10.0)
    barrier = threading.Event()

    def sleep(delta: float) -> bool:
        clock.advance(delta)
        # Signal once the replay thread has finished the initial snapshot and
        # entered the freeze-refresh loop.
        if replay.health.done:
            barrier.set()
        return False

    replay = ReplaySource(
        path,
        clock=clock.now,
        sleep=sleep,
        freeze_on_end=True,
        end_refresh_interval_s=0.01,
    )
    replay.start()
    try:
        assert barrier.wait(timeout=2.0)
        # Advance well past the store's stale window and let the replay
        # thread iterate the freeze-refresh loop a few times.
        for _ in range(200):
            if replay.store.snapshot(now=clock.now()):
                if clock.now() > 500.0:
                    break
            clock.advance(30.0)
            time.sleep(0.005)
        snap = replay.store.snapshot(now=clock.now())
        assert [t.icao for t in snap] == ["B0BCAB"]
    finally:
        replay.stop(timeout=2.0)


def test_replay_source_uses_receiver_override(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    _record_fixture(
        path,
        origin=100.0,
        snapshots=[(0.1, ())],
    )

    replay = ReplaySource(
        path,
        receiver_lat_override=51.0,
        receiver_lon_override=-2.5,
    )
    try:
        assert replay.process.receiver_lat == pytest.approx(51.0)
        assert replay.process.receiver_lon == pytest.approx(-2.5)
    finally:
        replay.stop()


def test_replay_source_hides_map_when_receiver_partial(tmp_path: Path) -> None:
    """Recording without receiver + only one override -> no receiver."""

    path = tmp_path / "session.db"
    # Recording had no receiver; the recorder's clock start is unrelated.
    _make_recorder_no_receiver(path)
    replay = ReplaySource(path, receiver_lat_override=51.0)
    try:
        assert replay.process.receiver_lat is None
        assert replay.process.receiver_lon is None
    finally:
        replay.stop()


def _make_recorder_no_receiver(path: Path) -> None:
    recorder = SessionRecorder(
        path,
        receiver_lat=None,
        receiver_lon=None,
        stale_after_s=60.0,
        poll_interval_s=1.0,
        app_version="test",
        clock=lambda: 100.0,
        wall_clock=lambda: 1_700_000_000.0,
    )
    recorder.close()


def test_replay_source_health_reports_totals(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    _record_fixture(
        path,
        origin=0.0,
        snapshots=[(0.5, ()), (1.0, ()), (1.5, ())],
    )
    replay = ReplaySource(path)
    try:
        health = replay.health
        assert isinstance(health, ReplayHealth)
        assert health.snapshots_total == 3
        assert health.duration_s == pytest.approx(1.5)
        assert health.done is False
    finally:
        replay.stop()


def test_replay_source_poller_health_shape_matches_dump1090(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.db"
    _record_fixture(path, origin=0.0, snapshots=[(0.1, ())])
    replay = ReplaySource(path)
    try:
        health = replay.poller.health
        # Duck-typed for WaterfallWindow: same fields as the live path uses.
        assert hasattr(health, "snapshots_processed")
        assert hasattr(health, "last_message_count")
        assert hasattr(health, "consecutive_failures")
        assert hasattr(health, "last_error")
    finally:
        replay.stop()


# ---------------------------------------------------------------------------
# Dump1090JsonSource on_poll hook (record path)
# ---------------------------------------------------------------------------


def test_dump1090_source_on_poll_fires_after_successful_poll() -> None:
    from adsb_live import Dump1090JsonSource

    calls: list[tuple[AircraftTrack, ...]] = []

    def sink(store: TrackStore) -> None:
        calls.append(store.snapshot(now=1000.0))

    payload = json.dumps({
        "messages": 1,
        "aircraft": [{"hex": "abc123", "seen": 0.1, "lat": 40.0, "lon": -105.0}],
    }).encode()

    source = Dump1090JsonSource(
        reader=lambda: payload,
        store=TrackStore(stale_after_s=60.0, clock=lambda: 1000.0),
        clock=lambda: 1000.0,
        on_poll=sink,
    )
    source.poll_once()

    assert len(calls) == 1
    assert [t.icao for t in calls[0]] == ["ABC123"]


def test_dump1090_source_on_poll_is_not_called_on_parse_failure() -> None:
    from adsb_live import Dump1090JsonSource, Dump1090FormatError

    calls: list[int] = []

    source = Dump1090JsonSource(
        reader=lambda: b"{",
        store=TrackStore(stale_after_s=60.0, clock=lambda: 1000.0),
        clock=lambda: 1000.0,
        on_poll=lambda store: calls.append(1),
    )
    with pytest.raises(Dump1090FormatError):
        source.poll_once()
    assert calls == []


def test_dump1090_source_on_poll_hook_error_does_not_break_polling() -> None:
    from adsb_live import Dump1090JsonSource

    payload = json.dumps({"messages": 1, "aircraft": []}).encode()
    calls: list[int] = []

    def failing(store: TrackStore) -> None:
        calls.append(1)
        raise RuntimeError("boom")

    source = Dump1090JsonSource(
        reader=lambda: payload,
        store=TrackStore(stale_after_s=60.0, clock=lambda: 100.0),
        clock=lambda: 100.0,
        on_poll=failing,
    )
    source.poll_once()
    source.poll_once()
    assert calls == [1, 1]
    # Health still reflects a successful poll: hook errors are contained.
    assert source.health.snapshots_processed == 2
    assert source.health.last_error is None


# ---------------------------------------------------------------------------
# End-to-end round-trip
# ---------------------------------------------------------------------------


def test_end_to_end_record_then_replay(tmp_path: Path) -> None:
    """Record a synthetic snapshot stream, replay it, expect the same tracks."""

    path = tmp_path / "session.db"
    _record_fixture(
        path,
        origin=1_000.0,
        snapshots=[
            (
                0.25,
                (
                    AircraftTrack(
                        icao="A00001",
                        last_seen=1_000.1,
                        callsign="ONE",
                        latitude=40.0,
                        longitude=-105.0,
                        altitude_ft=10000.0,
                        ground_speed_kt=300.0,
                        vertical_rate_fpm=64.0,
                    ),
                    AircraftTrack(
                        icao="B00002",
                        last_seen=1_000.1,
                        callsign="TWO",
                        altitude_ft=12000.0,
                    ),
                ),
            ),
            (
                0.75,
                (
                    AircraftTrack(
                        icao="A00001",
                        last_seen=1_000.6,
                        callsign="ONE",
                        latitude=40.01,
                        longitude=-105.01,
                        altitude_ft=10100.0,
                        ground_speed_kt=305.0,
                        vertical_rate_fpm=64.0,
                    ),
                ),
            ),
        ],
    )

    clock = _ManualClock(start=0.0)

    def instant_sleep(delta: float) -> bool:
        clock.advance(delta)
        return False

    replay = ReplaySource(
        path,
        clock=clock.now,
        sleep=instant_sleep,
        freeze_on_end=False,
    )
    replay.start()
    replay.join(timeout=2.0)

    snap = replay.store.snapshot(now=clock.now())
    icaos = {t.icao for t in snap}
    assert icaos == {"A00001", "B00002"}
    a1 = {t.icao: t for t in snap}["A00001"]
    assert a1.callsign == "ONE"
    assert a1.altitude_ft == pytest.approx(10100.0)

    telemetry = replay.store.telemetry("A00001")
    assert [ts.altitude_ft for ts in telemetry] == pytest.approx(
        [10000.0, 10100.0]
    )

    history = replay.store.history("A00001")
    assert [(p.latitude, p.longitude) for p in history] == [
        (40.0, -105.0),
        (40.01, -105.01),
    ]
