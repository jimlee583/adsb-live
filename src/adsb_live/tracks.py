"""Aircraft track model and thread-safe in-memory storage."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, NamedTuple


#: Optional fields on :class:`AircraftTrack` that :meth:`TrackStore.upsert`
#: merges across snapshots. An incoming ``None`` on one of these fields
#: means "not observed in this snapshot", not "cleared" -- the previously
#: observed value is preserved until it ages past ``field_stale_after_s``.
_MERGEABLE_FIELDS: tuple[str, ...] = (
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


def _normalize_icao(value: str) -> str:
    icao = value.strip().upper()
    if len(icao) != 6 or any(char not in "0123456789ABCDEF" for char in icao):
        raise ValueError("icao must contain exactly six hexadecimal characters")
    return icao


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class AircraftTrack:
    """Latest known state for one aircraft.

    ``first_seen`` and ``last_seen`` use the same monotonic clock as the
    :class:`TrackStore`. Optional fields represent data that has not yet been
    observed.
    """

    icao: str
    last_seen: float
    first_seen: float | None = None
    callsign: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_ft: float | None = None
    on_ground: bool | None = None
    ground_speed_kt: float | None = None
    track_deg: float | None = None
    vertical_rate_fpm: float | None = None
    squawk: str | None = None
    category: str | None = None
    signal_db: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "icao", _normalize_icao(self.icao))
        _require_finite("last_seen", self.last_seen)

        first_seen = self.last_seen if self.first_seen is None else self.first_seen
        _require_finite("first_seen", first_seen)
        if first_seen > self.last_seen:
            raise ValueError("first_seen cannot be later than last_seen")
        object.__setattr__(self, "first_seen", first_seen)

        if self.callsign is not None:
            callsign = self.callsign.strip().upper()
            object.__setattr__(self, "callsign", callsign or None)

        if self.latitude is not None:
            _require_finite("latitude", self.latitude)
            if not -90.0 <= self.latitude <= 90.0:
                raise ValueError("latitude must be between -90 and 90 degrees")

        if self.longitude is not None:
            _require_finite("longitude", self.longitude)
            if not -180.0 <= self.longitude <= 180.0:
                raise ValueError("longitude must be between -180 and 180 degrees")

        for name in ("altitude_ft", "vertical_rate_fpm", "signal_db"):
            value = getattr(self, name)
            if value is not None:
                _require_finite(name, value)

        if self.on_ground is not None and not isinstance(self.on_ground, bool):
            raise TypeError("on_ground must be a bool or None")

        if self.ground_speed_kt is not None:
            _require_finite("ground_speed_kt", self.ground_speed_kt)
            if self.ground_speed_kt < 0.0:
                raise ValueError("ground_speed_kt cannot be negative")

        if self.track_deg is not None:
            _require_finite("track_deg", self.track_deg)
            if not 0.0 <= self.track_deg < 360.0:
                raise ValueError("track_deg must be at least 0 and less than 360")

        if self.squawk is not None:
            squawk = self.squawk.strip()
            if len(squawk) != 4 or any(char not in "01234567" for char in squawk):
                raise ValueError("squawk must contain exactly four octal digits")
            object.__setattr__(self, "squawk", squawk)

        if self.category is not None:
            category = self.category.strip().upper()
            object.__setattr__(self, "category", category or None)


def _assert_mergeable_fields_cover_track() -> None:
    """Fail import if a new optional field on ``AircraftTrack`` is added
    without also listing it in ``_MERGEABLE_FIELDS``.

    Without this guard, a new field would silently be reset to its
    default on every snapshot -- exactly the bug field-level merging
    exists to prevent.
    """

    excluded = {"icao", "last_seen", "first_seen"}
    optional = tuple(
        field.name for field in fields(AircraftTrack) if field.name not in excluded
    )
    if optional != _MERGEABLE_FIELDS:
        raise RuntimeError(
            "_MERGEABLE_FIELDS is out of sync with AircraftTrack; "
            f"expected {optional}, got {_MERGEABLE_FIELDS}"
        )


_assert_mergeable_fields_cover_track()


class PositionSample(NamedTuple):
    """One point of an aircraft's trail.

    ``timestamp`` uses the same monotonic clock as :class:`TrackStore`.
    ``altitude_ft`` may be ``None`` when only lat/lon are known at the
    time the sample is appended.
    """

    timestamp: float
    latitude: float
    longitude: float
    altitude_ft: float | None


class TelemetrySample(NamedTuple):
    """One point in an aircraft's altitude / speed / vertical-rate history.

    Recorded on every :meth:`TrackStore.upsert` (one sample per snapshot
    from ``dump1090``) so the detail pane can plot climb profiles and
    speed history for a selected aircraft. Any of the numeric fields
    may be ``None`` when that field was not (yet) observed; downstream
    plotting should skip such gaps.
    """

    timestamp: float
    altitude_ft: float | None
    ground_speed_kt: float | None
    vertical_rate_fpm: float | None


class TrackStore:
    """Thread-safe collection containing the latest active aircraft tracks.

    In addition to the latest state, the store keeps a bounded position
    history per aircraft so that map views can draw trails without
    maintaining independent state. ``history_size`` bounds the deque per
    ICAO; the default (120) is about two minutes of ADS-B position
    reports at typical rates.

    Snapshots from ``dump1090`` intermittently drop fields for aircraft
    that are still being tracked -- weak-signal position dropouts are
    the most common example. ``upsert`` merges each incoming track with
    the previously stored one so that an incoming ``None`` preserves the
    last observed value instead of clobbering it. Preserved values age
    out after ``field_stale_after_s`` so a callsign observed once
    minutes ago cannot linger indefinitely.
    """

    def __init__(
        self,
        *,
        stale_after_s: float = 60.0,
        history_size: int = 120,
        telemetry_size: int = 1200,
        field_stale_after_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _require_finite("stale_after_s", stale_after_s)
        if stale_after_s <= 0.0:
            raise ValueError("stale_after_s must be positive")
        if history_size <= 0:
            raise ValueError("history_size must be positive")
        if telemetry_size <= 0:
            raise ValueError("telemetry_size must be positive")
        _require_finite("field_stale_after_s", field_stale_after_s)
        if field_stale_after_s <= 0.0:
            raise ValueError("field_stale_after_s must be positive")
        self.stale_after_s = float(stale_after_s)
        self.history_size = int(history_size)
        self.telemetry_size = int(telemetry_size)
        self.field_stale_after_s = float(field_stale_after_s)
        self._clock = clock
        self._tracks: dict[str, AircraftTrack] = {}
        self._histories: dict[str, deque[PositionSample]] = {}
        #: Per-ICAO altitude / speed / vertical-rate history, one sample
        #: per snapshot, bounded by ``telemetry_size`` (default 1200
        #: samples is about 20 minutes at the default 1 Hz poll rate).
        self._telemetry: dict[str, deque[TelemetrySample]] = {}
        #: Per-ICAO timestamp of the last snapshot that carried a non-None
        #: value for each field. Used to age preserved values out.
        self._field_seen: dict[str, dict[str, float]] = {}
        self._lock = threading.RLock()

    def upsert(self, track: AircraftTrack) -> bool:
        """Insert or field-level merge a track.

        Returns ``False`` and leaves the store unchanged when an update is
        strictly older than the currently stored state for the same aircraft.
        Fields that are ``None`` on the incoming track preserve the
        previously observed value (subject to ``field_stale_after_s``),
        so a snapshot that transiently drops a field does not blank it in
        the store. Appends a :class:`PositionSample` to the aircraft's
        history when the merged track carries a fresh lat/lon that
        differs from the last stored sample.
        """

        if not isinstance(track, AircraftTrack):
            raise TypeError("track must be an AircraftTrack")
        with self._lock:
            current = self._tracks.get(track.icao)
            if current is not None and track.last_seen < current.last_seen:
                return False

            merged = self._merge_locked(current, track)
            self._tracks[merged.icao] = merged

            # One telemetry sample per snapshot, drawn from the merged
            # values so preserved altitude / speed data continues to
            # appear on the plot through transient dropouts.
            telemetry = self._telemetry.setdefault(
                merged.icao, deque(maxlen=self.telemetry_size)
            )
            telemetry.append(
                TelemetrySample(
                    timestamp=merged.last_seen,
                    altitude_ft=merged.altitude_ft,
                    ground_speed_kt=merged.ground_speed_kt,
                    vertical_rate_fpm=merged.vertical_rate_fpm,
                )
            )

            # Only append to history when *this* snapshot actually carried a
            # position. Preserved lat/lon from a previous snapshot has already
            # been recorded in the history and must not be duplicated with
            # the newer timestamp.
            if track.latitude is not None and track.longitude is not None:
                history = self._histories.get(merged.icao)
                if history is None:
                    history = deque(maxlen=self.history_size)
                    self._histories[merged.icao] = history
                    should_append = True
                else:
                    last = history[-1] if history else None
                    should_append = (
                        last is None
                        or last.latitude != track.latitude
                        or last.longitude != track.longitude
                    )
                if should_append:
                    history.append(
                        PositionSample(
                            timestamp=track.last_seen,
                            latitude=track.latitude,
                            longitude=track.longitude,
                            altitude_ft=track.altitude_ft,
                        )
                    )
            return True

    def _merge_locked(
        self, current: AircraftTrack | None, incoming: AircraftTrack
    ) -> AircraftTrack:
        """Combine ``incoming`` with ``current`` using field-level merging.

        Fields present on ``incoming`` win outright and refresh their
        per-field ``last observed at`` timestamp. Fields absent from
        ``incoming`` fall back to the previously stored value, provided
        that value was observed within ``field_stale_after_s`` of the
        incoming snapshot; otherwise the field ages out to ``None``.
        ``first_seen`` collapses to the earliest observation across all
        merges so it stays meaningful over an aircraft's lifetime.
        """

        seen = self._field_seen.setdefault(incoming.icao, {})

        if current is None:
            for name in _MERGEABLE_FIELDS:
                if getattr(incoming, name) is not None:
                    seen[name] = incoming.last_seen
            return incoming

        earliest_first_seen = current.first_seen
        if (
            incoming.first_seen is not None
            and earliest_first_seen is not None
            and incoming.first_seen < earliest_first_seen
        ):
            earliest_first_seen = incoming.first_seen

        merged_values: dict[str, Any] = {}
        for name in _MERGEABLE_FIELDS:
            new_value = getattr(incoming, name)
            if new_value is not None:
                merged_values[name] = new_value
                seen[name] = incoming.last_seen
                continue
            cached = getattr(current, name)
            if cached is None:
                merged_values[name] = None
                continue
            last_field_ts = seen.get(name, current.last_seen)
            if incoming.last_seen - last_field_ts <= self.field_stale_after_s:
                merged_values[name] = cached
            else:
                seen.pop(name, None)
                merged_values[name] = None

        return AircraftTrack(
            icao=incoming.icao,
            last_seen=incoming.last_seen,
            first_seen=earliest_first_seen,
            **merged_values,
        )

    def snapshot(self, *, now: float | None = None) -> tuple[AircraftTrack, ...]:
        """Return active tracks sorted by ICAO, expiring stale entries first."""

        with self._lock:
            self._expire_stale_locked(self._resolve_now(now))
            return tuple(self._tracks[icao] for icao in sorted(self._tracks))

    def history(self, icao: str) -> tuple[PositionSample, ...]:
        """Return a copy of the position history for one aircraft."""

        key = _normalize_icao(icao)
        with self._lock:
            entries = self._histories.get(key)
            if entries is None:
                return ()
            return tuple(entries)

    def telemetry(self, icao: str) -> tuple[TelemetrySample, ...]:
        """Return a copy of the altitude / speed / vertical-rate history
        for one aircraft, oldest sample first."""

        key = _normalize_icao(icao)
        with self._lock:
            entries = self._telemetry.get(key)
            if entries is None:
                return ()
            return tuple(entries)

    def histories(
        self, *, now: float | None = None
    ) -> Mapping[str, tuple[PositionSample, ...]]:
        """Return all position histories in one lock acquisition.

        Stale tracks are expired first so callers do not draw trails for
        aircraft that have dropped out of the store.
        """

        with self._lock:
            self._expire_stale_locked(self._resolve_now(now))
            return {icao: tuple(entries) for icao, entries in self._histories.items()}

    def expire_stale(
        self, *, now: float | None = None
    ) -> tuple[AircraftTrack, ...]:
        """Remove and return stale tracks, sorted by ICAO."""

        with self._lock:
            return self._expire_stale_locked(self._resolve_now(now))

    def _resolve_now(self, now: float | None) -> float:
        resolved = self._clock() if now is None else now
        _require_finite("now", resolved)
        return resolved

    def _expire_stale_locked(self, now: float) -> tuple[AircraftTrack, ...]:
        stale_icaos = sorted(
            icao
            for icao, track in self._tracks.items()
            if now - track.last_seen > self.stale_after_s
        )
        expired: list[AircraftTrack] = []
        for icao in stale_icaos:
            expired.append(self._tracks.pop(icao))
            self._histories.pop(icao, None)
            self._telemetry.pop(icao, None)
            self._field_seen.pop(icao, None)
        return tuple(expired)
