"""Caller-provided airport reference, endpoint matching, and exact local days."""

import importlib.metadata
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

from .errors import DataRejected
from .trace import number

EARTH_KM = 6371.0088


def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2
    h += math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(min(1, max(0, h))))


def tzdb_version():
    for root in TZPATH:
        try:
            first = (Path(root) / "tzdata.zi").open(encoding="utf-8")
            with first:
                match = re.fullmatch(r"# version (\S+)\s*", first.readline())
            if match:
                return match[1]
        except OSError:
            continue
    try:
        return "tzdata-" + importlib.metadata.version("tzdata")
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


@dataclass(frozen=True)
class Airport:
    icao: str
    lat: float
    lon: float
    elevation_ft: float | None
    tz: str | None
    reference_version: str
    tz_version: str

    def __post_init__(self):
        if (
            not isinstance(self.icao, str)
            or not re.fullmatch(r"[A-Z0-9]{3,8}", self.icao)
            or not number(self.lat)
            or not -90 <= self.lat <= 90
            or not number(self.lon)
            or not -180 <= self.lon <= 180
            or (self.elevation_ft is not None and not number(self.elevation_ft))
            or not isinstance(self.reference_version, str)
            or not self.reference_version
            or not isinstance(self.tz_version, str)
            or not self.tz_version
        ):
            raise DataRejected("airport_config")

    @property
    def zone(self):
        if not self.tz:
            return None
        try:
            return ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return None


def load_airports(config):
    if not isinstance(config, dict) or not isinstance(config.get("airports"), list):
        raise DataRejected("airport_config")
    try:
        return [
            Airport(
                row["icao"],
                row["lat"],
                row["lon"],
                row.get("elevation_ft"),
                row.get("tz"),
                config["version"],
                config.get("tz_version", tzdb_version()),
            )
            for row in config["airports"]
        ]
    except (KeyError, TypeError):
        raise DataRejected("airport_config") from None


def cell(lat, lon):
    lat, lon = math.radians(lat), math.radians(lon)
    return tuple(
        math.floor(v / 5)
        for v in (
            EARTH_KM * math.cos(lat) * math.cos(lon),
            EARTH_KM * math.cos(lat) * math.sin(lon),
            EARTH_KM * math.sin(lat),
        )
    )


class AirportIndex:
    """5 km Earth-centred grid; handles poles and the antimeridian."""

    def __init__(self, airports):
        self.airports = list(airports)
        self.by_icao = {airport.icao: airport for airport in self.airports}
        if len(self.by_icao) != len(self.airports):
            raise DataRejected("duplicate_airport")
        self.grid = defaultdict(list)
        for airport in self.airports:
            self.grid[cell(airport.lat, airport.lon)].append(airport)

    def match(self, position, altitude_ft=None):
        if position is None:
            return None
        x, y, z = cell(*position)
        candidates = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for airport in self.grid.get((x + dx, y + dy, z + dz), ()):
                        km = distance_km(position, (airport.lat, airport.lon))
                        if km > 5 + 1e-9:
                            continue
                        if altitude_ft is not None and airport.elevation_ft is not None:
                            if abs(altitude_ft - airport.elevation_ft) > 1500:
                                continue
                        candidates.append((km, airport.icao, airport))
        candidates.sort(key=lambda value: (value[0], value[1]))
        if not candidates or (
            len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 1e-6
        ):
            return None
        return candidates[0][2]


def local_interval(airport: Airport, day: date):
    zone = airport.zone
    if zone is None:
        return None
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
    # Includes historical skipped dates (zero elapsed seconds).
    return start, end


@dataclass(frozen=True)
class ReleaseState:
    manifest_sha: str
    good: bool = False
    converted: bool = False
    boundary_done: bool = False
    successor_sha: str | None = None


def release_ready(day, releases):
    current, successor = releases.get(day), releases.get(day + timedelta(days=1))
    return bool(
        current
        and successor
        and current.good
        and current.converted
        and current.boundary_done
        and current.manifest_sha
        and current.successor_sha
        and successor.good
        and successor.converted
        and successor.manifest_sha
        and current.successor_sha == successor.manifest_sha
    )


def airport_day(airport: Airport, day: date, pieces, releases):
    """Roll up anonymous pieces; unavailable coverage produces NULL counts.

    The caller supplies current manifest states, including the converted successor.
    Replace pieces by their conversion generation on replay; never append a replay.
    """
    interval = local_interval(airport, day)
    result = {
        "icao": airport.icao,
        "day": day.isoformat(),
        "tz": airport.tz,
        "tz_version": airport.tz_version,
        "reference_version": airport.reference_version,
        "deps": None,
        "arrs": None,
        "n_seconds": 0,
        "day_seconds": None,
        "n_hours": 0,
        "day_hours": None,
        "final": False,
    }
    if interval is None:
        return result
    start, end = interval
    seconds = int((end - start).total_seconds())
    cursor = start.date()
    while datetime.combine(cursor, datetime.min.time(), UTC) < end:
        left = max(start, datetime.combine(cursor, datetime.min.time(), UTC))
        right = min(end, datetime.combine(cursor + timedelta(days=1), datetime.min.time(), UTC))
        if release_ready(cursor, releases):
            result["n_seconds"] += int((right - left).total_seconds())
        cursor += timedelta(days=1)
    final = seconds > 0 and seconds == result["n_seconds"]
    deps = arrs = 0
    for piece in pieces:
        if piece["icao"] != airport.icao or piece["day"] != day.isoformat():
            continue
        if (
            piece["tz"] != airport.tz
            or piece["tz_version"] != airport.tz_version
            or piece["reference_version"] != airport.reference_version
        ):
            final = False
        for support in piece["release_days"]:
            status = releases.get(date.fromisoformat(support))
            if not status or not status.good or not status.converted:
                final = False
        deps += piece["deps"]
        arrs += piece["arrs"]
    result.update(
        day_seconds=seconds,
        day_hours=seconds / 3600,
        n_hours=result["n_seconds"] / 3600,
        final=final,
    )
    if final:
        result.update(deps=deps, arrs=arrs)
    return result
