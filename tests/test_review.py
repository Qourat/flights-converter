"""Adversarial regressions using synthetic identities only."""

import gzip
import io
import json
import tarfile
import traceback
from datetime import date, timedelta

import pytest

from flights_converter import DataRejected, Reducer, trace
from flights_converter import reducer as reducer_module
from flights_converter.quality import assess, calibrate

from .conftest import archive, body, consume, member_name, payload, point
from .test_cli import configs, run
from .test_quality import history, stats
from .test_segment import segments, totals

SENTINELS = ("e7dead", "SENT-DENIED", "OWNER-SENTINEL", "PRIVATE-SENT")


def assert_private(text):
    for sentinel in SENTINELS:
        assert sentinel not in text


@pytest.mark.parametrize("gap,lon", [(1801, 0), (60, 80)])
def test_operator_inheritance_stops_at_discontinuity(reducer, day, gap, lon, capsys, caplog):
    first = body(
        day,
        identity=SENTINELS[0],
        r=SENTINELS[1],
        rows=[point(86360, callsign="TST123"), point(86390)],
    )
    consume(reducer, day, [first], warmup=True)
    tomorrow = day + timedelta(days=1)
    second = body(
        tomorrow,
        identity=SENTINELS[0],
        r=SENTINELS[1],
        ownOp=SENTINELS[2],
        rows=[point(gap - 10, lon=lon), point(gap + 20, lon=lon)],
    )
    records, summary = consume(reducer, tomorrow, [second])
    assert not segments(records)
    captured = capsys.readouterr()
    assert_private(json.dumps([records, summary]) + captured.out + captured.err + caplog.text)


def test_inherited_callsign_does_not_admit_a_new_takeoff(reducer, day):
    first = body(day, rows=[point(86360, callsign="TST123"), point(86390)])
    consume(reducer, day, [first], warmup=True)
    tomorrow = day + timedelta(days=1)
    second = body(
        tomorrow,
        rows=[
            point(0),
            point(30),
            point(60, "ground"),
            point(90, "ground"),
            point(120),
            point(150),
            point(180, "ground"),
            point(210, "ground"),
        ],
    )
    records, _ = consume(reducer, tomorrow, [second])
    assert len(segments(records)) == 1
    assert segments(records)[0]["t0"].startswith(str(day))
    assert totals(records, "deps") == 1 and totals(records, "arrs") == 2


@pytest.mark.parametrize("cleared", [None, "", "   "])
def test_explicit_callsign_clear_cannot_preserve_operator_classification(
    reducer, day, cleared, capsys, caplog
):
    value = body(day, identity=SENTINELS[0], r=SENTINELS[1], ownOp=SENTINELS[2])
    value["trace"][2][8] = {"flight": cleared}
    records, _ = consume(reducer, day, [value])
    assert not segments(records)
    captured = capsys.readouterr()
    assert_private(json.dumps(records) + captured.out + captured.err + caplog.text)


def test_later_operator_callsign_cannot_admit_earlier_unclassified_movement(reducer, day):
    value = body(day, identity=SENTINELS[0], callsign=None)
    value["trace"] += [point(180, callsign="TST123"), point(210)]
    records, _ = consume(reducer, day, [value])
    assert not segments(records)
    assert_private(json.dumps(records))


def test_empty_callsign_is_not_positive_even_with_a_broad_reviewed_pattern(airports, day):
    reducer = Reducer(
        airports, {"version": "synthetic-broad-v1", "rules": [{"code": "TST", "pattern": ".*"}]}
    )
    value = body(day, identity=SENTINELS[0])
    value["trace"][2][8] = {"flight": ""}
    records, _ = consume(reducer, day, [value])
    assert not segments(records)
    assert_private(json.dumps(records))


def test_private_trace_and_point_representations_do_not_expose_fields(day):
    value = body(day, identity=SENTINELS[0], r=SENTINELS[1], callsign=SENTINELS[3], flags=4)
    decoded = trace.decode_trace(payload(value), value["icao"], day)
    assert_private(repr(decoded))
    assert "lat=" not in repr(decoded.points[0])


def test_remembered_registration_denials_are_bounded(airports, policy, day, monkeypatch):
    monkeypatch.setattr(reducer_module, "MAX_AIRCRAFT", 1)
    reducer = Reducer(airports, policy, opt_outs=["reg:sent-denied"])
    for offset in range(2):
        current = day + timedelta(days=offset)
        consume(reducer, current, [body(current, identity=SENTINELS[0], r=SENTINELS[1])])
    current = day + timedelta(days=2)
    with pytest.raises(DataRejected, match="^aircraft_limit$"):
        consume(reducer, current, [body(current, identity="e7beef", r=SENTINELS[1])])
    assert not reducer._denied_hexes


