import csv
import io
import json
import os
import subprocess
import sys

import pytest

from .conftest import archive, body


def configs(tmp_path, policy):
    airports = tmp_path / "airports.json"
    airports.write_text(
        json.dumps(
            {
                "version": "fixture-v1",
                "tz_version": "test",
                "airports": [{"icao": "TEST", "lat": 0, "lon": 0, "elevation_ft": 0, "tz": "UTC"}],
            }
        )
    )
    operators = tmp_path / "operators.json"
    operators.write_text(json.dumps(policy))
    return ["--day", "2026-01-01", "--airports", str(airports), "--operators", str(operators)]


def run(args, data=b""):
    return subprocess.run(
        [sys.executable, "-m", "flights_converter", *args],
        input=data,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("format", ["jsonl", "csv"])
def test_stdin_jsonl_and_csv(tmp_path, policy, day, format):
    result = run(configs(tmp_path, policy) + ["--format", format], archive([body(day)]))
    assert result.returncode == 0 and not result.stderr
    if format == "jsonl":
        records = [json.loads(line) for line in result.stdout.splitlines()]
    else:
        records = [
            json.loads(row["record"]) for row in csv.DictReader(io.StringIO(result.stdout.decode()))
        ]
    assert records[-1]["record_type"] == "manifest_summary"
    assert records[-1]["parse_complete"]
    assert any(r["record_type"] == "eligible_segment" for r in records)


def test_paths_and_atomic_replacement(tmp_path, policy, day):
    data = archive([body(day)])
    a = tmp_path / "v2026.01.01-planes-readsb-prod-0.tar.aa"
    b = tmp_path / "v2026.01.01-planes-readsb-prod-0.tar.ab"
    a.write_bytes(data[:600])
    b.write_bytes(data[600:])
    destination = tmp_path / "output.jsonl"
    destination.write_text("previous")
    args = configs(tmp_path, policy) + ["--input", str(b), str(a), "--output", str(destination)]
    result = run(args)
    assert result.returncode == 0 and not result.stdout
    assert destination.read_text() != "previous"
    assert os.stat(destination).st_mode & 0o777 == 0o600
    destination.write_text("previous")
    b.write_bytes(b.read_bytes()[:-9000])
    result = run(args)
    assert result.returncode == 65 and not result.stdout
    assert destination.read_text() == "previous"
    assert not list(tmp_path.glob(".flights-*"))


def test_failure_discards_all_stdout_and_sanitises_errors(tmp_path, policy, day):
    value = body(day, ownOp="OWNER-SENTINEL")
    result = run(configs(tmp_path, policy), archive([value])[:-9000])
    assert result.returncode == 65 and result.stdout == b""
    assert b"OWNER-SENTINEL" not in result.stderr
    assert b"Traceback" not in result.stderr
    assert str(tmp_path).encode() not in result.stderr


@pytest.mark.parametrize("format", ["jsonl", "csv"])
def test_privacy_sentinels_never_reach_cli_output(tmp_path, policy, day, privacy_cases, format):
    args = configs(tmp_path, policy) + ["--format", format]
    deny = tmp_path / "opt-outs.json"
    deny.write_text('["hex:f00012"]')
    values = [
        body(
            day,
            identity=c["icao"],
            flags=c["dbFlags"],
            r=c["r"],
            callsign=c["flight"],
            ownOp=c["ownOp"],
        )
        for c in privacy_cases
    ]
    result = run(args + ["--opt-outs", str(deny)], archive(values))
    assert result.returncode == 0
    rendered = (result.stdout + result.stderr).decode()
    for case in privacy_cases:
        for field in ("icao", "r", "flight", "ownOp"):
            assert case[field] not in rendered


def test_io_failure_has_no_path_in_error(tmp_path, policy):
    args = configs(tmp_path, policy)
    missing = tmp_path / "v2026.01.01-planes-readsb-prod-0.tar.aa"
    result = run(args + ["--input", str(missing)])
    assert result.returncode == 74
    assert str(tmp_path).encode() not in result.stderr
