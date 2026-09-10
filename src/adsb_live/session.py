"""SQLite-backed session recording and replay for aircraft tracks.

A session file captures the observable state of an :class:`~adsb_live.tracks.TrackStore`
once per successful dump1090 poll. Later, :class:`ReplaySource` streams those
snapshots back through a fresh ``TrackStore`` in real time, so the existing
aircraft table, map, and detail widgets work end-to-end without a dongle or a
``dump1090`` binary.

The file format is plain SQLite (stdlib only, no new dependencies):

``session`` table (one row):

* ``schema_version`` -- integer, currently ``1``.
* ``started_at_unix`` -- ``time.time()`` at recorder start (wall-clock, for humans).
* ``origin_monotonic`` -- ``time.monotonic()`` at recorder start; stored so
  timestamp offsets on disk are anchored explicitly, not implicitly.
* ``receiver_lat`` / ``receiver_lon`` -- optional floats (``NULL`` if unknown).
* ``stale_after_s`` -- the ``TrackStore.stale_after_s`` in effect during recording.
* ``poll_interval_s`` -- the ``Dump1090JsonSource.poll_interval_s`` in effect
  during recording.
* ``app_version`` -- ``adsb_live.__version__`` at recording time.

``snapshot`` table (one row per poll):

* ``elapsed_s`` -- ``time.monotonic() - origin_monotonic`` when the snapshot
  was recorded; monotonically non-decreasing.
* ``aircraft_json`` -- JSON array of :class:`~adsb_live.tracks.AircraftTrack`
  records. ``last_seen`` and ``first_seen`` are stored as **offsets from
  ``origin_monotonic``** so they can be remapped onto a fresh monotonic clock
  during replay.

Recording is invoked from a background thread; replay drives its own
background thread. Both open the connection with ``check_same_thread=False``
and serialize writes with a lock so ``stop()`` on the UI thread is safe.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .dump1090 import Dump1090SourceHealth
from .sdr import DropOldestByteQueue
from .tracks import AircraftTrack, TrackStore

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE session (
    schema_version    INTEGER NOT NULL,
    started_at_unix   REAL NOT NULL,
    origin_monotonic  REAL NOT NULL,
    receiver_lat      REAL,
    receiver_lon      REAL,
    stale_after_s     REAL NOT NULL,
    poll_interval_s   REAL NOT NULL,
    app_version       TEXT NOT NULL
);

CREATE TABLE snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    elapsed_s      REAL NOT NULL,
    aircraft_json  TEXT NOT NULL
);

CREATE INDEX idx_snapshot_elapsed ON snapshot (elapsed_s);
"""


# Fields that are always present on :class:`AircraftTrack` and require special
# handling. Everything else is a plain optional scalar that JSON can round-trip.
_TIMESTAMP_FIELDS: tuple[str, ...] = ("last_seen", "first_seen")


class SessionFileError(RuntimeError):
    """Raised when a session file is missing, malformed, or the wrong version."""


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    """Header row from a session file."""

    schema_version: int
    started_at_unix: float
    origin_monotonic: float
    receiver_lat: float | None
    receiver_lon: float | None
    stale_after_s: float
    poll_interval_s: float
    app_version: str


def _track_to_dict(track: AircraftTrack, *, origin: float) -> dict[str, Any]:
    """Serialise ``track`` to a JSON-safe dict with monotonic-time offsets."""

    row = asdict(track)
    for name in _TIMESTAMP_FIELDS:
        value = row.get(name)
        if value is None:
            continue
        row[name] = float(value) - origin
    return row


def _track_from_dict(row: dict[str, Any], *, origin: float) -> AircraftTrack:
    """Rebuild an :class:`AircraftTrack` and rebase its offsets onto ``origin``."""

    kwargs = dict(row)
    for name in _TIMESTAMP_FIELDS:
        value = kwargs.get(name)
        if value is None:
            continue
        kwargs[name] = origin + float(value)
    return AircraftTrack(**kwargs)