@pytest.mark.parametrize("replacement", [None, "CHANGED-REG"])
def test_registration_opt_out_remains_denied_without_matching_snapshot(
    airports, policy, day, replacement, capsys, caplog
):
    reducer = Reducer(airports, policy, opt_outs=["reg:sent-denied"])
    for offset in range(3):
        current = day + timedelta(days=offset)
        value = body(
            current,
            identity=SENTINELS[0],
            r=SENTINELS[1] if offset == 0 else replacement,
            flags=1,
            ownOp=SENTINELS[2],
            rows=[point(86340, callsign="TST123"), point(86370)],
        )
        records, _ = consume(reducer, current, [value])
        assert not segments(records)
        captured = capsys.readouterr()
        assert_private(json.dumps(records) + captured.out + captured.err + caplog.text)
    reducer.close()
    assert not reducer._denied_hexes


def test_registration_deny_rules_fail_closed_when_registration_is_unavailable(
    airports, policy, day
):
    reducer = Reducer(airports, policy, opt_outs=["reg:sent-denied"])
    records, _ = consume(reducer, day, [body(day, identity=SENTINELS[0], r=None, flags=1)])
    assert not segments(records)
    assert_private(json.dumps(records))


@pytest.mark.parametrize("flags", [flag for flag in range(16) if flag & 12])
def test_each_privacy_flag_combination_overrides_positive_eligibility(
    reducer, day, flags, capsys, caplog
):
    records, _ = consume(
        reducer,
        day,
        [body(day, identity=SENTINELS[0], flags=flags, r=SENTINELS[1], ownOp=SENTINELS[2])],
    )
    assert not segments(records)
    captured = capsys.readouterr()
    assert_private(json.dumps(records) + captured.out + captured.err + caplog.text)


@pytest.mark.parametrize("flags", range(16))
@pytest.mark.parametrize("non_icao", [False, True])
def test_denials_override_all_flag_and_operator_combinations(
    airports, policy, day, flags, non_icao, capsys, caplog
):
    identity = ("~" if non_icao else "") + SENTINELS[0]
    reducer = Reducer(airports, policy, opt_outs=[] if non_icao else [f"hex:{identity}"])
    records, _ = consume(
        reducer,
        day,
        [body(day, identity=identity, flags=flags, r=SENTINELS[1], ownOp=SENTINELS[2])],
    )
    assert not segments(records)
    captured = capsys.readouterr()
    assert_private(json.dumps(records) + captured.out + captured.err + caplog.text)


@pytest.mark.parametrize(
    "rows",
    [
        [point(0, callsign="TST123")],
        [
            point(0, "ground", callsign="TST123"),
            point(30, "ground"),
            point(60),
            point(90, "ground"),
            point(120, "ground"),
        ],
        [point(0, callsign="TST123"), point(1801)],
    ],
)
def test_zero_duration_fragments_never_become_flights(reducer, day, rows):
    records, _ = consume(reducer, day, [body(day, rows=rows)])
    assert not segments(records)


def test_unknown_altitude_does_not_contribute_airborne_distance(reducer, day):
    records, _ = consume(
        reducer,
        day,
        [
            body(
                day,
                rows=[
                    point(0, callsign="TST123"),
                    point(30, None, lon=0.01),
                    point(60, lon=0.02),
                    point(90, lon=0.03),
                ],
            )
        ],
    )
    assert segments(records)[0]["km"] == pytest.approx(1.111951, abs=0.00001)


@pytest.mark.parametrize("knots,joined", [(1000, True), (1000.001, False)])
def test_speed_threshold_is_inclusive(reducer, day, knots, joined):
    # At the equator, radians * Earth radius gives the observed distance.
    import math

    from flights_converter.airports import EARTH_KM

    lon = math.degrees((knots * 1.852 / 60) / EARTH_KM)
    records, _ = consume(
        reducer,
        day,
        [
            body(
                day,
                rows=[
                    point(0, callsign="TST123"),
                    point(30),
                    point(90, lon=lon),
                    point(120, lon=lon),
                ],
            )
        ],
    )
    assert len(segments(records)) == (1 if joined else 2)


