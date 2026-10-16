# Public converter contract

This document describes the standalone `flights_converter` boundary. It contains
no deployment configuration or application-specific integration instructions.

## Inputs and outputs

Python 3.12+, method `p4-v1`, no runtime dependencies, no network access. The
caller supplies a binary non-seekable concatenation of all advertised parts of
one `vYYYY.MM.DD-planes-readsb-prod-0` release, a versioned airport snapshot,
a reviewed operator policy, and current opt-out keys. Other release instances
are outside this contract.

`Reducer.consume(day, stream, emit_sanitised, warmup=False)` emits dictionaries
with `record_type` equal to `eligible_segment`, `airport_piece`, `diagnostics`,
or `manifest_summary`. The emitter is a staging callback. Every record belongs
to the current conversion generation and must be discarded if parsing, quality
assessment, or promotion fails. A complete parse alone establishes no source
coverage. `close()` discards all boundary state. `break_continuity()` is the
explicit missing/rejected-release hook.

The summary contains `day`, `input_bytes`, `n_members`, `skipped_members`,
`skipped_bytes`, `n_aircraft`, 24 UTC `hour_aircraft` integers, `parse_errors`,
`date_errors`, `duplicate_members`,
`method_version`, plus policy version and continuity indicators.
Hourly populations include every parsed identity before classification. Only
their totals leave memory. Member hashes and per-identity masks are transient.
No input or per-aircraft fingerprint is included in receipts. Digest checks in
`JoinedParts` remain available for transfer verification.

Non-trace members are skipped only if matched by the explicit
[`NON_TRACE_MEMBER_PATTERNS` allowlist](../flights_converter/trace.py).
`skipped_members` counts fully discarded regular members and `skipped_bytes`
sums their stored payload bytes, excluding tar headers and padding. No skipped
member names or contents are emitted, decompressed or parsed. Path, type, size
and archive integrity checks still apply; unknown members reject the release.

The CLI accepts stdin or ordered local part paths. JSONL has one record per line.
CSV has `record_type,record` columns, with a JSON object in `record`; this preserves
NULLs, lists and booleans across the heterogeneous record types. Exit codes are
0 for complete parsing, 65 for malformed/date/format input, 74 for I/O failure,
130 for interruption, and 2 for invalid arguments. Named destinations are
replaced only after successful conversion. Stdout starts only after validation;
a downstream write failure can still interrupt its delivery.

## Privacy

PIA (`dbFlags & 4`), LADD (`& 8`), non-ICAO (`~`) and opted-out identities never
appear in entity records. These denials override military and operator matches.
Military is bit 1; bit 2 alone confers no eligibility. Missing/malformed flags or
unknown bits fail closed. Only an explicit reviewed operator match or military
status permits entity output. Everything else contributes anonymous airport
counts only. Owners, receiver identifiers, free text, private positions,
registrations, callsigns and reversible substitute identifiers never enter
anonymous pieces, receipts or diagnostics.

Private boundary state is RAM only. On every cold start, replay the predecessor
with `warmup=True`; this emits no movements or counts. No raw trace or dump spool
is created. The CLI sanitised spool is limited to 512 MiB and removed on failure.
Applications must prevent private RAM from entering swap, crash dumps or debug
logs. The public allowlist starts empty; review, version and test every added
pattern before use.

Registration denials fail closed when the current registration is missing.
Previously observed registration opt-outs remain attached to their hex in RAM
throughout a consecutive run, even when a later registration changes. A cold
start cannot infer unavailable aliases; callers should supply hex keys for known
opted-out identities and enforce retractions in stored outputs.

## Method p4-v1

Ground means an explicit `"ground"` report. Confirm a ground run using at least
two reports separated by at least 30 seconds. A confirmed ground-to-air change
produces a departure at the first airborne timestamp; a confirmed landing is
dated at the first ground timestamp in that run. First/last airborne timestamps
are `t0`/`t1`. Initial airborne data is an open fragment. Callsign changes and
leg-marker flags alone do not split a flight. Stale/missing positions cannot
establish transitions.

Every emitted segment requires `t1 > t0`, including open fragments. Single-report
movements can support observed anonymous airport endpoints but never become
flight rows. An inherited callsign applies only to an already-open connected
movement; a new movement requires current positive classification.
An explicit blank/null callsign prevents operator inheritance. Trace-level
operator classification also fails closed if earlier airborne observations
precede the first available callsign.

