from datetime import date, timedelta

import pytest

from flights_converter import DataRejected, Reducer

from .conftest import archive, body, consume, point


def segments(records):
    return [r for r in records if r["record_type"] == "eligible_segment"]


def totals(records, field):
    return sum(r[field] for r in records if r["record_type"] == "airport_piece")


def overnight(day, *, flags=0):
    first = [point(64740, "ground", callsign="TST123"), point(64770, "ground")]
    second = []
    for seconds in range(64800, 108001, 1200):
        row = point(seconds % 86400, 35000, lon=80 * (seconds - 64800) / 43200)
        (first if seconds < 86400 else second).append(row)
    second += [point(21660, "ground", lon=80), point(21690, "ground", lon=80)]
    return body(day, rows=first, flags=flags), body(
        day + timedelta(days=1), rows=second, flags=flags
    )


@pytest.mark.parametrize(
    "start",
    [
        date(2025, 12, 31),
        date(2026, 1, 7),
        date(2026, 3, 7),
        date(2026, 6, 30),
        date(2026, 10, 31),
    ],
)
def test_five_overnight_flights_one_canonical_key(reducer, start):
    first, second = overnight(start)
    rows1, _ = consume(reducer, start, [first])
    rows2, summary = consume(reducer, start + timedelta(days=1), [second])
    canonical = {(r["hex"], r["t0"]): r for r in segments(rows1 + rows2)}
    assert len(canonical) == 1
    flight = next(iter(canonical.values()))
    assert flight["dep_at"] == f"{start}T18:00:00Z"
    assert flight["arr_at"] == f"{start + timedelta(days=1)}T06:01:00Z"
    assert flight["t1"] == f"{start + timedelta(days=1)}T06:00:00Z"
    assert flight["from_icao"] == "TEST"
    assert flight["to_icao"] == "DEST"
    assert flight["release_days"] == [str(start), str(start + timedelta(days=1))]
    assert not flight["open_start"] and not flight["open_end"]
    assert flight["max_alt_ft"] == 35000
    assert flight["km"] == pytest.approx(8895.606, abs=0.01)
    assert totals(rows1 + rows2, "deps") == 1
    assert totals(rows1 + rows2, "arrs") == 1
    assert summary["predecessor_continuity"]
    assert not summary["boundary_done"]
    arrival = next(r for r in rows2 if r["record_type"] == "airport_piece")
    assert arrival["day"] == str(start + timedelta(days=1))


def test_warmup_suppresses_departure_but_preserves_original_key(reducer, day):
    first, second = overnight(day)
    warmup, summary = consume(reducer, day, [first], warmup=True)
    assert summary["warmup"]
    assert not segments(warmup) and not totals(warmup, "deps")
    records, _ = consume(reducer, day + timedelta(days=1), [second])
    assert segments(records)[0]["t0"] == f"{day}T18:00:00Z"
    assert totals(records, "deps") == 0 and totals(records, "arrs") == 1


@pytest.mark.parametrize("flags", [0, 4, 8, 5, 9, 12, 13, 16, None])
def test_private_overnights_counts_only(airports, day, flags):
    reducer = Reducer(airports, {"version": "empty-v1", "rules": []})
    first, second = overnight(day, flags=flags)
    consume(reducer, day, [first], warmup=True)
    records, _ = consume(reducer, day + timedelta(days=1), [second])
    assert not segments(records)
    assert totals(records, "deps") == 0 and totals(records, "arrs") == 1
    reducer.close()
    assert not reducer._tracks


def test_cold_start_landing_does_not_invent_departure(reducer, day):
    _, second = overnight(day)
    second["trace"][0][8] = {"flight": "TST123"}
    records, summary = consume(reducer, day + timedelta(days=1), [second])
    assert segments(records)[0]["open_start"]
    assert not summary["predecessor_continuity"]
    assert totals(records, "deps") == 0 and totals(records, "arrs") == 1


@pytest.mark.parametrize("seconds,count,observed", [(29, 2, False), (30, 2, True), (60, 1, False)])
def test_ground_hysteresis(reducer, day, seconds, count, observed):
    rows = [point(0, "ground", callsign="TST123")]
    if count == 2:
        rows.append(point(seconds, "ground"))
    rows += [
        point(90, 1000),
        point(100, 1000),
        point(120, "ground"),
        point(120 + seconds, "ground"),
    ]
    records, _ = consume(reducer, day, [body(day, rows=rows)])
    flight = segments(records)[0]
    assert flight["dep_observed"] is observed
    assert flight["arr_observed"] is (seconds >= 30)


def test_chatter_callsign_and_leg_marker_do_not_split(reducer, day):
    rows = [
        point(0, "ground", callsign="TST123"),
        point(30, "ground"),
        point(60, 1000),
        point(90, "ground"),
        point(100, 1500, flags=2, callsign="TST999"),
        point(120, 2000),
        point(150, "ground"),
        point(180, "ground"),
    ]
    records, _ = consume(reducer, day, [body(day, rows=rows)])
    assert len(segments(records)) == 1
    assert totals(records, "deps") == totals(records, "arrs") == 1


def test_zero_altitude_and_geometric_maximum(reducer, day):
    rows = [point(0, 0, callsign="TST123"), point(30, 2000), point(60, 50000, flags=8)]
    records, _ = consume(reducer, day, [body(day, rows=rows)])
    flight = segments(records)[0]
    assert flight["open_start"] and flight["max_alt_ft"] == 2000
    assert totals(records, "deps") == 0


