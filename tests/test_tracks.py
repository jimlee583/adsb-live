from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from adsb_live import AircraftTrack, TrackStore


def test_aircraft_track_is_immutable_and_normalizes_identifiers() -> None:
    track = AircraftTrack(
        icao=" ab12ef ",
        callsign=" aal123 ",
        category=" a3 ",
        last_seen=12.5,
    )

    assert track.icao == "AB12EF"
    assert track.callsign == "AAL123"
    assert track.category == "A3"
    assert track.first_seen == 12.5
    assert not hasattr(track, "__dict__")

    with pytest.raises(FrozenInstanceError):
        track.callsign = "NEW123"  # type: ignore[misc]


@pytest.mark.parametrize("icao", ["", "12345", "1234567", "12X456"])
def test_aircraft_track_rejects_invalid_icao(icao: str) -> None:
    with pytest.raises(ValueError, match="six hexadecimal"):
        AircraftTrack(icao=icao, last_seen=1.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"last_seen": float("inf")}, "last_seen must be finite"),
        ({"last_seen": 1.0, "first_seen": 2.0}, "first_seen cannot be later"),
        ({"last_seen": 1.0, "latitude": -90.1}, "latitude must be between"),
        ({"last_seen": 1.0, "longitude": 180.1}, "longitude must be between"),
        ({"last_seen": 1.0, "ground_speed_kt": -0.1}, "cannot be negative"),
        ({"last_seen": 1.0, "track_deg": 360.0}, "less than 360"),
        ({"last_seen": 1.0, "squawk": "1289"}, "four octal digits"),
        ({"last_seen": 1.0, "signal_db": float("nan")}, "signal_db must be finite"),
    ],
)
def test_aircraft_track_validates_domain_values(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AircraftTrack(icao="ABC123", **kwargs)  # type: ignore[arg-type]


def test_empty_callsign_and_category_are_normalized_to_none() -> None:
    track = AircraftTrack(
        icao="ABC123",
        callsign="  ",
        category="",
        last_seen=1.0,
    )

    assert track.callsign is None
    assert track.category is None


def test_upsert_adds_tracks_and_snapshot_is_sorted_and_immutable() -> None:
    store = TrackStore(stale_after_s=60.0, clock=lambda: 10.0)
    second = AircraftTrack(icao="BBBBBB", last_seen=5.0)
    first = AircraftTrack(icao="AAAAAA", last_seen=5.0)

    assert store.upsert(second) is True
    assert store.upsert(first) is True

    snapshot = store.snapshot()
    assert snapshot == (first, second)
    assert isinstance(snapshot, tuple)


def test_upsert_replaces_equal_or_newer_state() -> None:
    store = TrackStore(stale_after_s=60.0, clock=lambda: 10.0)
    initial = AircraftTrack(icao="ABC123", callsign="OLD", last_seen=5.0)
    equal_time = AircraftTrack(icao="abc123", callsign="EQUAL", last_seen=5.0)
    newer = AircraftTrack(icao="ABC123", callsign="NEW", last_seen=6.0)

    store.upsert(initial)
    assert store.upsert(equal_time) is True
    assert store.upsert(newer) is True
    assert store.snapshot() == (newer,)


def test_upsert_ignores_out_of_order_state() -> None:
    store = TrackStore(stale_after_s=60.0, clock=lambda: 10.0)
    current = AircraftTrack(icao="ABC123", callsign="CURRENT", last_seen=8.0)
    older = AircraftTrack(icao="ABC123", callsign="OLDER", last_seen=7.0)

    store.upsert(current)

    assert store.upsert(older) is False
    assert store.snapshot() == (current,)


def test_upsert_rejects_values_other_than_aircraft_tracks() -> None:
    store = TrackStore()

    with pytest.raises(TypeError, match="must be an AircraftTrack"):
        store.upsert(object())  # type: ignore[arg-type]


def test_expire_stale_uses_injected_clock_and_keeps_boundary_value() -> None:
    now = 110.0
    store = TrackStore(stale_after_s=10.0, clock=lambda: now)
    stale = AircraftTrack(icao="AAAAAA", last_seen=99.9)
    boundary = AircraftTrack(icao="BBBBBB", last_seen=100.0)
    fresh = AircraftTrack(icao="CCCCCC", last_seen=105.0)
    for track in (fresh, boundary, stale):
        store.upsert(track)

    assert store.expire_stale() == (stale,)
    assert store.snapshot() == (boundary, fresh)


def test_snapshot_expires_stale_tracks_and_accepts_explicit_time() -> None:
    store = TrackStore(stale_after_s=5.0, clock=lambda: 0.0)
    stale = AircraftTrack(icao="AAAAAA", last_seen=1.0)
    fresh = AircraftTrack(icao="BBBBBB", last_seen=7.0)
    store.upsert(stale)
    store.upsert(fresh)

    assert store.snapshot(now=8.0) == (fresh,)
    assert store.expire_stale(now=100.0) == (fresh,)
    assert store.snapshot(now=100.0) == ()


@pytest.mark.parametrize("stale_after_s", [0.0, -1.0, float("inf")])
def test_track_store_rejects_invalid_expiration_interval(
    stale_after_s: float,
) -> None:
    with pytest.raises(ValueError):
        TrackStore(stale_after_s=stale_after_s)


def test_track_store_rejects_non_finite_expiration_time() -> None:
    store = TrackStore()

    with pytest.raises(ValueError, match="now must be finite"):
        store.expire_stale(now=float("nan"))


def test_concurrent_upserts_keep_latest_state_for_each_aircraft() -> None:
    store = TrackStore(stale_after_s=1_000.0, clock=lambda: 100.0)
    updates = [
        AircraftTrack(icao=f"{index:06X}", last_seen=float(version))
        for index in range(16)
        for version in range(10)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(store.upsert, reversed(updates)))

    snapshot = store.snapshot()
    assert len(snapshot) == 16
    assert all(track.last_seen == 9.0 for track in snapshot)
