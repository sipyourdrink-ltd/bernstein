## The DLP scanner reports every occurrence of a rule, not just the first

`DLPScanner._scan_lines` deduplicated matches per scan instead of per line: once a
rule (e.g. `us_ssn`, `icd10_code`, `spdx_identifier`) fired on one line, it was
silently skipped for every later line in the same diff or text blob. A diff
leaking two different SSNs, or introducing the same license violation in two
files, was reported once instead of twice, undercounting real violations in the
compliance record. The scan-wide dedup set is removed; each rule can now match
independently on every line, matching the behaviour of `dlp_scanner_v2.py` (#5882).

Note: this reports every *line* a rule matches on, not every occurrence within a
line — two regulated values on the same line (e.g. two SSNs on one line) still
count as one finding, since each rule uses `pattern.search()` (first match per
line), not `finditer()`. Reporting every occurrence within a line is a separate,
deliberate change left for a follow-up.