@pytest.mark.parametrize("gap,lon,joined", [(1800, 0, True), (1800.01, 0, False), (60, 80, False)])
def test_boundary_gap_and_speed(reducer, day, gap, lon, joined):
    first = body(day, rows=[point(86360, callsign="TST123"), point(86390)])
    second = body(
        day + timedelta(days=1),
        rows=[point(gap - 10, lon=lon, callsign="TST123"), point(gap + 20, lon=lon)],
    )
    consume(reducer, day, [first])
    records, _ = consume(reducer, day + timedelta(days=1), [second])
    flights = segments(records)
    if joined:
        assert flights[-1]["t0"] == f"{day}T23:59:20Z"
    else:
        assert len(flights) == 2
        assert flights[-1]["open_start"]


@pytest.mark.parametrize("field,value", [("r", "SYNTH-NEW"), ("t", "NEW")])
def test_changed_airframe_breaks_join(reducer, day, field, value):
    first, second = overnight(day)
    second[field] = value
    second["trace"][0][8] = {"flight": "TST123"}
    consume(reducer, day, [first])
    records, _ = consume(reducer, day + timedelta(days=1), [second])
    assert segments(records)[-1]["open_start"]


def test_missing_or_rejected_day_breaks_continuity(reducer, day):
    first, second = overnight(day)
    second["trace"][0][8] = {"flight": "TST123"}
    consume(reducer, day, [first])
    with pytest.raises(DataRejected):
        consume(reducer, day + timedelta(days=1), data=archive([second])[:-9000])
    records, summary = consume(reducer, day + timedelta(days=1), [second])
    assert not summary["predecessor_continuity"]
    assert segments(records)[0]["open_start"]
    reducer.break_continuity()
    assert reducer._last_day is None


def test_repeat_and_out_of_order_rebuild_connected_days(reducer, day):
    first, second = overnight(day)

    # Adapter rebuilds the affected component from bounded ordered inputs. Generation
    # replacement removes any provisional D+1 key and any previous anonymous pieces.
    def replay():
        reducer.break_continuity()
        a, _ = consume(reducer, day, [first])
        b, _ = consume(reducer, day + timedelta(days=1), [second])
        return {(r["hex"], r["t0"]): r for r in segments(a + b)}, totals(a + b, "deps")

    expected = replay()
    assert replay() == expected
    consume(reducer, day, [first])
    assert replay() == expected
    consume(reducer, day + timedelta(days=1), [second])
    consume(reducer, day, [first])
    assert replay() == expected


def test_48_hour_unresolved_fragment_bound(reducer, day):
    # A continuously observed unresolved movement must split beyond 48 hours.
    keys = set()
    for offset in range(3):
        current = day + timedelta(days=offset)
        rows = [point(s, 1000, callsign="TST123") for s in range(0, 86400, 1200)]
        records, _ = consume(reducer, current, [body(current, rows=rows)])
        keys.update(r["t0"] for r in segments(records))
    assert len(keys) == 2


def test_midnight_departure_counts_once_in_new_day(reducer, day):
    first = body(day, rows=[point(86340, "ground", callsign="TST123"), point(86370, "ground")])
    second = body(
        day + timedelta(days=1),
        rows=[
            point(0, 1000, callsign="TST123"),
            point(10, 1000),
            point(30, "ground"),
            point(60, "ground"),
        ],
    )
    a, _ = consume(reducer, day, [first])
    b, _ = consume(reducer, day + timedelta(days=1), [second])
    assert totals(a, "deps") == 0 and totals(b, "deps") == 1
    assert segments(b)[0]["dep_at"] == f"{day + timedelta(days=1)}T00:00:00Z"
    assert all(
        r["day"] == str(day + timedelta(days=1)) for r in b if r["record_type"] == "airport_piece"
    )


def test_ground_confirmation_can_cross_midnight(reducer, day):
    first = body(
        day,
        rows=[point(86350, 1000, callsign="TST123"), point(86360, 1000), point(86390, "ground")],
    )
    second = body(day + timedelta(days=1), rows=[point(20, "ground")])
    a, _ = consume(reducer, day, [first])
    b, _ = consume(reducer, day + timedelta(days=1), [second])
    assert totals(a, "arrs") == 0 and totals(b, "arrs") == 1
    assert segments(b)[0]["arr_at"] == f"{day}T23:59:50Z"
    piece = next(r for r in b if r["record_type"] == "airport_piece")
    assert piece["day"] == str(day)
    assert piece["release_days"] == [str(day), str(day + timedelta(days=1))]


def test_boundary_privacy_change_requests_anonymous_retraction(reducer, day):
    first, second = overnight(day)
    second["dbFlags"] = 5
    consume(reducer, day, [first])
    records, _ = consume(reducer, day + timedelta(days=1), [second])
    assert not segments(records)
    diagnostic = next(r for r in records if r["record_type"] == "diagnostics")
    assert diagnostic["retract_release_days"] == [str(day)]
    assert "f0abcd" not in str(records)


def test_stale_ground_reports_cannot_establish_departure(reducer, day):
    value = body(
        day,
        rows=[
            point(0, "ground", callsign="TST123", flags=1),
            point(30, "ground", flags=1),
            point(60, 1000),
            point(90, 1000),
        ],
    )
    records, _ = consume(reducer, day, [value])
    assert totals(records, "deps") == 0 and segments(records)[0]["open_start"]


def test_interrupted_callback_discards_boundary_state(reducer, day):
    import io

    first, _ = overnight(day)

    def interrupt(record):
        if record["record_type"] == "eligible_segment":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        reducer.consume(day, io.BytesIO(archive([first])), interrupt)
    assert reducer._last_day is None and not reducer._tracks