class SessionRecorder:
    """Append ``TrackStore.snapshot()`` rows to a SQLite session file.

    Thread-safe. The connection is opened with ``check_same_thread=False``
    so ``write_snapshot`` can be called from the dump1090 poller thread and
    ``close`` can be called from the UI thread on shutdown.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        receiver_lat: float | None,
        receiver_lon: float | None,
        stale_after_s: float,
        poll_interval_s: float,
        app_version: str,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        target = Path(path)
        if target.exists():
            raise FileExistsError(
                f"session file already exists: {target}. "
                "Refusing to overwrite; delete it or choose a different path."
            )
        target.parent.mkdir(parents=True, exist_ok=True)

        self.path = target
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False
        self._origin = float(clock())
        self._started_at_unix = float(wall_clock())
        self._snapshots_written = 0

        self._conn = sqlite3.connect(str(target), check_same_thread=False)
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(
            """
            INSERT INTO session (
                schema_version, started_at_unix, origin_monotonic,
                receiver_lat, receiver_lon,
                stale_after_s, poll_interval_s, app_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SCHEMA_VERSION,
                self._started_at_unix,
                self._origin,
                None if receiver_lat is None else float(receiver_lat),
                None if receiver_lon is None else float(receiver_lon),
                float(stale_after_s),
                float(poll_interval_s),
                str(app_version),
            ),
        )
        self._conn.commit()

    @property
    def snapshots_written(self) -> int:
        with self._lock:
            return self._snapshots_written

    def write_snapshot(self, tracks: Sequence[AircraftTrack]) -> None:
        """Append one snapshot. Empty snapshots are still recorded."""

        payload = [_track_to_dict(t, origin=self._origin) for t in tracks]
        elapsed = float(self._clock()) - self._origin
        with self._lock:
            if self._closed:
                return
            self._conn.execute(
                "INSERT INTO snapshot (elapsed_s, aircraft_json) VALUES (?, ?)",
                (elapsed, json.dumps(payload, separators=(",", ":"))),
            )
            self._conn.commit()
            self._snapshots_written += 1

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _read_metadata(conn: sqlite3.Connection) -> SessionMetadata:
    try:
        row = conn.execute(
            """
            SELECT schema_version, started_at_unix, origin_monotonic,
                   receiver_lat, receiver_lon,
                   stale_after_s, poll_interval_s, app_version
            FROM session
            """
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SessionFileError(f"not a valid session file: {exc}") from exc
    if row is None:
        raise SessionFileError("session file has no metadata row")
    (
        schema_version,
        started_at_unix,
        origin_monotonic,
        receiver_lat,
        receiver_lon,
        stale_after_s,
        poll_interval_s,
        app_version,
    ) = row
    if int(schema_version) != SCHEMA_VERSION:
        raise SessionFileError(
            f"unsupported session schema {schema_version}; "
            f"this build reads version {SCHEMA_VERSION}"
        )
    return SessionMetadata(
        schema_version=int(schema_version),
        started_at_unix=float(started_at_unix),
        origin_monotonic=float(origin_monotonic),
        receiver_lat=None if receiver_lat is None else float(receiver_lat),
        receiver_lon=None if receiver_lon is None else float(receiver_lon),
        stale_after_s=float(stale_after_s),
        poll_interval_s=float(poll_interval_s),
        app_version=str(app_version),
    )


def read_metadata(path: str | Path) -> SessionMetadata:
    """Open ``path`` and return only the session-header row."""

    target = Path(path)
    if not target.exists():
        raise SessionFileError(f"session file not found: {target}")
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        return _read_metadata(conn)
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class RecordedSnapshot:
    """One row from ``snapshot`` with tracks rebased onto ``origin``."""

    elapsed_s: float
    tracks: tuple[AircraftTrack, ...]


def _iter_snapshots(
    conn: sqlite3.Connection, *, origin: float, recorder_origin: float
) -> Iterator[RecordedSnapshot]:
    del recorder_origin  # kept for symmetry; offsets are already recorder-relative
    cursor = conn.execute(
        "SELECT elapsed_s, aircraft_json FROM snapshot ORDER BY id ASC"
    )
    try:
        for elapsed_s, aircraft_json in cursor:
            try:
                raw = json.loads(aircraft_json)
            except json.JSONDecodeError as exc:
                raise SessionFileError(
                    f"corrupt aircraft_json at elapsed_s={elapsed_s}: {exc}"
                ) from exc
            if not isinstance(raw, list):
                raise SessionFileError(
                    f"aircraft_json at elapsed_s={elapsed_s} is not a list"
                )
            tracks: list[AircraftTrack] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    raise SessionFileError(
                        f"aircraft_json entry at elapsed_s={elapsed_s} "
                        "is not an object"
                    )
                tracks.append(_track_from_dict(entry, origin=origin))
            yield RecordedSnapshot(
                elapsed_s=float(elapsed_s), tracks=tuple(tracks)
            )
    finally:
        cursor.close()


def load_snapshots(
    path: str | Path,
    *,
    origin: float | None = None,
) -> tuple[SessionMetadata, tuple[RecordedSnapshot, ...]]:
    """Load metadata and every snapshot, rebasing timestamps onto ``origin``.

    ``origin`` defaults to ``time.monotonic()`` at load time.
    """

    target = Path(path)
    if not target.exists():
        raise SessionFileError(f"session file not found: {target}")
    replay_origin = float(time.monotonic()) if origin is None else float(origin)
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        metadata = _read_metadata(conn)
        snapshots = tuple(
            _iter_snapshots(
                conn,
                origin=replay_origin,
                recorder_origin=metadata.origin_monotonic,
            )
        )
    finally:
        conn.close()
    return metadata, snapshots


@dataclass(frozen=True, slots=True)
class ReplayHealth:
    """Status snapshot for :class:`ReplaySource`, shaped like
    :class:`~adsb_live.dump1090.Dump1090SourceHealth` so the existing status
    bar formatter keeps working."""

    running: bool = False
    snapshots_processed: int = 0
    snapshots_total: int = 0
    elapsed_s: float = 0.0
    duration_s: float = 0.0
    done: bool = False
    last_error: str | None = None
    consecutive_failures: int = 0


class _ReplayProcessView:
    """Duck-type shim for :attr:`LiveDecoder.process` used by ``WaterfallWindow``."""

    def __init__(
        self,
        *,
        receiver_lat: float | None,
        receiver_lon: float | None,
    ) -> None:
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon

    @property
    def health(self) -> "_ReplayProcessHealth":
        return _ReplayProcessHealth()


@dataclass(frozen=True, slots=True)
class _ReplayProcessHealth:
    """Static "always running" health for the replay path."""

    running: bool = True
    pid: int | None = None
    started_at: float | None = None
    exits: int = 0
    restarts: int = 0
    last_exit_code: int | None = None
    last_stderr_line: str | None = None
    bytes_written: int = 0
    blocks_dropped_on_write: int = 0


class _ReplayPollerView:
    """Adapter that maps :class:`ReplayHealth` onto the fields the status bar reads."""

    def __init__(self, owner: "ReplaySource") -> None:
        self._owner = owner

    @property
    def health(self) -> Dump1090SourceHealth:
        rh = self._owner.health
        return Dump1090SourceHealth(
            running=rh.running,
            last_success_at=None,
            last_error=rh.last_error,
            consecutive_failures=rh.consecutive_failures,
            snapshots_processed=rh.snapshots_processed,
            rejected_records=0,
            decoder_restarts=0,
            last_message_count=None,
        )


class ReplaySource(threading.Thread):
    """Stream recorded snapshots into a fresh :class:`TrackStore` in real time.

    The public shape mirrors :class:`~adsb_live.decoder.LiveDecoder` closely
    enough to be passed to :class:`~adsb_live.ui.WaterfallWindow` as its
    ``decoder`` argument:

    * ``store`` -- the :class:`TrackStore` the UI reads from.
    * ``process`` -- exposes ``receiver_lat``, ``receiver_lon``, ``health``.
    * ``poller`` -- exposes a ``Dump1090SourceHealth``-shaped ``health``.
    * ``iq_sink`` -- an unused :class:`DropOldestByteQueue` so status-bar
      formatting that reads ``iq_sink.dropped`` does not need to branch.
    * ``kind`` -- ``"replay"``.
    * ``start()`` / ``stop()`` -- lifecycle matching ``LiveDecoder``.
    """

    kind = "replay"

    def __init__(
        self,
        path: str | Path,
        *,
        receiver_lat_override: float | None = None,
        receiver_lon_override: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], bool] | None = None,
        freeze_on_end: bool = True,
        end_refresh_interval_s: float = 1.0,
    ) -> None:
        super().__init__(name="ReplaySource", daemon=True)
        self.path = Path(path)
        self._clock = clock
        self._freeze_on_end = bool(freeze_on_end)
        self._end_refresh_interval_s = float(end_refresh_interval_s)

        self._origin = float(clock())
        self._metadata, self._snapshots = load_snapshots(
            self.path, origin=self._origin
        )
        lat = (
            receiver_lat_override
            if receiver_lat_override is not None
            else self._metadata.receiver_lat
        )
        lon = (
            receiver_lon_override
            if receiver_lon_override is not None
            else self._metadata.receiver_lon
        )
        # Preserve pairing: if only one coordinate is available drop the map.
        if lat is None or lon is None:
            lat = None
            lon = None

        self.store = TrackStore(
            stale_after_s=self._metadata.stale_after_s,
            clock=clock,
        )
        self.iq_sink = DropOldestByteQueue(maxsize=1)
        self.process = _ReplayProcessView(receiver_lat=lat, receiver_lon=lon)
        self.poller = _ReplayPollerView(self)

        self._stop_event = threading.Event()
        # ``sleep(timeout) -> should_stop``. Default is the stop event's own
        # ``wait`` so ``stop()`` interrupts real waits promptly. Tests inject
        # a variant that advances a fake clock and always returns ``False``.
        self._sleep: Callable[[float], bool] = sleep or self._stop_event.wait
        self._start_called = False
        self._stopped = False
        self._health_lock = threading.Lock()
        duration = (
            self._snapshots[-1].elapsed_s if self._snapshots else 0.0
        )
        self._health = ReplayHealth(
            snapshots_total=len(self._snapshots),
            duration_s=duration,
        )

    # ------------------------------------------------------------------
    # Introspection

    @property
    def metadata(self) -> SessionMetadata:
        return self._metadata

    @property
    def snapshots_total(self) -> int:
        return len(self._snapshots)

    @property
    def duration_s(self) -> float:
        return self._snapshots[-1].elapsed_s if self._snapshots else 0.0

    @property
    def health(self) -> ReplayHealth:
        with self._health_lock:
            return self._health

    # ------------------------------------------------------------------
    # Lifecycle

    def start(self) -> None:  # noqa: D401 -- matches Thread
        if self._start_called:
            return
        self._start_called = True
        with self._health_lock:
            self._health = replace(self._health, running=True)
        super().start()

    def stop(self, timeout: float = 3.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Thread body

    def run(self) -> None:  # noqa: D401
        try:
            self._play_snapshots()
        finally:
            with self._health_lock:
                self._health = replace(self._health, running=False)

    def _play_snapshots(self) -> None:
        for index, snap in enumerate(self._snapshots):
            if self._stop_event.is_set():
                return
            deadline = self._origin + snap.elapsed_s
            if self._wait_until(deadline):
                return
            self._apply_snapshot(snap, index + 1)

        if not self._freeze_on_end or not self._snapshots:
            with self._health_lock:
                self._health = replace(self._health, done=True)
            return

        # Replay is done. Re-apply the final snapshot at a slow cadence with
        # freshened timestamps so ``stale_after_s`` does not clear the store
        # while the user is still looking at the window.
        final = self._snapshots[-1]
        with self._health_lock:
            self._health = replace(self._health, done=True)
        while not self._stop_event.is_set():
            refreshed = _refresh_snapshot(final, now=self._clock())
            self._apply_snapshot(refreshed, self._health.snapshots_processed)
            if self._sleep(self._end_refresh_interval_s):
                return

    def _wait_until(self, deadline: float) -> bool:
        """Sleep until ``deadline`` on the injected clock.

        Returns ``True`` when a stop was requested during the wait.
        """

        while not self._stop_event.is_set():
            remaining = deadline - self._clock()
            if remaining <= 0.0:
                return False
            # Cap each sleep so ``stop()`` responds quickly on long gaps.
            chunk = min(remaining, 0.25)
            if self._sleep(chunk):
                return True
        return True

    def _apply_snapshot(
        self, snap: RecordedSnapshot, processed_count: int
    ) -> None:
        try:
            for track in snap.tracks:
                self.store.upsert(track)
            # Expire relative to the newest ``last_seen`` in this snapshot so
            # tracks that stopped transmitting in the recording still age out
            # correctly during replay.
            expiry_now = _snapshot_max_last_seen(snap, fallback=self._clock())
            self.store.expire_stale(now=expiry_now)
        except Exception as exc:  # noqa: BLE001
            with self._health_lock:
                self._health = replace(
                    self._health,
                    last_error=f"{type(exc).__name__}: {exc}",
                    consecutive_failures=self._health.consecutive_failures + 1,
                )
            return
        with self._health_lock:
            self._health = replace(
                self._health,
                snapshots_processed=processed_count,
                elapsed_s=snap.elapsed_s,
                last_error=None,
                consecutive_failures=0,
            )


def _snapshot_max_last_seen(
    snap: RecordedSnapshot, *, fallback: float
) -> float:
    if not snap.tracks:
        return fallback
    return max(track.last_seen for track in snap.tracks)


def _refresh_snapshot(
    snap: RecordedSnapshot, *, now: float
) -> RecordedSnapshot:
    """Return a copy of ``snap`` with every ``last_seen`` bumped to ``now``.

    ``first_seen`` is preserved so age-since-first-seen still means something
    once replay reaches the freeze phase.
    """

    if not snap.tracks:
        return RecordedSnapshot(elapsed_s=snap.elapsed_s, tracks=())
    refreshed = tuple(
        _bump_last_seen(track, now=now) for track in snap.tracks
    )
    return RecordedSnapshot(elapsed_s=snap.elapsed_s, tracks=refreshed)


def _bump_last_seen(track: AircraftTrack, *, now: float) -> AircraftTrack:
    first_seen = track.first_seen if track.first_seen is not None else now
    if first_seen > now:
        first_seen = now
    kwargs: dict[str, Any] = {
        name: getattr(track, name)
        for name in (
            "icao",
            "callsign",
            "latitude",
            "longitude",
            "altitude_ft",
            "on_ground",
            "ground_speed_kt",
            "track_deg",
            "vertical_rate_fpm",
            "squawk",
            "category",
            "signal_db",
        )
    }
    return AircraftTrack(last_seen=now, first_seen=first_seen, **kwargs)


__all__ = [
    "RecordedSnapshot",
    "ReplayHealth",
    "ReplaySource",
    "SCHEMA_VERSION",
    "SessionFileError",
    "SessionMetadata",
    "SessionRecorder",
    "load_snapshots",
    "read_metadata",
]
