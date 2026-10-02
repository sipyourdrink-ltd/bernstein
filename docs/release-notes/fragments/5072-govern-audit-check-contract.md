## 5072

Add the `bernstein govern audit` CLI command supporting `--list`, `--only <AREA>`,
and `--skip <ID>` filters, with stable ID tombstone pinning and integration tests (#5072).
Note: `bernstein govern audit` no longer runs the verifier-key staleness check;
that check is now invoked via `bernstein govern audit-keys`.
