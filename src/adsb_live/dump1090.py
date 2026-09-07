"""Hardware-independent ingestion of dump1090/readsb ``aircraft.json``."""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen

from .tracks import AircraftTrack, TrackStore


class Dump1090FormatError(ValueError):
    """Raised when a dump1090 snapshot has an invalid top-level structure."""


@dataclass(frozen=True, slots=True)
class Dump1090Snapshot:
    """Normalized contents and metadata from one ``aircraft.json`` snapshot."""

    tracks: tuple[AircraftTrack, ...]
    observed_at: float
    generated_at: float | None
    message_count: int | None
    rejected_records: int


@dataclass(frozen=True, slots=True)
class Dump1090SourceHealth:
    """Thread-safe status snapshot for a :class:`Dump1090JsonSource`."""

    running: bool = False
    last_success_at: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    snapshots_processed: int = 0
    rejected_records: int = 0
    decoder_restarts: int = 0


class SnapshotReader(Protocol):
    """Callable that returns one raw dump1090 JSON snapshot."""

    def __call__(self) -> bytes | str | Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class FileSnapshotReader:
    """Read an ``aircraft.json`` snapshot from a local filesystem path."""

    path: Path

    def __init__(self, path: str | Path) -> None:
        object.__setattr__(self, "path", Path(path))

    def __call__(self) -> bytes:
        return self.path.read_bytes()


@dataclass(frozen=True, slots=True)
class HttpSnapshotReader:
    """Fetch an ``aircraft.json`` snapshot over HTTP."""

    url: str
    timeout_s: float = 2.0

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("url must use http or https")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0.0:
            raise ValueError("timeout_s must be positive and finite")

    def __call__(self) -> bytes:
        request = Request(self.url, headers={"User-Agent": "adsb-live/0.1"})
        with urlopen(request, timeout=self.timeout_s) as response:
            return response.read()


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _optional_number(
    record: Mapping[str, object], *names: str
) -> float | None:
    for name in names:
        value = record.get(name)
        if value is not None:
            return _number(value, name=name)
    return None


def _optional_string(record: Mapping[str, object], name: str) -> str | None:
    value = record.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _altitude(record: Mapping[str, object]) -> tuple[float | None, bool | None]:
    for name in ("alt_baro", "alt_geom"):
        value = record.get(name)
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() == "ground":
            return None, True
        return _number(value, name=name), False
    return None, None


def _parse_track(
    record: Mapping[str, object], *, observed_at: float
) -> AircraftTrack:
    raw_icao = record.get("hex")
    if not isinstance(raw_icao, str):
        raise ValueError("hex must be a string")
    if raw_icao.startswith("~"):
        raise ValueError("non-ICAO address")

    raw_seen = record.get("seen", 0.0)
    seen = _number(raw_seen, name="seen")
    if seen < 0.0:
        raise ValueError("seen cannot be negative")

    altitude_ft, on_ground = _altitude(record)
    return AircraftTrack(
        icao=raw_icao,
        last_seen=observed_at - seen,
        callsign=_optional_string(record, "flight"),
        latitude=_optional_number(record, "lat"),
        longitude=_optional_number(record, "lon"),
        altitude_ft=altitude_ft,
        on_ground=on_ground,
        ground_speed_kt=_optional_number(record, "gs"),
        track_deg=_optional_number(record, "track"),
        vertical_rate_fpm=_optional_number(record, "baro_rate", "geom_rate"),
        squawk=_optional_string(record, "squawk"),
        category=_optional_string(record, "category"),
        signal_db=_optional_number(record, "rssi"),
    )


