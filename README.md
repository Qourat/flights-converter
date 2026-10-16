# flights-converter

`flights_converter` converts local adsb.lol **prod-0** release archives into
privacy-filtered flight segments and anonymous airport counts. Python 3.12+,
stdlib only at runtime. The converter performs no network requests.

Version **0.1.1** accepts ACAS/TCAS advisory members (`acas/*.csv.gz`) in prod-0
releases by skipping them without parsing. Trace parsing and privacy filtering
are unchanged. See the [changelog](CHANGELOG.md).

## Run

```sh
python -m pip install .
flights-convert --day 2026-10-12 --airports airports.json \
  --operators operators.json --method p4-v1 \
  --input v2026.10.12-planes-readsb-prod-0.tar.aa \
          v2026.10.12-planes-readsb-prod-0.tar.ab \
  --output compact.jsonl
```

Or concatenate the already-downloaded parts into stdin:

```sh
cat v2026.10.12-planes-readsb-prod-0.tar.aa \
    v2026.10.12-planes-readsb-prod-0.tar.ab |
  python -m flights_converter --day 2026-10-12 \
    --airports airports.json --operators operators.json --input - --output compact.jsonl
```

Use `--format csv` for CSV. It has `record_type,record` columns; `record` is a
JSON object preserving the types of each record. Omit `--output` for stdout.
Named part inputs are checked for the exact day/prod-0 name and contiguous
suffixes, then sorted. A truncated final part fails archive validation. Checking
the full advertised asset list, including whether an entire final part is absent,
belongs to the caller; use `JoinedParts(streams, expected=[(size, sha256), ...])`
for per-part transfer verification.

Exit 0 means complete parsing, **not** complete source coverage. Exit 65 rejects
malformed input, 74 indicates I/O failure, 130 interruption, and 2 bad arguments.
Failed parsing leaves an existing output file untouched and emits only a fixed
diagnostic code to stderr. The CLI holds at most 512 MiB of sanitised compact
output in a private temporary spool, removed when the process exits normally or
raises an exception. It never extracts or stores raw archive members.

## Reference configuration

The caller supplies an airport snapshot; there is no airport downloader. Positions
are degrees, elevations feet, and time zones IANA names. Mark an unknown or
untrusted zone as `null`. Pin the installed tzdb for reproducibility; `tz_version`
defaults to the detected system/package version. When overriding this label, the
caller must ensure it describes the installed rules.

```json
{
  "version": "example-airports-v1",
  "airports": [
    {"icao": "TEST", "lat": 0, "lon": 0, "elevation_ft": 0, "tz": "UTC"}
  ]
}
```

The bundled [operator policy](flights_converter/operators.json) is versioned and
empty, so operator eligibility requires a reviewed caller policy. Military
classification works independently. Operator rules use full-callsign regular
expressions. Configurations are trusted, reviewed inputs: use bounded patterns,
an explicit version, and a fixture for every admitted pattern. A synthetic example
for testing the file format is:

```json
{
  "version": "synthetic-v1",
  "rules": [{"code": "TST", "pattern": "TST[0-9]{1,4}[A-Z]?"}]
}
```

This example asserts no real operator identity. Do not infer an operator from any
three letters. Add opt-outs using `--opt-outs deny.json`, where the file is an
array of `hex:abcdef` or `reg:EXAMPLE` keys. The converter never echoes deny keys.

## Privacy and method

PIA (`dbFlags & 4`), LADD (`& 8`), non-ICAO (`~`) and opted-out identities cannot
produce entity rows, even when marked military or using an operator callsign.
Military is bit 1; bit 2 means interesting and grants no eligibility. Missing,
malformed or unknown flag bits fail closed. A reviewed operator match or military
status is required for an `eligible_segment`. Other aircraft contribute anonymous
counts only, including opted-out traffic. No owner, receiver UUID, free text,
private identifier, private position or identifier hash is emitted in those
counts or diagnostics. Receipts contain aggregate counters, versions, dates and
fixed status fields; input fingerprints stay out of receipts. `JoinedParts` can
still verify caller-supplied per-part digests without emitting them.

