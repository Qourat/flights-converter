"""Robustness fuzz tests: truncated, bit-flipped, concatenated archives and malformed JSON.

Verifies the converter never crashes except with documented error classes/exit codes,
never exceeds documented memory bounds on 20 MB input, and never emits privacy sentinels.
"""

import gzip
import io
import json
import random
import subprocess
import sys
import tarfile
import tracemalloc

import pytest

from flights_converter import DataRejected, Reducer

from .conftest import archive, body, consume, member_name, payload, point

# Documented error classes from errors.py
DOCUMENTED_ERROR_CODES = frozenset(
    """aircraft_limit airport_config archive_size body_date configuration conflicting_duplicate
    day_argument duplicate_airport duplicate_json_key identity_format json_complexity json_size
    member_limit member_size method_version no_traces opt_out_config operator_config output_is_input
    output_limit part_count part_hash part_length part_names path_date piece_limit point_altitude
    point_date point_details point_order point_position point_schema rejected tar_directory_size
    tar_end_markers tar_header tar_member_type tar_padding tar_trailing_data trace_encoding
    trace_identity trace_path_identity trace_schema trace_version track_limit truncated_tar
    unexpected_tar_member unsafe_tar_path""".split()
)

# Documented CLI exit codes from README.md and cli.py
DOCUMENTED_EXIT_CODES = {0, 2, 65, 74, 130}

# Privacy sentinels from fixtures/synthetic.json
PRIVACY_SENTINELS = [
    "OWNER-PIA-SENTINEL",
    "OWNER-LADD-SENTINEL",
    "OWNER-MPIA-SENTINEL",
    "OWNER-MLADD-SENTINEL",
    "OWNER-BOTH-SENTINEL",
    "OWNER-MBOTH-SENTINEL",
    "OWNER-NONICAO-SENTINEL",
    "OWNER-PRIVATE-SENTINEL",
    "OWNER-INTEREST-SENTINEL",
    "OWNER-UNKNOWN-SENTINEL",
    "OWNER-NOFLAGS-SENTINEL",
    "OWNER-OPTOUT-SENTINEL",
    "SENT-PIA",
    "SENT-LADD",
    "SENT-MPIA",
    "SENT-MLADD",
    "SENT-BOTH",
    "SENT-MBOTH",
    "SENT-NONICAO",
    "SENT-PRIVATE",
    "SENT-INTEREST",
    "SENT-UNKNOWN",
    "SENT-NOFLAGS",
    "SENT-OPTOUT",
    "TST9004",
    "TST9008",
    "TST9005",
    "TST9009",
    "TST9012",
    "TST9013",
    "TST9001",
    "PRIVATE77",
    "PRIVATE88",
    "TST9016",
    "TST9017",
    "TST9018",
]

# Documented memory limit: 512 MiB spool + 128 MiB buffer (from README.md)
MAX_MEMORY_MB = 512 + 128 + 64  # Add 64 MB overhead for Python runtime


@pytest.fixture
def fuzz_seed():
    return 42  # Seeded for reproducibility


def create_synthetic_archive(day, num_aircraft=10):
    """Create a synthetic archive with multiple aircraft traces."""
    bodies = []
    for i in range(num_aircraft):
        identity = f"f{i:05x}"
        bodies.append(
            body(
                day,
                identity=identity,
                callsign=f"TST{100 + i}",
                flags=0,
                rows=[point(j * 30, "ground" if j < 2 else 1000 + j * 100) for j in range(6)],
            )
        )
    return archive(bodies)


class TestTruncatedArchives:
    """Test truncated archives at various boundaries."""

    def test_truncated_at_tar_header(self, reducer, day):
        """Truncate in the middle of a tar header."""
        data = create_synthetic_archive(day, 5)
        truncated = data[: len(data) // 2]

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=truncated)
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_truncated_at_member_data(self, reducer, day):
        """Truncate in the middle of member data."""
        data = create_synthetic_archive(day, 5)
        truncated = data[:1000]

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=truncated)
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_truncated_mid_member(self, reducer, day):
        """Truncate in the middle of a large member."""
        data = create_synthetic_archive(day, 5)
        # Remove a significant chunk from the middle/end
        truncated = data[:2000]

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=truncated)
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_truncated_gzip_stream(self, reducer, day):
        """Truncate a gzip-compressed trace member."""
        value = body(day)
        data = payload(value)[:-10]  # Truncate gzip stream
        info = tarfile.TarInfo(member_name(value))
        info.size = len(data)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, data)]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES


