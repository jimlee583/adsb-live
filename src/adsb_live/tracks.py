"""Aircraft track model and thread-safe in-memory storage."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


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


class TrackStore:
    """Thread-safe collection containing the latest active aircraft tracks."""

    def __init__(
        self,
        *,
        stale_after_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _require_finite("stale_after_s", stale_after_s)
        if stale_after_s <= 0.0:
            raise ValueError("stale_after_s must be positive")
        self.stale_after_s = float(stale_after_s)
        self._clock = clock
        self._tracks: dict[str, AircraftTrack] = {}
        self._lock = threading.RLock()

    def upsert(self, track: AircraftTrack) -> bool:
        """Insert or replace a track.

        Returns ``False`` and leaves the store unchanged when an update is
        older than the currently stored state for the same aircraft.
        """

        if not isinstance(track, AircraftTrack):
            raise TypeError("track must be an AircraftTrack")
        with self._lock:
            current = self._tracks.get(track.icao)
            if current is not None and track.last_seen < current.last_seen:
                return False
            self._tracks[track.icao] = track
            return True

    def snapshot(self, *, now: float | None = None) -> tuple[AircraftTrack, ...]:
        """Return active tracks sorted by ICAO, expiring stale entries first."""

        with self._lock:
            self._expire_stale_locked(self._resolve_now(now))
            return tuple(self._tracks[icao] for icao in sorted(self._tracks))

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
        return tuple(self._tracks.pop(icao) for icao in stale_icaos)