Registration opt-outs fail closed when a snapshot lacks a registration. Once a
registration denial is observed, it continues to deny that hex for the current
consecutive run, including changed registrations. Supply hex deny keys for known
aliases across cold starts; unavailable alias history cannot be inferred.

Method `p4-v1` uses observed ground transitions. Confirm ground with two reports
at least 30 seconds apart. First airborne data without confirmed ground is an
open fragment. Flight rows require `t1 > t0`; a single airborne report never
produces a flight row. Observed endpoints can still contribute anonymous counts.
Join plausible adjacent observations across midnight only within
30 minutes and 1,000 knots; split unresolved tracks after 48 hours. Callsign
changes and leg markers alone do not split a flight. Geometric fallback altitude
is excluded from the barometric maximum. Distance sums observed plausible
airborne steps. Airport endpoints use the nearest airport within 5 km and within
1,500 feet of its elevation when altitude is available; ties remain unknown.
Only observed departures/arrivals add counts.

Local days use `ZoneInfo` midnight instants converted to UTC, then elapsed-second
intersections with ready releases. DST and fractional offsets retain exact
seconds. Missing zones, failed inputs or unreconciled boundaries withhold counts;
they cannot establish zero. The full method, output fields and adapter duties
are in the [public converter contract](docs/method.md).

## Streaming API and boundary hooks

```python
from flights_converter import Reducer, load_airports

# airport_config and operator_config are parsed caller-supplied JSON objects.
reducer = Reducer(load_airports(airport_config), operator_config, method="p4-v1")
try:
    reducer.consume(previous_day, predecessor_stream, stage, warmup=True)
    reducer.consume(day, current_stream, stage)
finally:
    reducer.close()
```

`stage(record)` receives sanitised candidate dictionaries. Record types are
`eligible_segment`, `airport_piece`, `diagnostics`, and `manifest_summary`.
The summary includes input bytes, member/aircraft/duplicate counts,
`skipped_members` and `skipped_bytes`, errors, 24 distinct-aircraft UTC-hour
populations counted **before** privacy filtering,
method/policy versions and continuity flags. No identity-level audit data leaves
RAM for aggregate-only aircraft.

Keep one reducer across consecutive inputs. Every cold start replays the
predecessor in warmup mode, which promotes no movements or counts. A one-day CLI
run reports absent predecessor continuity and never claims `boundary_done`.
Call `break_continuity()` immediately if a release is absent or rejected by the
external quality gate. Exceptions also clear boundary state. Private state is
RAM only; applications must disable swap/core dumping of that memory and avoid
debugging raw trace objects into logs.

The adapter must stage transactionally and discard the generation on any parsing,
transfer, quality or promotion failure. Upsert eligible snapshots by `(hex,t0)`
so a successor extends the existing flight. Anonymous pieces are replaced by
conversion generation, never appended on retry. Out-of-order/replaced inputs
require an ordered replay of the connected days and removal of obsolete
provisional rows. Honor any `retract_release_days` diagnostic before publication.
The caller owns persistent eligible fragments, opt-out retractions and aliases.
An inherited operator callsign can preserve only an already-open connected
movement. New movements need current positive classification.
Explicit callsign clears fail closed; a later operator match cannot classify
earlier airborne observations whose callsign was unknown.

Archive reads are bounded to 64 KiB. Limits reject oversized input with fixed
codes: 16 GiB per archive, 16 MiB per compressed trace, 64 MiB per expanded trace
or skipped auxiliary member, one million members and 500,000 distinct aircraft
per release. JSON is checked before object allocation for depth (32) and
structural delimiters (one million); only UTF-8 traces are supported. Trailing
zero padding is limited to 1 MiB. At most 50,000 boundary tracks and 100,000
anonymous groups are retained; registration-denied hexes are capped at 500,000
per consecutive run. CLI configurations are limited to 8 MiB each. These are
rejection ceilings, not measured real-release capacity claims.
Eligible candidates stay in a 128 MiB RAM buffer until archive validation ends,
so a later contradictory duplicate cannot expose an earlier candidate to the
callback or CLI spool. Raw trace bodies never enter that buffer.

