import gzip
import io
import json
import tarfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from flights_converter import Airport, Reducer


@pytest.fixture
def day():
    return date(2026, 1, 1)


@pytest.fixture
def policy():
    return {
        "version": "synthetic-v1",
        "rules": [
            {"code": "TST", "pattern": "TST[0-9]{1,4}[A-Z]?"},
        ],
    }


@pytest.fixture
def airports():
    return [
        Airport("TEST", 0, 0, 0, "UTC", "synthetic-v1", "test-tzdb"),
        Airport("DEST", 0, 80, 0, "Asia/Dubai", "synthetic-v1", "test-tzdb"),
    ]


@pytest.fixture
def reducer(airports, policy):
    value = Reducer(airports, policy)
    yield value
    value.close()


def point(second, altitude=1000, *, lat=0, lon=0, callsign=None, flags=0, geom=None):
    return [
        second,
        lat,
        lon,
        altitude,
        100,
        0,
        flags,
        0,
        {"flight": callsign} if callsign else None,
        "adsb_icao",
        geom,
        None,
        None,
        None,
    ]


def body(day, *, identity="f0abcd", flags=0, callsign="TST123", rows=None, **extra):
    rows = (
        rows
        if rows is not None
        else [
            point(0, "ground", callsign=callsign),
            point(30, "ground"),
            point(60, 1000),
            point(90, 2000),
            point(120, "ground"),
            point(150, "ground"),
        ]
    )
    return {
        "icao": identity,
        "r": "SYNTH-OK",
        "t": "TEST",
        "dbFlags": flags,
        "timestamp": datetime.combine(day, datetime.min.time(), UTC).timestamp(),
        "version": "readsb 3.14.0 abcdef0",
        "trace": rows,
        **extra,
    }


def payload(value):
    return gzip.compress(json.dumps(value, separators=(",", ":")).encode(), mtime=0)


def member_name(value):
    return f"traces/{value['icao'][-2:]}/trace_full_{value['icao']}.json"


def archive(bodies=(), *, members=()):
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w", format=tarfile.USTAR_FORMAT) as output:
        for value in bodies:
            data = payload(value)
            info = tarfile.TarInfo(member_name(value))
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))
        for info, data in members:
            output.addfile(info, io.BytesIO(data))
    return source.getvalue()


def consume(reducer, day, values=(), *, data=None, warmup=False):
    records = []
    summary = reducer.consume(
        day,
        io.BytesIO(data if data is not None else archive(values)),
        records.append,
        warmup=warmup,
    )
    return records, summary


@pytest.fixture
def privacy_cases():
    return json.loads((Path(__file__).parent / "fixtures" / "synthetic.json").read_text())