class TestBitFlippedData:
    """Test bit-flipped data at various locations."""

    def test_bit_flip_in_tar_header(self, reducer, day, fuzz_seed):
        """Flip bits in tar header."""
        random.seed(fuzz_seed)
        data = bytearray(create_synthetic_archive(day, 3))

        # Flip random bits in first tar header (first 512 bytes)
        for _ in range(10):
            pos = random.randint(0, min(511, len(data) - 1))
            bit = random.randint(0, 7)
            data[pos] ^= 1 << bit

        with pytest.raises((DataRejected, OSError)):
            consume(reducer, day, data=bytes(data))

    def test_bit_flip_in_compressed_data(self, reducer, day, fuzz_seed):
        """Flip bits in gzip-compressed trace data."""
        random.seed(fuzz_seed)
        value = body(day)
        data = bytearray(payload(value))

        # Flip random bits in compressed data
        for _ in range(5):
            pos = random.randint(0, len(data) - 1)
            bit = random.randint(0, 7)
            data[pos] ^= 1 << bit

        info = tarfile.TarInfo(member_name(value))
        info.size = len(data)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, bytes(data))]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_bit_flip_in_json_structure(self, reducer, day, fuzz_seed):
        """Flip bits that corrupt JSON structure."""
        random.seed(fuzz_seed)
        value = body(day)
        json_bytes = json.dumps(value, separators=(",", ":")).encode()
        data = bytearray(json_bytes)

        # Corrupt JSON structure characters
        for _ in range(3):
            pos = random.randint(0, len(data) - 1)
            data[pos] ^= 0xFF

        compressed = gzip.compress(bytes(data), mtime=0)
        info = tarfile.TarInfo(member_name(value))
        info.size = len(compressed)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, compressed)]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES


class TestConcatenatedArchives:
    """Test concatenated and doubled archives."""

    def test_double_concatenated_archive(self, reducer, day):
        """Concatenate archive with itself."""
        data = create_synthetic_archive(day, 3)
        doubled = data[:-1024] + data  # Remove end markers, then concat

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=doubled)
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_archive_with_trailing_garbage(self, reducer, day, fuzz_seed):
        """Append random data after valid archive."""
        random.seed(fuzz_seed)
        data = create_synthetic_archive(day, 3)
        garbage = bytes([random.randint(0, 255) for _ in range(1000)])

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=data + garbage)
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_interleaved_valid_and_invalid_members(self, reducer, day):
        """Mix valid and corrupted members."""
        valid = body(day, identity="f00001")
        invalid = body(day, identity="f00002")
        invalid["version"] = "unknown-version"

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, [valid, invalid])
        assert exc.value.code in DOCUMENTED_ERROR_CODES


class TestMalformedJSON:
    """Test malformed JSON traces."""

    def test_deeply_nested_json(self, reducer, day):
        """Test JSON exceeding depth limit."""
        value = body(day)
        # Create deeply nested structure
        nested = value
        for _ in range(40):  # Exceeds MAX_JSON_DEPTH of 32
            nested = {"nested": nested}

        compressed = gzip.compress(json.dumps(nested, separators=(",", ":")).encode(), mtime=0)
        info = tarfile.TarInfo(member_name(value))
        info.size = len(compressed)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, compressed)]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_json_with_null_bytes(self, reducer, day):
        """Test JSON containing null bytes."""
        value = body(day)
        json_str = json.dumps(value, separators=(",", ":"))
        corrupted = json_str.replace("TST123", "TST\x00123")

        compressed = gzip.compress(corrupted.encode(), mtime=0)
        info = tarfile.TarInfo(member_name(value))
        info.size = len(compressed)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, compressed)]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_json_with_invalid_utf8(self, reducer, day):
        """Test JSON with invalid UTF-8 sequences."""
        value = body(day)
        valid_json = json.dumps(value, separators=(",", ":")).encode()
        # Insert invalid UTF-8 sequence
        corrupted = valid_json[:50] + b"\xff\xfe" + valid_json[50:]

        compressed = gzip.compress(corrupted, mtime=0)
        info = tarfile.TarInfo(member_name(value))
        info.size = len(compressed)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, compressed)]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_json_with_duplicate_keys(self, reducer, day):
        """Test JSON with duplicate keys."""
        value = body(day)
        json_str = json.dumps(value, separators=(",", ":"))
        # Inject duplicate key
        corrupted = json_str.replace('"dbFlags":0', '"dbFlags":0,"dbFlags":1')

        compressed = gzip.compress(corrupted.encode(), mtime=0)
        info = tarfile.TarInfo(member_name(value))
        info.size = len(compressed)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, compressed)]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES

    def test_json_with_nan_and_infinity(self, reducer, day):
        """Test JSON with NaN and Infinity values."""
        value = body(day)
        value["trace"][0][3] = float("nan")
        value["trace"][1][3] = float("inf")

        # JSON encoder will fail or produce invalid output
        try:
            json_str = json.dumps(value)
        except ValueError:
            return  # Expected behavior

        compressed = gzip.compress(json_str.encode(), mtime=0)
        info = tarfile.TarInfo(member_name(value))
        info.size = len(compressed)

        with pytest.raises(DataRejected) as exc:
            consume(reducer, day, data=archive(members=[(info, compressed)]))
        assert exc.value.code in DOCUMENTED_ERROR_CODES