`quality.assess` returns byte/hour ratios against four earlier complete
same-weekday references. Size below .85 OR any hourly ratio below the explicitly
supplied audited F rejects the release. `quality.calibrate` returns the
.50/.60/.70/.80 sensitivity table and highest passing candidate, or `None` when
none passes. It does not manufacture an eight-week audit or bootstrap acceptance.

`ReleaseState` and `release_ready` require good converted D and D+1 plus a
committed boundary using D+1's exact current manifest hash. D+1 need not itself be
ready. `airport_day(airport, local_date, pieces, release_states)` returns exact
`n_seconds`, `day_seconds`, hours, finality and nullable departure/arrival totals.
Only full elapsed coverage and reconciled inputs can publish counts.

## Verified upstream format and attribution

Public documentation was inspected on **2026-10-15**. The adsb.lol
[release README](https://github.com/adsblol/globe_history_2026/blob/main/README.md)
describes a tar archive split across assets, containing gzip JSON traces and
heatmap data. The inspected
[2026-10-12 prod-0 asset list](https://github.com/adsblol/globe_history_2026/releases/expanded_assets/v2026.10.12-planes-readsb-prod-0)
advertises `.tar.aa` and `.tar.ab`; the reader does not assume three parts.

The field layout follows readsb's
[trace JSON documentation](https://github.com/wiedehopf/readsb/blob/dev/README-json.md#trace-jsons)
and [`generateTraceJson` / `sprintTracePoint` writer](https://github.com/wiedehopf/readsb/blob/dev/json_out.c).
The producer's [permanent-trace writer](https://github.com/wiedehopf/readsb/blob/dev/globe_index.c)
uses a UTC-day interval. We validate the body timestamp and every offset against
that half-open day; a path date alone is insufficient. Supported point rows have
14 fields or 15 with a discarded receiver field. The documented unversioned
layout and schema-compatible `readsb 3.x.y [commit]` versions are accepted;
other versions/shapes reject the release. Trace files may end in `.json` while
their contents are gzip.

The explicit non-trace allowlist lives in `NON_TRACE_MEMBER_PATTERNS` in
[`flights_converter/trace.py`](flights_converter/trace.py). It permits direct
`acas/*.csv.gz` files (ACAS/TCAS advisories), the existing `heatmap` family, and
metadata named `README`, `README.txt`, `README.md`, `LICENSE`, `LICENSE-ODbL.txt`
or `LICENSE-CC0.txt`. Release directory prefixes are permitted; nested files
under `acas/` and other ACAS extensions are not allowlisted. These payloads are
discarded without decompression or parsing, under the existing archive/member
limits. `skipped_members` counts these regular files; `skipped_bytes` sums their
stored payload sizes (compressed bytes for `.gz`), excluding tar headers and
padding. Both counters start at zero and expose no member names or contents.
Directory entries are validated but do not increment the skip counters. Unknown
files, links, unsafe paths and corrupt archives still fail.
Tar validation requires two zero end blocks and only block-aligned zero padding
through EOF. A member may cross any asset boundary.

No release asset was downloaded, including range requests. Fixtures are
synthetic and producer-shaped; they are not claimed to be captured real traces.
The test suite does not certify real-release performance or real-flight sampling.

Software is [MIT licensed](LICENSE), copyright Qourat. The upstream database is
licensed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/): credit
**adsb.lol and its contributors**, link to the source and this converter's method,
and preserve applicable database/derived-data attribution and share-alike terms.
The software licence does not replace the data licence. Upstream also provides a
[CC0 notice](https://github.com/adsblol/globe_history_2026/blob/main/LICENSE-cc0.txt)
for feeder contributions; its release README identifies the assembled database
as ODbL.

## Development and remaining integration gates

```sh
python -m pytest -q
ruff check flights_converter tests
ruff format --check flights_converter tests
```

Tests run offline, with synthetic privacy sentinels, five overnight flights,
year-boundary/replay cases, corrupt/split archives, airport boundaries and DST
days in New York and Lord Howe, plus UTC, Dubai and Kathmandu. Before derived
data publication, the integrating application must measure a complete release
and predecessor replay, inspect sampled real flights/overnights, perform the
eight-week completeness audit, pin the code/config/tzdb, and validate its atomic
coverage/retraction pipeline. No publishing or production measurements are
performed by this repository's unit suite.
