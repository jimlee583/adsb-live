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


def test_aircraft_track_rejects_non_boolean_ground_state() -> None:
    with pytest.raises(TypeError, match="on_ground must be a bool"):
        AircraftTrack(
            icao="ABC123",
            last_seen=1.0,
            on_ground="yes",  # type: ignore[arg-type]
        )


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


def test_track_store_history_appends_only_on_new_position() -> None:
    store = TrackStore(stale_after_s=60.0, clock=lambda: 100.0)

    store.upsert(
        AircraftTrack(icao="A12345", last_seen=1.0, latitude=40.0, longitude=-105.0)
    )
    store.upsert(
        AircraftTrack(icao="A12345", last_seen=2.0, latitude=40.0, longitude=-105.0)
    )
    store.upsert(
        AircraftTrack(icao="A12345", last_seen=3.0, latitude=40.1, longitude=-105.0)
    )

    history = store.history("A12345")
    assert len(history) == 2
    assert history[0].latitude == 40.0
    assert history[1].latitude == 40.1
    # Timestamp on the sample tracks the source's last_seen.
    assert history[1].timestamp == 3.0


def test_track_store_history_ignores_updates_without_position() -> None:
    store = TrackStore(stale_after_s=60.0, clock=lambda: 100.0)

    store.upsert(AircraftTrack(icao="A12345", last_seen=1.0, callsign="TEST"))
    store.upsert(
        AircraftTrack(icao="A12345", last_seen=2.0, latitude=39.0, longitude=-104.0)
    )
    store.upsert(AircraftTrack(icao="A12345", last_seen=3.0, callsign="TEST"))

    history = store.history("A12345")
    assert len(history) == 1
    assert history[0].latitude == 39.0


def test_track_store_history_is_bounded_by_history_size() -> None:
    store = TrackStore(stale_after_s=60.0, history_size=3, clock=lambda: 100.0)
    for i in range(6):
        store.upsert(
            AircraftTrack(
                icao="A12345",
                last_seen=float(i),
                latitude=40.0 + 0.1 * i,
                longitude=-105.0,
            )
        )

    history = store.history("A12345")
    assert len(history) == 3
    assert [round(sample.latitude, 3) for sample in history] == [40.3, 40.4, 40.5]


def test_track_store_expiry_removes_position_history() -> None:
    now = {"value": 100.0}
    store = TrackStore(stale_after_s=10.0, clock=lambda: now["value"])
    store.upsert(
        AircraftTrack(icao="A12345", last_seen=100.0, latitude=40.0, longitude=-105.0)
    )
    assert store.history("A12345")

    now["value"] = 200.0
    store.snapshot()

    assert store.history("A12345") == ()
    assert "A12345" not in store.histories()


def test_track_store_histories_returns_snapshot_copy() -> None:
    store = TrackStore(stale_after_s=60.0, clock=lambda: 100.0)
    store.upsert(
        AircraftTrack(icao="A12345", last_seen=90.0, latitude=40.0, longitude=-105.0)
    )

    view = store.histories()
    assert set(view) == {"A12345"}
    # Snapshot is decoupled from the store: further upserts do not leak in.
    store.upsert(
        AircraftTrack(icao="A12345", last_seen=95.0, latitude=41.0, longitude=-105.0)
    )
    assert len(view["A12345"]) == 1
    assert len(store.history("A12345")) == 2


def test_track_store_rejects_non_positive_history_size() -> None:
    with pytest.raises(ValueError, match="history_size must be positive"):
        TrackStore(history_size=0)