Adjacent points join only with a gap at most 30 minutes and observed speed at
most 1,000 knots. Changed registration or aircraft type breaks identity
continuity. Open movements split after 48 hours. Distance is the sum of observed,
plausible airborne-to-airborne steps, including the boundary step once; it is
not a great-circle route estimate. Geometric fallback altitude does not enter
the barometric maximum. Missing quantities remain NULL.

Archive reads are at most 64 KiB. Resource ceilings are 16 GiB per archive,
16 MiB per compressed trace, 64 MiB per expanded trace and auxiliary member,
one million members, 500,000 distinct aircraft per release, 50,000 retained
boundary tracks, 100,000 anonymous groups and 500,000 remembered registration
denials per consecutive run. Only UTF-8 trace JSON is accepted; a pre-allocation
guard limits nesting to 32 and structural delimiters to one million. End padding
is at most 1 MiB. The CLI bounds each configuration to 8 MiB and rejects duplicate
keys. Limit failures discard the generation and boundary state. These ceilings
bound converter-owned state; they are not real-release memory measurements.
Eligible output candidates occupy at most 128 MiB of encoded JSON in RAM and
reach the callback only after complete archive validation. In particular, a
late conflicting duplicate cannot expose an earlier candidate for that identity.
The candidate buffer never contains raw trace bodies and is never written to disk.

Airport matching uses confirmed ground endpoints, nearest within 5 km, and
altitude within 1,500 feet of elevation when both exist. Ground reports have no
numeric barometric altitude; the point's geometric altitude is used for this
endpoint check when present. Distance ties within one millimetre remain unknown.
The caller controls reference scope and validated time zones. No inferred
takeoff or high-altitude fragment endpoint creates an airport event.

Airport pieces contain counts grouped by airport, local date, conversion day
and supporting UTC dates, plus reference/tz/method versions. They contain no
per-event times or aircraft identifiers. Use half-open local-day intervals.
Compute their two midnight instants with `ZoneInfo`, convert both to UTC, and
intersect elapsed intervals with ready UTC days. Seconds are exact, including
fractional offsets, repeated hours and half-hour DST. Missing/invalid zones
produce missing counts. Re-zoning requires new pieces or a bounded input replay.

## Integration responsibilities

The caller discovers and pins the published release and complete asset list,
checks advertised lengths/digests (`JoinedParts` supports these), owns quality,
transactions, generation replacement, durable eligible fragments and retractions.
Never append anonymous pieces on replay. Upsert eligible snapshots by `(hex,t0)`;
the successor extends the original row. To handle late/out-of-order inputs,
re-stream the bounded connected component chronologically and transactionally
replace its prior rows and pieces, including obsolete provisional keys. The
reducer resets continuity on nonconsecutive inputs and failed conversions.

Diagnostics may request `retract_release_days` after a continuing row loses its
positive classification. Retract those generations before publishing, apply
the current deny policy throughout the affected component, and rebuild. No
excluded key is placed in that diagnostic. A changed opt-out policy also requires
the caller to retract existing entities, aliases and derived links.

`quality.assess` compares bytes and all 24 populations to medians of the four
earlier complete same-weekday releases. Size below .85 OR any hourly ratio below
the caller's audited F rejects the day. Missing references do not pass. Calibrate
F among .50/.60/.70/.80 using an independently reviewed eight-week audit; no
passing candidate means stop and return the sensitivity table. Bootstrap uses
28 separately audited warmup days, 56 core days and a successor; this package
does not download or certify that audit.

A ready UTC day needs a good converted successor and a committed reconciliation
against that successor's exact current manifest hash. The successor need not
itself be ready. `ReleaseState`, `release_ready` and `airport_day` implement this
gate. Counts remain NULL until every elapsed second is covered and all supporting
inputs are good. A complete observed zero is distinct from missing evidence.
The caller owns settling, audit receipts, retraction of invalid generations and
selection of its scheduled-airport cohort from common complete days.

## Evidence and validation limits

Public source links, retrieval date and the inspected asset names are in the
[README](../README.md). Fixtures are synthetic, shaped against readsb's documented
trace and writer; no real trace is bundled. No archive asset was downloaded or
range-probed. Unit tests do not establish real-release throughput, an eight-week
quality audit, ten sampled real flights, or five sampled real overnight flights.
Those integration gates must be measured before publishing derived data.

Software: MIT, Qourat. Source database and applicable derived-data obligations:
ODbL 1.0, attribution to adsb.lol and its contributors. See the README.