def test_48_hour_boundary_does_not_inherit_into_expired_movement(reducer, day):
    for offset in range(2):
        current = day + timedelta(days=offset)
        consume(
            reducer,
            current,
            [body(current, rows=[point(s, callsign="TST123") for s in range(0, 86400, 1200)])],
            warmup=True,
        )
    current = day + timedelta(days=2)
    records, _ = consume(
        reducer, current, [body(current, rows=[point(0), point(1200), point(2400)])]
    )
    assert len(segments(records)) == 1
    assert segments(records)[0]["t0"] == f"{day}T00:00:00Z"
    assert segments(records)[0]["t1"] == f"{current}T00:00:00Z"


def test_receipt_and_diagnostic_fields_are_closed_aggregate_schemas(reducer, day):
    records, summary = consume(reducer, day, [body(day, flags=4, identity=SENTINELS[0])])
    assert set(summary) == {
        "record_type",
        "day",
        "input_bytes",
        "n_members",
        "skipped_members",
        "skipped_bytes",
        "n_aircraft",
        "hour_aircraft",
        "parse_errors",
        "date_errors",
        "duplicate_members",
        "method_version",
        "operator_version",
        "parse_complete",
        "predecessor_continuity",
        "boundary_done",
        "warmup",
    }
    diagnostic = next(r for r in records if r["record_type"] == "diagnostics")
    assert set(diagnostic) == {
        "record_type",
        "day",
        "predecessor_continuity",
        "unknown_zone_endpoints",
        "retract_release_days",
        "boundary_done",
    }
    assert_private(json.dumps(records))


def test_unrecognised_rejection_code_cannot_echo_input(reducer, day, capsys, caplog):
    class Source:
        def read(self, size):
            raise DataRejected(" ".join(SENTINELS))

    records = []
    with pytest.raises(DataRejected, match="^rejected$") as error:
        reducer.consume(day, Source(), records.append)
    captured = capsys.readouterr()
    assert_private(
        json.dumps(records) + str(error.value) + captured.out + captured.err + caplog.text
    )


def test_late_denied_duplicate_never_exposes_earlier_candidate(reducer, day, capsys, caplog):
    allowed = body(day, identity=SENTINELS[0], r=SENTINELS[1], ownOp=SENTINELS[2])
    denied = {**allowed, "dbFlags": 5}
    records = []
    with pytest.raises(DataRejected, match="^conflicting_duplicate$") as error:
        reducer.consume(day, io.BytesIO(archive([allowed, denied])), records.append)
    assert not segments(records)
    captured = capsys.readouterr()
    assert_private(
        json.dumps(records) + str(error.value) + captured.out + captured.err + caplog.text
    )


def test_pending_entity_output_is_bounded_before_callback(reducer, day, monkeypatch):
    monkeypatch.setattr(reducer_module, "MAX_PENDING_BYTES", 1)
    records = []
    with pytest.raises(DataRejected, match="^output_limit$"):
        reducer.consume(day, io.BytesIO(archive([body(day)])), records.append)
    assert not segments(records)
    assert not reducer._tracks


@pytest.mark.parametrize("kind", [OSError, ValueError, EOFError])
def test_source_failures_have_identity_free_exception_text(reducer, day, kind):
    class Source:
        def read(self, size):
            raise kind(" ".join(SENTINELS))

    records = []
    with pytest.raises(OSError, match="^input_read$") as error:
        reducer.consume(day, Source(), records.append)
    assert_private("".join(traceback.format_exception(error.value)) + json.dumps(records))
    assert not reducer._tracks


@pytest.mark.parametrize("value", ["SENT-DENIED", date.min, date.max, 123, None])
def test_invalid_library_day_is_fixed_code_and_clears_state(reducer, day, value):
    consume(reducer, day, [body(day, rows=[point(86340, callsign="TST123"), point(86370)])])
    with pytest.raises(DataRejected, match="^day_argument$"):
        reducer.consume(value, io.BytesIO(), lambda record: None)
    assert not reducer._tracks


def malformed_gzip():
    # A reserved deflate block type produces zlib.error, rather than BadGzipFile.
    return b"\x1f\x8b\x08\x00" + bytes(6) + b"\x07" + bytes(8)


def compressed_member(day, data):
    info = tarfile.TarInfo(member_name(body(day, identity=SENTINELS[0])))
    info.size = len(data)
    return archive(members=[(info, data)])


def test_deflate_failure_is_sanitised_in_library_and_cli(reducer, day, tmp_path, policy):
    data = compressed_member(day, malformed_gzip())
    with pytest.raises(DataRejected, match="^trace_encoding$"):
        consume(reducer, day, data=data)
    result = run(configs(tmp_path, policy), data)
    assert result.returncode == 65 and not result.stdout
    assert b"Traceback" not in result.stderr
    assert_private(result.stderr.decode())


