import math
from datetime import UTC, date, datetime, timedelta

import pytest

from flights_converter import Airport, ReleaseState, airport_day, load_airports, release_ready
from flights_converter.airports import EARTH_KM, AirportIndex, local_interval


def airport(zone="UTC", **kwargs):
    return Airport(
        "TEST",
        kwargs.get("lat", 0),
        kwargs.get("lon", 0),
        kwargs.get("elevation", 0),
        zone,
        "reference-v1",
        "tzdb-v1",
    )


@pytest.mark.parametrize(
    "km,alt,matched",
    [
        (5, 1500, True),
        (5.0001, 0, False),
        (4.9999, 1500.001, False),
        (0, None, True),
        (0, -1500, True),
        (0, -1500.1, False),
    ],
)
def test_airport_boundary(km, alt, matched):
    index = AirportIndex([airport()])
    position = (0, math.degrees(km / EARTH_KM))
    assert bool(index.match(position, alt)) is matched


def test_equal_candidates_are_unknown():
    a = airport()
    b = Airport("OTHER", 0, 0, None, "UTC", "reference-v1", "tzdb-v1")
    assert AirportIndex([a, b]).match((0, 0)) is None


@pytest.mark.parametrize(
    "lat,lon,position", [(0, 179.99, (0, -179.99)), (89.99, 100, (89.99, -100))]
)
def test_index_wraps_dateline_and_poles(lat, lon, position):
    assert AirportIndex([airport(lat=lat, lon=lon)]).match(position)


def ready_releases(start, end):
    result = {}
    day = start.date()
    while day <= end.date() + timedelta(days=1):
        result[day] = ReleaseState(str(day), True, True, True, str(day + timedelta(days=1)))
        day += timedelta(days=1)
    return result


@pytest.mark.parametrize(
    "zone,day,seconds",
    [
        ("America/New_York", date(2026, 3, 8), 23 * 3600),
        ("America/New_York", date(2026, 11, 1), 25 * 3600),
        ("UTC", date(2026, 1, 1), 86400),
        ("Asia/Dubai", date(2026, 1, 1), 86400),
        ("Asia/Kathmandu", date(2026, 1, 1), 86400),
        ("Australia/Lord_Howe", date(2026, 10, 4), 23.5 * 3600),
        ("Australia/Lord_Howe", date(2026, 4, 5), 24.5 * 3600),
        ("UTC", date(2028, 2, 29), 86400),
        ("UTC", date(2025, 12, 31), 86400),
    ],
)
def test_exact_elapsed_local_days(zone, day, seconds):
    a = airport(zone)
    start, end = local_interval(a, day)
    result = airport_day(a, day, [], ready_releases(start, end))
    assert result["day_seconds"] == result["n_seconds"] == seconds
    assert result["n_hours"] == seconds / 3600
    assert result["final"] and result["deps"] == result["arrs"] == 0


def test_fractional_boundary_missing_one_release_keeps_exact_seconds():
    a, day = airport("Asia/Kathmandu"), date(2026, 1, 1)
    start, end = local_interval(a, day)
    states = ready_releases(start, end)
    states[start.date()] = ReleaseState(str(start.date()))
    result = airport_day(a, day, [], states)
    assert result["n_seconds"] == 18 * 3600 + 15 * 60
    assert result["day_seconds"] == 86400
    assert result["deps"] is None and result["arrs"] is None and not result["final"]


@pytest.mark.parametrize("zone", [None, "Invalid/SENTINEL"])
def test_missing_zone_never_falls_back_to_utc(zone):
    result = airport_day(airport(zone), date(2026, 1, 1), [], {})
    assert result["day_seconds"] is None and result["deps"] is None


def test_successor_and_manifest_hash_are_required():
    day = date(2026, 1, 1)
    states = {day: ReleaseState("one", True, True, True, "two")}
    assert not release_ready(day, states)
    states[day + timedelta(days=1)] = ReleaseState("two", True, True)
    assert release_ready(day, states)  # Successor need not itself be ready.
    states[day + timedelta(days=1)] = ReleaseState("replaced", True, True)
    assert not release_ready(day, states)
    result = airport_day(airport(), day, [], states)
    assert result["deps"] is None and not result["final"]


def test_zone_version_mismatch_withholds_old_pieces():
    day = date(2026, 1, 1)
    a = airport()
    interval = local_interval(a, day)
    pieces = [
        {
            "icao": "TEST",
            "day": str(day),
            "tz": "UTC",
            "tz_version": "old",
            "reference_version": "reference-v1",
            "deps": 2,
            "arrs": 3,
            "release_days": [str(day)],
        }
    ]
    result = airport_day(a, day, pieces, ready_releases(*interval))
    assert result["n_seconds"] == 86400 and result["deps"] is None
    pieces[0]["tz_version"] = "tzdb-v1"
    assert airport_day(a, day, pieces, ready_releases(*interval))["deps"] == 2


def test_caller_supplied_airport_snapshot():
    config = {
        "version": "fixture-v1",
        "tz_version": "tzdb-test",
        "airports": [
            {"icao": "TEST", "lat": 0, "lon": 0, "tz": "UTC"},
        ],
    }
    assert load_airports(config)[0].reference_version == "fixture-v1"
    assert local_interval(load_airports(config)[0], date(2026, 1, 1))[0] == datetime(
        2026, 1, 1, tzinfo=UTC
    )
