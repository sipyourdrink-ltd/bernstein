## Abandonment rows holding U+2028 are no longer dropped

The abandonment ledger read its JSONL file with `str.splitlines()`, which also
splits on U+2028 and U+2029. A row with a literal line separator in `detail`
became two invalid halves, so `bernstein abandonments list` omitted it and
`stats` counted one row too few. The ledger now splits on LF only, and a
malformed row is still skipped (#7960).