@pytest.mark.parametrize(
    "name,limit",
    [
        ("heatmap/ignored", "MAX_AUXILIARY"),
        ("README", "MAX_AUXILIARY"),
        ("acas/ignored.csv.gz", "MAX_AUXILIARY"),
        ("traces/ad/trace_full_e7dead.json", "MAX_COMPRESSED"),
    ],
)
def test_oversized_members_reject_at_header_without_reading_body(reducer, day, name, limit):
    info = tarfile.TarInfo(name)
    info.size = getattr(trace, limit) + 1
    with pytest.raises(DataRejected, match="^member_size$"):
        consume(reducer, day, data=info.tobuf())


def test_total_archive_and_trailing_zero_bytes_are_bounded(reducer, day, monkeypatch):
    data = archive([body(day)])
    with pytest.raises(DataRejected, match="^tar_padding$"):
        consume(reducer, day, data=data + bytes(trace.MAX_PADDING))
    monkeypatch.setattr(trace, "MAX_ARCHIVE_BYTES", len(data) - 1)
    with pytest.raises(DataRejected, match="^archive_size$"):
        consume(reducer, day, data=data)


def test_expansion_bomb_is_rejected_before_json_parsing(reducer, day, monkeypatch):
    monkeypatch.setattr(trace, "MAX_JSON", 1024)
    data = compressed_member(day, gzip.compress(b" " * 1_000_000))
    with pytest.raises(DataRejected, match="^json_size$"):
        consume(reducer, day, data=data)


@pytest.mark.parametrize("wide_encoding", [False, True])
def test_deep_json_cannot_bypass_allocation_guard(reducer, day, wide_encoding):
    raw = '{"ignored":' + "[" * 40 + "0" + "]" * 40 + "}"
    data = compressed_member(day, gzip.compress(raw.encode("utf-16" if wide_encoding else "utf-8")))
    with pytest.raises(
        DataRejected, match="^trace_encoding$" if wide_encoding else "^json_complexity$"
    ):
        consume(reducer, day, data=data)


def test_wide_json_is_bounded_before_container_allocation(reducer, day, monkeypatch):
    monkeypatch.setattr(trace, "MAX_JSON_ITEMS", 100)
    value = body(day, identity=SENTINELS[0], ignored=[None] * 101)
    with pytest.raises(DataRejected, match="^json_complexity$"):
        consume(reducer, day, data=compressed_member(day, payload(value)))


@pytest.mark.parametrize(
    "limit,code", [("MAX_TRACKS", "track_limit"), ("MAX_PIECES", "piece_limit")]
)
def test_boundary_state_and_anonymous_groups_are_bounded(reducer, day, monkeypatch, limit, code):
    monkeypatch.setattr(reducer_module, limit, 1)
    if limit == "MAX_TRACKS":
        values = [
            body(day, identity=identity, rows=[point(86340), point(86370)])
            for identity in ("e7dead", "e7beef")
        ]
    else:
        values = [
            body(day),
            body(
                day,
                identity="e7beef",
                rows=[
                    point(0, "ground", lon=80),
                    point(30, "ground", lon=80),
                    point(60, lon=80),
                    point(90, lon=80),
                ],
            ),
        ]
    with pytest.raises(DataRejected, match=f"^{code}$"):
        consume(reducer, day, values)
    assert not reducer._tracks


@pytest.mark.parametrize("version", [{"hex": "e7dead"}, ["PRIVATE-SENT"], "OWNER-SENTINEL\n"])
def test_quality_receipts_reject_free_form_versions(version):
    day = date(2026, 1, 29)
    for operation in (
        lambda: assess(stats(day), history(day), hourly_factor=0.7, rule_version=version),
        lambda: calibrate([], rule_version=version),
    ):
        with pytest.raises(ValueError, match="^invalid quality rule$") as error:
            operation()
        assert_private(str(error.value))


def test_config_size_depth_and_duplicates_have_fixed_cli_errors(tmp_path, policy):
    args = configs(tmp_path, policy)
    operators = tmp_path / "operators.json"
    for data in (b" " * (8 * 1024 * 1024 + 1), b"[" * 2000, b'{"rules":[],"rules":[]}'):
        operators.write_bytes(data)
        result = run(args)
        assert result.returncode == 65 and not result.stdout
        assert b"Traceback" not in result.stderr
        assert_private(result.stderr.decode())
