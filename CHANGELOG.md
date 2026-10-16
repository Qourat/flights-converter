# Changelog

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
