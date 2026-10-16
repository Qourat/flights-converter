# Changelog

## 0.1.1 — 2026-10-16

- Skip prod-0 ACAS/TCAS advisory files matching `acas/*.csv.gz` without
  decompression or parsing. Keep the complete non-trace allowlist in
  `flights_converter/trace.py`, including existing heatmap and metadata rules.
  Unknown members continue to reject the release.
- Add aggregate `skipped_members` and `skipped_bytes` counters to the run summary.
  Bytes are stored payload sizes, excluding tar headers and padding; no member
  names or contents are emitted.
- Preserve trace parsing, privacy filtering and trace-derived output bytes.
