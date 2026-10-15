"""Streaming conversion seam; no network or database integration."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

from .airports import AirportIndex
from .errors import DataRejected, rejection_code
from .privacy import Classification, OperatorPolicy, normalise_opt_outs
from .segment import MAX_GAP, MAX_OPEN_SECONDS, Track, plausible, process_points, segment_record
from .trace import MAX_AIRCRAFT, BoundedReader, archive_members, decode_trace

MAX_TRACKS = 50_000
MAX_PIECES = 100_000
MAX_PENDING_BYTES = 128 * 1024 * 1024


class Reducer:
    def __init__(self, airports, operators, method="p4-v1", *, opt_outs=()):
        if method != "p4-v1":
            raise DataRejected("method_version")
        self.index = AirportIndex(airports)
        self.policy = (
            operators if isinstance(operators, OperatorPolicy) else OperatorPolicy(operators)
        )
        self.method = method
        self.opt_outs = normalise_opt_outs(opt_outs)
        self._tracks = {}
        self._denied_hexes = set()
        self._last_day = None

    def close(self):
        self._tracks.clear()
        self._denied_hexes.clear()
        self._last_day = None

    def break_continuity(self):
        """Call immediately when the adapter rejects or misses a parsed release."""
        self.close()

    def consume(self, day, stream, emit_sanitised, warmup=False):
        """Emit candidate rows and a final summary. The adapter commits atomically.

        On any exception discard this generation and call break_continuity before
        retry. DataRejected does this automatically. No raw data is written here.
        Repeated/out-of-order days reset RAM: rebuild the connected days in order
        and replace affected canonical rows by (hex,t0), never append updates.
        """
        try:
            if isinstance(day, str):
                day = date.fromisoformat(day)
            if type(day) is not date or not date.min < day < date.max - timedelta(days=1):
                raise ValueError
        except (TypeError, ValueError):
            self.close()
            raise DataRejected("day_argument", date_error=True) from None
        continuous = self._last_day == day - timedelta(days=1)
        if not continuous:
            self.close()
        reader = BoundedReader(stream)
        stats = {
            "record_type": "manifest_summary",
            "day": day.isoformat(),
            "input_bytes": 0,
            "n_members": 0,
            "n_aircraft": 0,
            "hour_aircraft": [0] * 24,
            "parse_errors": 0,
            "date_errors": 0,
            "duplicate_members": 0,
            "method_version": self.method,
            "operator_version": self.policy.version,
            "parse_complete": False,
            "predecessor_continuity": continuous,
            "boundary_done": False,
            "warmup": warmup,
        }
        seen, pieces = {}, {}
        pending = []
        pending_bytes = 0
        unknown_zones = 0
        invalidated_days = set()
        start = datetime.combine(day, datetime.min.time(), UTC).timestamp()
        cutoff = start + 86400

        def event(kind, airport, ts, support):
            nonlocal unknown_zones
            if warmup or airport is None:
                return
            if airport.zone is None:
                unknown_zones += 1
                return
            local_day = datetime.fromtimestamp(ts, UTC).astimezone(airport.zone).date().isoformat()
            # No per-event timestamps, identities, positions, or hashes leave RAM.
            key = (airport.icao, local_day, tuple(sorted(support)))
            if key not in pieces and len(pieces) >= MAX_PIECES:
                raise DataRejected("piece_limit")
            piece = pieces.setdefault(
                key,
                {
                    "record_type": "airport_piece",
                    "day": local_day,
                    "icao": airport.icao,
                    "release_day": day.isoformat(),
                    "release_days": sorted(support),
                    "tz": airport.tz,
                    "tz_version": airport.tz_version,
                    "reference_version": airport.reference_version,
                    "deps": 0,
                    "arrs": 0,
                    "method_version": self.method,
                },
            )
            piece[kind] += 1

        try:
            for identity, payload in archive_members(reader, day, stats):
                digest = hashlib.sha256(payload).digest()
                if identity in seen:
                    if seen[identity] != digest:
                        raise DataRejected("conflicting_duplicate")
                    stats["duplicate_members"] += 1
                    continue
                if len(seen) >= MAX_AIRCRAFT:
                    raise DataRejected("aircraft_limit")
                seen[identity] = digest
                trace = decode_trace(payload, identity, day)
                mask = 0
                for point in trace.points:
                    hour = int((point.ts - start) // 3600)
                    mask |= 1 << hour
                stats["n_aircraft"] += 1
                for hour in range(24):
                    stats["hour_aircraft"][hour] += bool(mask & (1 << hour))
                # A registration opt-out remains a deny for this identity even
                # if a later snapshot omits or changes the registration.
                if trace.reg is not None and f"reg:{trace.reg}" in self.opt_outs:
                    if (
                        identity not in self._denied_hexes
                        and len(self._denied_hexes) >= MAX_AIRCRAFT
                    ):
                        raise DataRejected("aircraft_limit")
                    self._denied_hexes.add(identity)
                classification = self.policy.classify(
                    identity,
                    trace.reg,
                    trace.flags,
                    trace.callsigns,
                    self.opt_outs,
                )
                if identity in self._denied_hexes:
                    classification = Classification(None, None)
                track = self._tracks.pop(identity, None)
                continuation = classification
                # Inheritance applies only to the already-open, still-connected
                # movement. New movements always need current positive evidence.
                first = next(
                    (p for p in trace.points if not p.flags & 1 and p.position is not None),
                    None,
                )
                if (
                    track
                    and track.movement
                    and track.last
                    and first
                    and plausible(track.last, first)
                    and first.ts - track.movement.t0 <= MAX_OPEN_SECONDS
                    and track.reg == trace.reg
                    and track.aircraft_type == trace.aircraft_type
                    and classification.page_class is None
                    and identity not in self._denied_hexes
                    and not trace.callsigns
                    and track.movement.classification.page_class == "operator"
                ):
                    continuation = self.policy.classify(
                        identity,
                        trace.reg,
                        trace.flags,
                        {track.movement.callsign} if track.movement.callsign else set(),
                        self.opt_outs,
                    )
                if track and track.movement and continuation != track.movement.classification:
                    if track.movement.classification.page_class is not None:
                        invalidated_days.update(track.movement.days)
                    track.movement.classification = Classification(None, None)
                if track and (track.reg != trace.reg or track.aircraft_type != trace.aircraft_type):
                    track = None
                if track is None:
                    track = Track(
                        trace.reg,
                        trace.aircraft_type,
                        classification,
                        trace.flags if type(trace.flags) is int else None,
                    )
                track.classification = classification
                track.flags = trace.flags if type(trace.flags) is int else None

                def emit(movement, identity=identity, track=track):
                    nonlocal pending_bytes
                    if not warmup:
                        record = segment_record(identity, track, movement, day, self.method)
                        if record:
                            # A later conflicting duplicate may deny this same
                            # identity. Keep candidates in bounded RAM until
                            # the whole archive passes validation, never disk.
                            encoded = json.dumps(record, separators=(",", ":"), allow_nan=False)
                            pending_bytes += len(encoded)
                            if pending_bytes > MAX_PENDING_BYTES:
                                raise DataRejected("output_limit")
                            pending.append(encoded)

                process_points(
                    track,
                    trace.points,
                    day,
                    classification,
                    trace.callsigns,
                    self.index,
                    event,
                    emit,
                )
                if track.movement:
                    emit(track.movement)
                if track.last and track.last.ts >= cutoff - MAX_GAP:
                    if len(self._tracks) >= MAX_TRACKS:
                        raise DataRejected("track_limit")
                    self._tracks[identity] = track
            if not seen:
                raise DataRejected("no_traces")
            # Retain only tracks that could connect across this midnight.
            self._tracks = {
                key: track
                for key, track in self._tracks.items()
                if track.last.ts >= cutoff - MAX_GAP
            }
            self._last_day = day
            for encoded in pending:
                emit_sanitised(json.loads(encoded))
            for piece in sorted(
                pieces.values(), key=lambda p: (p["icao"], p["day"], p["release_days"])
            ):
                emit_sanitised(piece)
            stats["parse_complete"] = True
            emit_sanitised(
                {
                    "record_type": "diagnostics",
                    "day": day.isoformat(),
                    "predecessor_continuity": continuous,
                    "unknown_zone_endpoints": unknown_zones,
                    "retract_release_days": sorted(invalidated_days),
                    "boundary_done": False,
                }
            )
        except BaseException as error:
            self.close()
            if isinstance(error, DataRejected):
                stats["date_errors" if error.date_error else "parse_errors"] = 1
                emit_sanitised(
                    {
                        "record_type": "diagnostics",
                        "day": day.isoformat(),
                        "code": rejection_code(error.code),
                        "discard_generation": True,
                    }
                )
            raise
        finally:
            stats.update(input_bytes=reader.n_bytes)
            emit_sanitised(stats)
        return stats
