"""Conservative observed-transition segmentation, with RAM-only boundary state."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .airports import distance_km
from .privacy import Classification
from .trace import Point

MAX_GAP = 1800
MAX_KNOTS = 1000
MAX_OPEN_SECONDS = 48 * 3600
GROUND_SECONDS = 30


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def plausible(a: Point, b: Point):
    elapsed = b.ts - a.ts
    if not 0 < elapsed <= MAX_GAP or a.position is None or b.position is None:
        return False
    return distance_km(a.position, b.position) / (elapsed / 3600) / 1.852 <= MAX_KNOTS


@dataclass(slots=True, repr=False)
class Movement:
    t0: float
    t1: float
    from_pos: list | None
    to_pos: list | None
    dep_at: float | None = None
    arr_at: float | None = None
    from_icao: str | None = None
    to_icao: str | None = None
    km: float = 0
    max_alt_ft: float | None = None
    days: set[str] = field(default_factory=set)
    classification: Classification = field(default_factory=lambda: Classification(None, None))
    callsign: str | None = None


@dataclass(slots=True, repr=False)
class Track:
    reg: str | None
    aircraft_type: str | None
    classification: Classification
    flags: int | None
    last: Point | None = None
    ground_first: Point | None = None
    ground_last: Point | None = None
    ground_count: int = 0
    confirmed: bool = False
    movement: Movement | None = None


def segment_record(identity, track: Track, movement: Movement, day, method):
    if movement.classification.page_class is None or movement.t1 <= movement.t0:
        return None
    return {
        "record_type": "eligible_segment",
        "day": day.isoformat(),
        "hex": identity,
        "reg": track.reg,
        "type": track.aircraft_type,
        "callsign": movement.callsign,
        "t0": iso(movement.t0),
        "t1": iso(movement.t1),
        "dep_at": iso(movement.dep_at) if movement.dep_at is not None else None,
        "arr_at": iso(movement.arr_at) if movement.arr_at is not None else None,
        "dep_observed": movement.dep_at is not None,
        "arr_observed": movement.arr_at is not None,
        "from_icao": movement.from_icao,
        "to_icao": movement.to_icao,
        "from_pos": movement.from_pos,
        "to_pos": movement.to_pos,
        "km": movement.km,
        "max_alt_ft": movement.max_alt_ft,
        "open_start": movement.dep_at is None,
        "open_end": movement.arr_at is None,
        "release_days": sorted(movement.days),
        "page_class": movement.classification.page_class,
        "eligibility_rule": movement.classification.rule,
        "db_flags": track.flags,
        "method_version": method,
    }


def process_points(track, points, day, classification, callsigns, index, event, emit):
    """Update one compact track; callbacks receive only sanitised records/counts."""
    for point in points:
        # Stale positions count toward reporting population, never transitions.
        if point.flags & 1 or point.position is None:
            continue
        last = track.last
        discontinuity = last is not None and not plausible(last, point)
        if track.movement and point.ts - track.movement.t0 > MAX_OPEN_SECONDS:
            discontinuity = True
        if discontinuity:
            if track.movement:
                emit(track.movement)
            track.movement = None
            track.ground_first = track.ground_last = None
            track.ground_count = 0
            track.confirmed = False
            last = None
        if point.ground:
            if track.ground_first is None:
                track.ground_first = point
                track.ground_count = 0
            track.ground_count += 1
            track.ground_last = point
            if track.ground_count >= 2 and point.ts - track.ground_first.ts >= GROUND_SECONDS:
                track.confirmed = True
                if track.movement:
                    movement = track.movement
                    endpoint = track.ground_first
                    airport = index.match(endpoint.position, endpoint.ground_altitude)
                    movement.arr_at = endpoint.ts
                    movement.to_pos = endpoint.position
                    movement.to_icao = airport.icao if airport else None
                    movement.days.update((day.isoformat(), iso(endpoint.ts)[:10]))
                    event("arrs", airport, endpoint.ts, movement.days)
                    emit(movement)
                    track.movement = None
        elif point.altitude is not None:
            if track.movement is None:
                endpoint = track.ground_last if track.confirmed else None
                airport = (
                    index.match(endpoint.position, endpoint.ground_altitude) if endpoint else None
                )
                track.movement = Movement(
                    point.ts,
                    point.ts,
                    endpoint.position if endpoint else point.position,
                    point.position,
                    dep_at=point.ts if endpoint else None,
                    from_icao=airport.icao if airport else None,
                    classification=classification,
                    callsign=min(c for c in callsigns if c) if any(callsigns) else None,
                )
                if endpoint:
                    # The transition, rather than the beginning of the ground run, supports takeoff.
                    track.movement.days.add(iso(endpoint.ts)[:10])
                    event("deps", airport, point.ts, {day.isoformat(), iso(endpoint.ts)[:10]})
            movement = track.movement
            movement.days.add(day.isoformat())
            if last and not last.ground and last.altitude is not None and plausible(last, point):
                movement.km += distance_km(last.position, point.position)
            movement.t1 = point.ts
            movement.to_pos = point.position
            if not point.flags & 8:
                movement.max_alt_ft = max(
                    point.altitude,
                    movement.max_alt_ft if movement.max_alt_ft is not None else point.altitude,
                )
            # One unconfirmed ground report does not create a landing or a new takeoff.
            track.ground_first = track.ground_last = None
            track.ground_count = 0
            track.confirmed = False
        else:
            # Unknown altitude/on-ground state cannot bridge a transition.
            track.ground_first = track.ground_last = None
            track.ground_count = 0
            track.confirmed = False
        track.last = point