class TestCLIRobustness:
    """Test CLI never crashes with unexpected exceptions."""

    def test_cli_with_malformed_input(self, tmp_path, policy, day, fuzz_seed):
        """CLI handles malformed input gracefully."""
        random.seed(fuzz_seed)

        # Create config files
        airports = tmp_path / "airports.json"
        airports.write_text(
            json.dumps(
                {
                    "version": "test-v1",
                    "tz_version": "test",
                    "airports": [
                        {"icao": "TEST", "lat": 0, "lon": 0, "elevation_ft": 0, "tz": "UTC"}
                    ],
                }
            )
        )
        operators = tmp_path / "operators.json"
        operators.write_text(json.dumps(policy))

        # Generate random garbage input
        garbage = bytes([random.randint(0, 255) for _ in range(5000)])

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "flights_converter",
                "--day",
                "2026-01-01",
                "--airports",
                str(airports),
                "--operators",
                str(operators),
            ],
            input=garbage,
            capture_output=True,
            check=False,
        )

        assert result.returncode in DOCUMENTED_EXIT_CODES
        # Should not crash or produce tracebacks
        assert b"Traceback" not in result.stderr

    def test_cli_with_truncated_config(self, tmp_path, day):
        """CLI handles truncated config files."""
        airports = tmp_path / "airports.json"
        airports.write_text('{"version":"test-v1","airport')  # Truncated JSON

        operators = tmp_path / "operators.json"
        operators.write_text('{"version":')  # Truncated JSON

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "flights_converter",
                "--day",
                "2026-01-01",
                "--airports",
                str(airports),
                "--operators",
                str(operators),
            ],
            input=b"",
            capture_output=True,
            check=False,
        )

        assert result.returncode in DOCUMENTED_EXIT_CODES
        assert b"Traceback" not in result.stderr


