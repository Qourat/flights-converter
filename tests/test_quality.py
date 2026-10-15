from datetime import date, timedelta

import pytest

from flights_converter.quality import ReleaseStats, assess, calibrate


def stats(day, *, size=1000, count=100, complete=True):
    return ReleaseStats(day, size, (count,) * 24, complete)


def history(day):
    return [stats(day - timedelta(days=7 * n)) for n in range(1, 5)]


def test_size_and_hour_are_independent_or_rejections():
    day = date(2026, 1, 29)
    refs = history(day)
    for ratio in (0.649, 0.742, 0.777, 0.849):
        result = assess(
            stats(day, size=int(ratio * 1000)), refs, hourly_factor=0.7, rule_version="test-v1"
        )
        assert not result["good"] and result["reason"] == "size_below_threshold"
    hours = [100] * 24
    hours[12] = 69
    result = assess(
        ReleaseStats(day, 1000, tuple(hours)), refs, hourly_factor=0.7, rule_version="test-v1"
    )
    assert not result["good"] and result["gap_hours"] == [12]
    assert assess(stats(day, size=850, count=70), refs, hourly_factor=0.7, rule_version="test-v1")[
        "good"
    ]


def test_four_prior_complete_same_weekday_references_required():
    day = date(2026, 1, 29)
    refs = history(day)
    poisoned = [
        stats(day + timedelta(days=7), size=999999),
        stats(day - timedelta(days=1), size=999999),
        stats(day - timedelta(days=35), size=999999),
    ]
    expected = assess(stats(day), refs, hourly_factor=0.7, rule_version="test-v1")
    assert (
        assess(stats(day), refs + poisoned, hourly_factor=0.7, rule_version="test-v1") == expected
    )
    assert not assess(stats(day), refs[:3], hourly_factor=0.7, rule_version="test-v1")["good"]


def test_zero_reference_hours_do_not_prove_completeness():
    day = date(2026, 1, 29)
    refs = [stats(day - timedelta(days=7 * n), count=0) for n in range(1, 5)]
    assert not assess(stats(day), refs, hourly_factor=0.7, rule_version="test-v1")["good"]


def test_f_calibration_returns_table_and_stops_on_conflict():
    day = date(2026, 1, 29)
    audit = [
        (stats(day, count=75), history(day), True),
        (stats(day, count=65), history(day), False),
    ]
    result = calibrate(audit, rule_version="test-v1")
    assert result["factor"] == 0.7
    assert len(result["table"]) == 4
    assert (
        calibrate([(stats(day), history(day), False), *audit], rule_version="test-v1")["factor"]
        is None
    )
    assert calibrate([], rule_version="test-v1")["factor"] is None


def test_duplicate_reference_dates_rejected():
    day = date(2026, 1, 29)
    refs = history(day)
    with pytest.raises(ValueError, match="duplicate reference"):
        assess(stats(day), refs + [refs[0]], hourly_factor=0.7, rule_version="test-v1")


def test_size_rejection_cannot_stand_in_for_detecting_known_hour_outage():
    day = date(2026, 1, 29)
    audit = [(stats(day), history(day), True), (stats(day, size=100), history(day), False)]
    assert calibrate(audit, rule_version="test-v1")["factor"] is None
