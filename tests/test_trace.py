import hashlib
import io
import json
import tarfile

import pytest

from flights_converter import DataRejected, JoinedParts
from flights_converter.trace import ordered_part_paths

from .conftest import archive, body, consume, member_name, payload, point


class NonSeekable:
    def __init__(self, data):
        self.stream = io.BytesIO(data)

    def read(self, size):
        assert 0 <= size <= 65536
        return self.stream.read(min(size, 37))


@pytest.mark.parametrize("parts", [2, 3])
def test_split_inside_headers_and_members(reducer, day, parts):
    data = archive([body(day)])
    cuts = [0, 517, len(data)] if parts == 2 else [0, 511, 625, len(data)]
    chunks = [data[left:right] for left, right in zip(cuts, cuts[1:], strict=False)]
    expected = [(len(c), hashlib.sha256(c).hexdigest()) for c in chunks]
    records = []
    summary = reducer.consume(
        day, JoinedParts([NonSeekable(c) for c in chunks], expected), records.append
    )
    assert summary["input_bytes"] == len(data)
    assert "input_sha" not in summary
    assert summary["parse_complete"]


@pytest.mark.parametrize("remove", [1, 512, 1024, 8704, 9216])
def test_truncated_tar_rejected(reducer, day, remove):
    data = archive([body(day)])
    if remove <= 1024:
        # A tar with exactly the two required final blocks, then truncate those.
        data = data[:1024] + bytes(1024)
    with pytest.raises(DataRejected):
        consume(reducer, day, data=data[:-remove])
    assert reducer._last_day is None


def test_checksum_and_trailing_data(reducer, day):
    data = archive([body(day)])
    damaged = bytearray(data)
    damaged[0] ^= 1
    for bad in (bytes(damaged), data + b"secret-sentinel", data[:1024] + bytes(512) + b"x" * 512):
        with pytest.raises(DataRejected):
            consume(reducer, day, data=bad)


@pytest.mark.parametrize("kind", ["crc", "truncated", "plain"])
def test_gzip_integrity(reducer, day, kind):
    value = body(day)
    data = payload(value)
    data = data[:-8] + bytes(8) if kind == "crc" else data[:-5] if kind == "truncated" else b"{}"
    info = tarfile.TarInfo(member_name(value))
    info.size = len(data)
    with pytest.raises(DataRejected, match="trace_encoding"):
        consume(reducer, day, data=archive(members=[(info, data)]))


@pytest.mark.parametrize(
    "name,kind",
    [
        ("/secret-sentinel", tarfile.REGTYPE),
        ("../secret-sentinel", tarfile.REGTYPE),
        ("traces/link", tarfile.SYMTYPE),
        ("traces/link", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
        ("sparse", tarfile.GNUTYPE_SPARSE),
        ("contiguous", tarfile.CONTTYPE),
        ("unknown", tarfile.REGTYPE),
    ],
)
def test_unsafe_members_never_report_names(reducer, day, name, kind):
    info = tarfile.TarInfo(name)
    info.type = kind
    records = []
    with pytest.raises(DataRejected) as error:
        reducer.consume(day, io.BytesIO(archive(members=[(info, b"")])), records.append)
    assert "secret-sentinel" not in str(error.value) + json.dumps(records)


def test_duplicates_identical_only(reducer, day):
    value = body(day)
    _, summary = consume(reducer, day, [value, value])
    assert summary["duplicate_members"] == 1
    assert summary["n_aircraft"] == 1
    assert summary["hour_aircraft"][0] == 1
    with pytest.raises(DataRejected, match="conflicting_duplicate"):
        consume(reducer, day, [value, {**value, "r": "CHANGED"}])


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda v: v.update(timestamp=v["timestamp"] + 86400), "body_date"),
        (lambda v: v["trace"].append(point(86400)), "point_date"),
        (lambda v: v.update(version="readsb 99.0.0 abcdef0"), "trace_version"),
        (lambda v: v.update(version=2), "trace_version"),
        (lambda v: v.update(icao="f0ffff"), "trace_identity"),
        (lambda v: v["trace"][0].append(1) or v["trace"][0].append(2), "point_schema"),
        (lambda v: v["trace"][0].__setitem__(6, 16), "point_schema"),
        (lambda v: v["trace"][0].__setitem__(1, float("nan")), "point_position"),
        (lambda v: v["trace"][0].__setitem__(2, 181), "point_position"),
        (lambda v: v["trace"][1].__setitem__(0, 0), "point_order"),
    ],
)
def test_malformed_trace(reducer, day, mutation, code):
    value = body(day)
    original_name = member_name(value)
    mutation(value)
    info = tarfile.TarInfo(original_name)
    data = payload(value)
    info.size = len(data)
    records = []
    with pytest.raises(DataRejected, match=code):
        reducer.consume(day, io.BytesIO(archive(members=[(info, data)])), records.append)
    summary = records[-1]
    assert not summary["parse_complete"]
    assert summary["date_errors"] == int(code.endswith("date"))


def test_metadata_and_heatmap_are_streamed_away(reducer, day):
    data = b"SENTINEL" * 100000
    info = tarfile.TarInfo("heatmap/00.bin.ttf")
    info.size = len(data)
    records, summary = consume(reducer, day, data=archive([body(day)], members=[(info, data)]))
    assert summary["n_members"] == 2
    assert "SENTINEL" not in json.dumps(records)


def test_part_names(day, tmp_path):
    prefix = "v2026.01.01-planes-readsb-prod-0.tar."
    a, b, c = [tmp_path / (prefix + suffix) for suffix in ("aa", "ab", "ac")]
    assert ordered_part_paths([b, a], day) == [a, b]
    assert ordered_part_paths([c, b, a], day) == [a, b, c]
    for paths in ([a, c], [b], [a, a], [tmp_path / prefix.replace("prod", "staging")]):
        with pytest.raises(DataRejected):
            ordered_part_paths(paths, day)


@pytest.mark.parametrize(
    "spec,code", [([(1, None)], "part_length"), ([(2, "f" * 64)], "part_hash"), ([], "part_count")]
)
def test_advertised_lengths_and_hashes(spec, code):
    source = JoinedParts([io.BytesIO(b"ab")], spec)
    with pytest.raises(DataRejected, match=code):
        while source.read(100):
            pass


def test_path_date_is_not_a_substitute_for_body_date(reducer, day):
    value = body(day, timestamp=body(day)["timestamp"] + 86400)
    data = payload(value)
    info = tarfile.TarInfo(f"2026/01/01/{member_name(value)}")
    info.size = len(data)
    with pytest.raises(DataRejected, match="body_date"):
        consume(reducer, day, data=archive(members=[(info, data)]))


def test_wrong_path_date_rejected_even_with_correct_body(reducer, day):
    value = body(day)
    data = payload(value)
    info = tarfile.TarInfo(f"2026/01/02/{member_name(value)}")
    info.size = len(data)
    with pytest.raises(DataRejected, match="path_date"):
        consume(reducer, day, data=archive(members=[(info, data)]))


def test_duplicate_json_keys_rejected(reducer, day):
    import gzip

    value = body(day)
    data = gzip.compress(
        json.dumps(value).replace('"dbFlags": 0', '"dbFlags": 0,"dbFlags": 4').encode()
    )
    info = tarfile.TarInfo(member_name(value))
    info.size = len(data)
    with pytest.raises(DataRejected, match="duplicate_json_key"):
        consume(reducer, day, data=archive(members=[(info, data)]))