def _decode_document(
    payload: bytes | str | Mapping[str, object],
) -> Mapping[str, object]:
    if isinstance(payload, Mapping):
        return payload
    try:
        decoded: Any = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise Dump1090FormatError(f"invalid JSON: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise Dump1090FormatError("snapshot root must be an object")
    return decoded


def parse_aircraft_json(
    payload: bytes | str | Mapping[str, object],
    *,
    observed_at: float,
) -> Dump1090Snapshot:
    """Parse one snapshot without performing filesystem or network I/O.

    Invalid aircraft records are counted and skipped so one bad transponder
    cannot discard the other valid tracks in a snapshot.
    """

    observed_at = _number(observed_at, name="observed_at")
    document = _decode_document(payload)
    aircraft = document.get("aircraft")
    if not isinstance(aircraft, list):
        raise Dump1090FormatError("aircraft must be an array")

    generated_at = None
    if document.get("now") is not None:
        generated_at = _number(document["now"], name="now")

    message_count = None
    if document.get("messages") is not None:
        raw_messages = document["messages"]
        if (
            isinstance(raw_messages, bool)
            or not isinstance(raw_messages, int)
            or raw_messages < 0
        ):
            raise Dump1090FormatError("messages must be a non-negative integer")
        message_count = raw_messages

    tracks: list[AircraftTrack] = []
    rejected_records = 0
    for record in aircraft:
        if not isinstance(record, Mapping):
            rejected_records += 1
            continue
        try:
            tracks.append(_parse_track(record, observed_at=observed_at))
        except (TypeError, ValueError):
            rejected_records += 1

    tracks.sort(key=lambda track: track.icao)
    return Dump1090Snapshot(
        tracks=tuple(tracks),
        observed_at=observed_at,
        generated_at=generated_at,
        message_count=message_count,
        rejected_records=rejected_records,
    )


class Dump1090JsonSource(threading.Thread):
    """Poll dump1090 snapshots and update a shared :class:`TrackStore`."""

    def __init__(
        self,
        *,
        reader: SnapshotReader,
        store: TrackStore,
        poll_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0.0:
            raise ValueError("poll_interval_s must be positive and finite")
        super().__init__(name="Dump1090JsonSource", daemon=True)
        self.reader = reader
        self.store = store
        self.poll_interval_s = float(poll_interval_s)
        self._clock = clock
        self._stop_event = threading.Event()
        self._health_lock = threading.Lock()
        self._health = Dump1090SourceHealth()
        self._last_message_count: int | None = None
        self._last_generated_at: float | None = None

    @property
    def health(self) -> Dump1090SourceHealth:
        with self._health_lock:
            return self._health

    def stop(self) -> None:
        self._stop_event.set()

    def poll_once(self) -> Dump1090Snapshot:
        """Read and ingest one snapshot, updating health before returning."""

        try:
            observed_at = self._clock()
            snapshot = parse_aircraft_json(
                self.reader(), observed_at=observed_at
            )
            for track in snapshot.tracks:
                self.store.upsert(track)
            self.store.expire_stale(now=snapshot.observed_at)
        except Exception as exc:
            with self._health_lock:
                self._health = replace(
                    self._health,
                    last_error=f"{type(exc).__name__}: {exc}",
                    consecutive_failures=self._health.consecutive_failures + 1,
                )
            raise

        restarted = (
            self._last_message_count is not None
            and snapshot.message_count is not None
            and snapshot.message_count < self._last_message_count
        ) or (
            self._last_generated_at is not None
            and snapshot.generated_at is not None
            and snapshot.generated_at < self._last_generated_at
        )
        self._last_message_count = snapshot.message_count
        self._last_generated_at = snapshot.generated_at

        with self._health_lock:
            self._health = replace(
                self._health,
                last_success_at=snapshot.observed_at,
                last_error=None,
                consecutive_failures=0,
                snapshots_processed=self._health.snapshots_processed + 1,
                rejected_records=(
                    self._health.rejected_records + snapshot.rejected_records
                ),
                decoder_restarts=(
                    self._health.decoder_restarts + int(restarted)
                ),
            )
        return snapshot

    def run(self) -> None:
        with self._health_lock:
            self._health = replace(self._health, running=True)
        try:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                except Exception:
                    pass
                self._stop_event.wait(self.poll_interval_s)
        finally:
            with self._health_lock:
                self._health = replace(self._health, running=False)
