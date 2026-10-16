"""Synthetic ACAS releases preserve trace output and strict archive validation."""

import csv
import gzip
import io
import json
import tarfile

import pytest

from flights_converter import DataRejected

from .conftest import archive, body, consume, member_name, payload
from .test_cli import configs, run
from .test_trace import NonSeekable


def member(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


@pytest.mark.parametrize("format", ["jsonl", "csv"])
@pytest.mark.parametrize("prefix", ["", "2026/01/01/"])
def test_interleaved_acas_preserves_trace_output_bytes(tmp_path, policy, day, format, prefix):
    values = [
        body(day, identity="f00001"),
        body(day, identity="e7dead", flags=4, r="PRIVATE-REG"),
        body(day, identity="f00003", callsign="PRIVATE-CALL"),
    ]
    traces = [member(prefix + member_name(value), payload(value)) for value in values]
    advisories = [
        gzip.compress(b"ACAS-CONTENT-SENTINEL\n" * 10000, mtime=0),
        b"\xffINVALID-GZIP-SENTINEL" * 4000,  # Skipping must not try to decode gzip/CSV.
    ]
    baseline = archive(members=traces)
    mixed = archive(
        members=[
            traces[0],
            member(prefix + "acas/ACAS-NAME-SENTINEL.csv.gz", advisories[0]),
            traces[1],
            member(prefix + "acas/OTHER-NAME-SENTINEL.csv.gz", advisories[1]),
            traces[2],
        ]
    )
    args = configs(tmp_path, policy) + ["--format", format]
    original, result = run(args, baseline), run(args, mixed)
    assert original.returncode == result.returncode == 0
    assert original.stderr == result.stderr == b""
    # Every output byte except the final aggregate summary must be identical.
    assert (
        result.stdout.splitlines(keepends=True)[:-1]
        == original.stdout.splitlines(keepends=True)[:-1]
    )
    summaries = []
    for output in (original.stdout, result.stdout):
        if format == "jsonl":
            records = [json.loads(line) for line in output.splitlines()]
        else:
            records = [
                json.loads(row["record"]) for row in csv.DictReader(io.StringIO(output.decode()))
            ]
        assert any(record["record_type"] == "eligible_segment" for record in records)
        assert any(record["record_type"] == "airport_piece" for record in records)
        summaries.append(records[-1])
    before, after = summaries
    assert before["skipped_members"] == before["skipped_bytes"] == 0
    assert after["skipped_members"] == 2
    assert after["skipped_bytes"] == sum(map(len, advisories))
    assert before["n_members"] == 3 and after["n_members"] == 5
    assert before["input_bytes"] == len(baseline) and after["input_bytes"] == len(mixed)
    archive_counts = {"input_bytes", "n_members", "skipped_members", "skipped_bytes"}
    assert {k: v for k, v in after.items() if k not in archive_counts} == {
        k: v for k, v in before.items() if k not in archive_counts
    }
    for sentinel in (
        b"ACAS-NAME-SENTINEL",
        b"OTHER-NAME-SENTINEL",
        b"ACAS-CONTENT-SENTINEL",
        b"INVALID-GZIP-SENTINEL",
        b"e7dead",
        b"PRIVATE-REG",
        b"PRIVATE-CALL",
    ):
        assert sentinel not in result.stdout + result.stderr


def test_acas_discard_uses_bounded_non_seekable_reads(reducer, day):
    data = b"ADVISORY-CONTENT-SENTINEL" * 10000
    value = body(day)
    source = NonSeekable(
        archive(
            members=[
                member("acas/advisory.csv.gz", data),
                member(member_name(value), payload(value)),
            ]
        )
    )
    records = []
    summary = reducer.consume(day, source, records.append)
    assert summary["parse_complete"]
    assert summary["skipped_members"] == 1 and summary["skipped_bytes"] == len(data)
    assert "ADVISORY-CONTENT-SENTINEL" not in json.dumps(records)


@pytest.mark.parametrize(
    "name",
    [
        "acas/advisory.csv",
        "acas/advisory.json.gz",
        "acas/nested/advisory.csv.gz",
        "acas/advisory.csv.gz.extra",
        "acas/advisory.csv.gz\n",
        "notacas/advisory.csv.gz",
        "unknown/advisory.csv.gz",
        "heatmap\n",
    ],
)
def test_unknown_families_still_reject_after_acas(reducer, day, name):
    value = body(day)
    data = archive(
        members=[
            member(member_name(value), payload(value)),
            member("acas/known.csv.gz", b"SKIPPED-CONTENT-SENTINEL"),
            member(name, b"UNKNOWN-CONTENT-SENTINEL"),
        ]
    )
    records = []
    with pytest.raises(DataRejected, match="^unexpected_tar_member$"):
        reducer.consume(day, io.BytesIO(data), records.append)
    assert not any(record["record_type"] == "eligible_segment" for record in records)
    summary = records[-1]
    assert not summary["parse_complete"] and summary["parse_errors"] == 1
    assert summary["skipped_members"] == 1
    assert summary["skipped_bytes"] == len(b"SKIPPED-CONTENT-SENTINEL")
    rendered = json.dumps(records)
    assert "SKIPPED-CONTENT-SENTINEL" not in rendered
    assert "UNKNOWN-CONTENT-SENTINEL" not in rendered


def test_unknown_member_after_acas_discards_cli_output(tmp_path, policy, day):
    data = archive(
        [body(day)],
        members=[
            member("acas/known.csv.gz", b"SKIPPED-CONTENT-SENTINEL"),
            member("acas/UNKNOWN-NAME-SENTINEL.json.gz", b"UNKNOWN-CONTENT-SENTINEL"),
        ],
    )
    result = run(configs(tmp_path, policy), data)
    assert result.returncode == 65 and result.stdout == b""
    assert json.loads(result.stderr) == {
        "record_type": "diagnostics",
        "code": "unexpected_tar_member",
        "discard_generation": True,
    }


def test_truncated_acas_is_not_counted_as_fully_skipped(reducer, day):
    info = tarfile.TarInfo("acas/PRIVATE-NAME-SENTINEL.csv.gz")
    info.size = 1000
    records = []
    with pytest.raises(DataRejected, match="^truncated_tar$"):
        reducer.consume(day, io.BytesIO(info.tobuf() + b"PRIVATE-CONTENT-SENTINEL"), records.append)
    summary = records[-1]
    assert not summary["parse_complete"] and summary["parse_errors"] == 1
    assert summary["skipped_members"] == summary["skipped_bytes"] == 0
    assert "PRIVATE-NAME-SENTINEL" not in json.dumps(records)
    assert "PRIVATE-CONTENT-SENTINEL" not in json.dumps(records)


def test_acas_without_traces_still_rejects(reducer, day):
    data = archive(members=[member("acas/empty.csv.gz", b"")])
    with pytest.raises(DataRejected, match="^no_traces$"):
        consume(reducer, day, data=data)
