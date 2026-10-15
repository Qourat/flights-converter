"""Strict, bounded archive reading. No extraction, network, or raw spool."""

import gzip
import hashlib
import io
import json
import math
import re
import tarfile
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePosixPath

from .errors import DataRejected

CHUNK = 64 * 1024
MAX_COMPRESSED = 16 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_ITEMS = 1_000_000
MAX_AUXILIARY = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_PADDING = 1024 * 1024
MAX_MEMBERS = 1_000_000
MAX_AIRCRAFT = 500_000
TRACE_NAME = re.compile(r"(?:^|/)traces/([0-9a-f]{2})/trace_full_(~?[0-9a-f]{6})\.json(?:\.gz)?$")
METADATA = {"README", "README.txt", "README.md", "LICENSE", "LICENSE-ODbL.txt", "LICENSE-CC0.txt"}


def number(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class BoundedReader:
    def __init__(self, stream):
        self.stream = stream
        self.n_bytes = 0

    def read(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("bounded reads required")
        try:
            data = self.stream.read(min(size, CHUNK))
        except DataRejected:
            raise
        except (OSError, ValueError, EOFError):
            # Source exceptions can contain filenames or response bodies.
            raise OSError("input_read") from None
        self.n_bytes += len(data)
        if self.n_bytes > MAX_ARCHIVE_BYTES:
            raise DataRejected("archive_size")
        return data

    def exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            part = self.read(min(CHUNK, size - len(data)))
            if not part:
                raise DataRejected("truncated_tar")
            data.extend(part)
        return bytes(data)

    def discard(self, size: int):
        while size:
            part = self.exact(min(size, CHUNK))
            size -= len(part)


class JoinedParts:
    """Non-seekable concatenation; optional advertised lengths and SHA256 digests.

    Streams belong to the caller. All metadata must be verified by EOF, including
    padding after the tar end markers. ``expected`` entries are (size, sha256).
    """

    def __init__(self, streams, expected=None):
        self.streams = iter(streams)
        self.expected = iter(expected) if expected is not None else None
        self.current = None
        self.digest = None
        self.received = 0
        self.spec = None
        self.done = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("bounded reads required")
        if size == 0:
            return b""
        while not self.done:
            if self.current is None:
                self.current = next(self.streams, None)
                if self.current is None:
                    self.done = True
                    if self.expected is not None and next(self.expected, None) is not None:
                        raise DataRejected("part_count")
                    break
                self.spec = next(self.expected, None) if self.expected is not None else None
                if self.expected is not None and self.spec is None:
                    raise DataRejected("part_count")
                self.digest = hashlib.sha256()
                self.received = 0
            data = self.current.read(min(size, CHUNK))
            if data:
                self.received += len(data)
                self.digest.update(data)
                if self.spec is not None and self.received > self.spec[0]:
                    raise DataRejected("part_length")
                return data
            if self.spec is not None:
                length, digest = self.spec
                if self.received != length:
                    raise DataRejected("part_length")
                if digest is not None and self.digest.hexdigest() != digest.removeprefix("sha256:"):
                    raise DataRejected("part_hash")
            self.current = None
        return b""


def ordered_part_paths(paths, day: date):
    """Require the exact prod-0 tag and contiguous aa, ab, ... part names."""
    prefix = f"v{day:%Y.%m.%d}-planes-readsb-prod-0.tar"
    by_suffix = {}
    for path in paths:
        name = path.name
        if name == prefix and len(paths) == 1:
            return list(paths)
        match = re.fullmatch(re.escape(prefix) + r"\.([a-z]{2})", name)
        if not match or match[1] in by_suffix:
            raise DataRejected("part_names")
        by_suffix[match[1]] = path
    suffixes = sorted(by_suffix)
    expected = [chr(97 + i // 26) + chr(97 + i % 26) for i in range(len(suffixes))]
    if not suffixes or suffixes != expected:
        raise DataRejected("part_names")
    return [by_suffix[s] for s in suffixes]


def safe_name(name: str, day: date) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name:
        raise DataRejected("unsafe_tar_path")
    name = str(path)
    match = re.search(r"(?:^|/)(\d{4})/(\d{2})/(\d{2})(?:/|$)", name)
    if match and "/".join(match.groups()) != f"{day:%Y/%m/%d}":
        raise DataRejected("path_date", date_error=True)
    return name


def archive_members(reader: BoundedReader, day: date, stats: dict):
    """Yield one compressed trace at a time; consume all padding through EOF."""
    while True:
        block = reader.exact(512)
        if block == bytes(512):
            if reader.exact(512) != bytes(512):
                raise DataRejected("tar_end_markers")
            trailing = 0
            while part := reader.read(CHUNK):
                trailing += len(part)
                if any(part):
                    raise DataRejected("tar_trailing_data")
                if trailing > MAX_PADDING:
                    raise DataRejected("tar_padding")
            if trailing % 512:
                raise DataRejected("tar_padding")
            return
        try:
            info = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
        except (tarfile.TarError, UnicodeError, ValueError):
            raise DataRejected("tar_header") from None
        stats["n_members"] += 1
        if stats["n_members"] > MAX_MEMBERS:
            raise DataRejected("member_limit")
        name = safe_name(info.name, day)
        if info.isdir():
            if info.size:
                raise DataRejected("tar_directory_size")
            continue
        if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or info.size < 0:
            raise DataRejected("tar_member_type")
        match = TRACE_NAME.search(name)
        if match:
            if match[1] != match[2][-2:]:
                raise DataRejected("trace_path_identity")
            if info.size > MAX_COMPRESSED:
                raise DataRejected("member_size")
            payload = reader.exact(info.size)
            reader.discard(-info.size % 512)
            yield match[2], payload
        elif "heatmap" in PurePosixPath(name).parts or PurePosixPath(name).name in METADATA:
            if info.size > MAX_AUXILIARY:
                raise DataRejected("member_size")
            reader.discard(info.size + (-info.size % 512))
        else:
            raise DataRejected("unexpected_tar_member")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataRejected("duplicate_json_key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True, repr=False)
class Point:
    ts: float
    lat: float | None
    lon: float | None
    altitude: float | None
    ground: bool
    flags: int
    ground_altitude: float | None = None

    @property
    def position(self):
        return [self.lat, self.lon] if self.lat is not None and self.lon is not None else None


@dataclass(slots=True, repr=False)
class Trace:
    icao: str
    reg: str | None
    aircraft_type: str | None
    flags: object
    callsigns: set[str]
    points: list[Point]


def identifier(value, pattern):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str) or not re.fullmatch(pattern, value.strip().upper()):
        raise DataRejected("identity_format")
    return value.strip().upper()


def check_json_limits(raw):
    """Bound container allocation before json.loads builds Python objects.

    Count structural delimiters outside strings. Every container and additional
    value needs one; quoted/escaped punctuation cannot bypass the limit.
    Syntax validation remains the JSON decoder's responsibility.
    """
    depth = items = 0
    quoted = escaped = False
    for value in raw:
        if quoted:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                quoted = False
        elif value == 34:
            quoted = True
        elif value in (91, 123):
            depth += 1
            items += 1
        elif value in (93, 125):
            depth -= 1
        elif value in (44, 58):
            items += 1
        if depth > MAX_JSON_DEPTH or items > MAX_JSON_ITEMS:
            raise DataRejected("json_complexity")


def decode_trace(payload: bytes, identity: str, day: date) -> Trace:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as source:
            raw = source.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise DataRejected("json_size")
        # Reject wide encodings rather than let NUL bytes hide delimiters from
        # the allocation guard. Upstream traces are UTF-8.
        if b"\x00" in raw:
            raise DataRejected("trace_encoding")
        check_json_limits(raw)
        body = json.loads(raw, object_pairs_hook=unique_object)
    except DataRejected:
        raise
    except (OSError, EOFError, UnicodeError, ValueError, RecursionError, zlib.error):
        raise DataRejected("trace_encoding") from None
    if not isinstance(body, dict) or body.get("icao") != identity:
        raise DataRejected("trace_identity")
    version = body.get("version")
    # The upstream documented example has no version; modern writer uses readsb 3.x.
    if version is not None and (
        not isinstance(version, str)
        or not re.fullmatch(r"readsb 3\.\d+\.\d+(?: [0-9a-f]{6,40})?", version)
    ):
        raise DataRejected("trace_version")
    base, rows = body.get("timestamp"), body.get("trace")
    if not number(base) or not isinstance(rows, list):
        raise DataRejected("trace_schema")
    start = datetime.combine(day, datetime.min.time(), UTC).timestamp()
    if not start <= base < start + 86400:
        raise DataRejected("body_date", date_error=True)
    points, callsigns = [], set()
    current_callsign = None
    unclassified_air = False
    last = -math.inf
    for row in rows:
        if not isinstance(row, list) or len(row) not in (14, 15):
            raise DataRejected("point_schema")
        if not number(row[0]) or type(row[6]) is not int or row[6] < 0 or row[6] & ~15:
            raise DataRejected("point_schema")
        ts = base + row[0]
        if not start <= ts < start + 86400:
            raise DataRejected("point_date", date_error=True)
        if ts <= last:
            raise DataRejected("point_order")
        last = ts
        lat, lon, alt, flags = row[1], row[2], row[3], row[6]
        if lat is not None and (not number(lat) or not -90 <= lat <= 90):
            raise DataRejected("point_position")
        if lon is not None and (not number(lon) or not -180 <= lon <= 180):
            raise DataRejected("point_position")
        if alt not in (None, "ground") and not number(alt):
            raise DataRejected("point_altitude")
        if row[10] is not None and not number(row[10]):
            raise DataRejected("point_altitude")
        extra = row[8]
        if extra is not None and not isinstance(extra, dict):
            raise DataRejected("point_details")
        if extra is not None and "flight" in extra:
            callsign = identifier(extra["flight"], r"[A-Z0-9-]{1,16}")
            current_callsign = callsign
            # An explicit clear must prevent inheritance, unlike an omitted
            # metadata snapshot. The empty marker cannot match an operator.
            callsigns.add(callsign or "")
        if number(alt) and not flags & 1 and lat is not None and lon is not None:
            unclassified_air |= current_callsign is None
        points.append(
            Point(
                ts,
                lat,
                lon,
                alt if number(alt) else None,
                alt == "ground",
                flags,
                row[10] if alt == "ground" else None,
            )
        )
    if callsigns and unclassified_air:
        # A later positive callsign cannot classify earlier unknown movements.
        callsigns.add("")
    return Trace(
        identity,
        identifier(body.get("r"), r"[A-Z0-9-]{1,16}"),
        identifier(body.get("t"), r"[A-Z0-9]{1,8}"),
        body.get("dbFlags"),
        callsigns,
        points,
    )