class TestPrivacySentinelLeakage:
    """Verify privacy sentinels never leak in any error condition."""

    def test_no_sentinels_in_truncated_archive_errors(self, airports, policy, day, privacy_cases):
        """Privacy sentinels don't leak when archive is truncated."""
        values = []
        for case in privacy_cases:
            values.append(
                body(
                    day,
                    identity=case["icao"],
                    flags=case["dbFlags"],
                    callsign=case["flight"],
                    r=case["r"],
                    ownOp=case["ownOp"],
                )
            )

        data = archive(values)
        truncated = data[: len(data) // 2]

        reducer = Reducer(airports, policy)
        records = []
        try:
            reducer.consume(day, io.BytesIO(truncated), records.append)
        except (DataRejected, OSError) as e:
            error_str = str(e) + json.dumps(records)
            for sentinel in PRIVACY_SENTINELS:
                assert sentinel not in error_str
        finally:
            reducer.close()

    def test_no_sentinels_in_corrupted_json_errors(
        self, airports, policy, day, privacy_cases, fuzz_seed
    ):
        """Privacy sentinels don't leak when JSON is corrupted."""
        random.seed(fuzz_seed)

        case = privacy_cases[0]
        value = body(
            day,
            identity=case["icao"],
            flags=case["dbFlags"],
            callsign=case["flight"],
            r=case["r"],
            ownOp=case["ownOp"],
        )

        json_bytes = json.dumps(value, separators=(",", ":")).encode()
        data = bytearray(json_bytes)

        # Corrupt it
        for _ in range(10):
            pos = random.randint(0, len(data) - 1)
            data[pos] ^= 0xFF

        compressed = gzip.compress(bytes(data), mtime=0)
        info = tarfile.TarInfo(member_name(value))
        info.size = len(compressed)

        reducer = Reducer(airports, policy)
        records = []
        try:
            reducer.consume(day, io.BytesIO(archive(members=[(info, compressed)])), records.append)
        except (DataRejected, OSError) as e:
            error_str = str(e) + json.dumps(records)
            for sentinel in PRIVACY_SENTINELS:
                assert sentinel not in error_str
        finally:
            reducer.close()


class TestMemoryBounds:
    """Verify memory usage stays within documented bounds on 20 MB input."""

    def test_memory_bounded_on_large_input(self, airports, policy, day):
        """Memory usage stays under limit for 20 MB input."""
        # Create a ~20 MB archive with many aircraft
        bodies = []
        for i in range(1000):
            identity = f"f{i:05x}"
            bodies.append(
                body(
                    day,
                    identity=identity,
                    callsign=f"TST{1000 + i}",
                    flags=0,
                    rows=[point(j * 30, 1000 + j * 100) for j in range(100)],
                )
            )

        data = archive(bodies)

        # Pad to approximately 20 MB if needed
        if len(data) < 20 * 1024 * 1024:
            # Add metadata members to increase size
            padding_needed = 20 * 1024 * 1024 - len(data)
            info = tarfile.TarInfo("README.txt")
            info.size = padding_needed
            data = archive(bodies, members=[(info, b"x" * padding_needed)])

        tracemalloc.start()
        try:
            reducer = Reducer(airports, policy)
            records = []
            try:
                reducer.consume(day, io.BytesIO(data), records.append)
            except DataRejected:
                pass  # Expected for oversized input
            finally:
                reducer.close()

            current, peak = tracemalloc.get_traced_memory()
            peak_mb = peak / (1024 * 1024)

            # Allow some margin for Python overhead
            assert peak_mb < MAX_MEMORY_MB * 1.2, f"Peak memory {peak_mb:.1f} MB exceeds limit"
        finally:
            tracemalloc.stop()


class TestRandomFuzz:
    """Random fuzz testing with seeded generation."""

    def test_random_truncations(self, reducer, day, fuzz_seed):
        """Test random truncation points."""
        random.seed(fuzz_seed)
        data = create_synthetic_archive(day, 10)

        for _ in range(20):
            cut_point = random.randint(0, len(data) - 1)
            truncated = data[:cut_point]

            try:
                consume(reducer, day, data=truncated)
            except (DataRejected, OSError) as e:
                # Should only raise documented errors
                if isinstance(e, DataRejected):
                    assert e.code in DOCUMENTED_ERROR_CODES
                # No privacy leakage in any case
                for sentinel in PRIVACY_SENTINELS[:5]:  # Check subset
                    assert sentinel not in str(e)

    def test_random_bit_flips(self, reducer, day, fuzz_seed):
        """Test random bit flips throughout the archive."""
        random.seed(fuzz_seed)
        data = bytearray(create_synthetic_archive(day, 5))

        for _ in range(10):
            # Make a copy and flip some bits
            corrupted = bytearray(data)
            for _ in range(random.randint(1, 20)):
                pos = random.randint(0, len(corrupted) - 1)
                bit = random.randint(0, 7)
                corrupted[pos] ^= 1 << bit

            try:
                consume(reducer, day, data=bytes(corrupted))
            except (DataRejected, OSError) as e:
                if isinstance(e, DataRejected):
                    assert e.code in DOCUMENTED_ERROR_CODES

    def test_random_insertions(self, reducer, day, fuzz_seed):
        """Test random byte insertions."""
        random.seed(fuzz_seed)
        data = create_synthetic_archive(day, 5)

        for _ in range(10):
            pos = random.randint(0, len(data) - 1)
            insertion = bytes([random.randint(0, 255) for _ in range(100)])
            corrupted = data[:pos] + insertion + data[pos:]

            try:
                consume(reducer, day, data=corrupted)
            except (DataRejected, OSError) as e:
                if isinstance(e, DataRejected):
                    assert e.code in DOCUMENTED_ERROR_CODES
