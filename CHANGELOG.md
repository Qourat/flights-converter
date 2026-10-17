# Changelog

## 0.1.3 — 2026-10-16

- Apply the lead decisions from the complete v2026.10.15 prod-0 scan of 39,550
  traces: the remaining rejections were 4 `point_date` traces with points at most
  1 second after day end, and 34 `identity_format` traces with malformed callsigns.
- Drop points within 60 seconds outside the half-open UTC day and count them in
  `out_of_day_points`. Further offsets still reject with `point_date` and a date
  error; body timestamps must still fall within the day.
- Treat every malformed callsign string as unknown and count it in
  `invalid_callsigns`, including punctuation and values longer than 16 characters
  after stripping and uppercasing. Non-string values still reject.
- Keep the first point at repeated timestamps (`duplicate_points`) and treat
  malformed registration/type strings as unknown (`invalid_identifiers`).

## 0.1.2 — 2026-10-16

- Accept the real prod-0 layout found on v2026.10.15: `acas/acas.json.gz` (besides `acas/acas.csv.gz`) and the
  lowercase `LICENSE-cc0.txt` are skipped like other known non-trace members (counted only). `./` prefixes and
  non-ICAO `trace_full_~xxxxxx` traces were already supported; a real-layout regression test now covers all of it.

## 0.1.1 — 2026-10-16

- Skip prod-0 ACAS/TCAS advisory files matching `acas/*.csv.gz` without
  decompression or parsing. Keep the complete non-trace allowlist in
  `flights_converter/trace.py`, including existing heatmap and metadata rules.
  Unknown members continue to reject the release.
- Add aggregate `skipped_members` and `skipped_bytes` counters to the run summary.
  Bytes are stored payload sizes, excluding tar headers and padding; no member
  names or contents are emitted.
- Preserve trace parsing, privacy filtering and trace-derived output bytes.
