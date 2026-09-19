## Scanner findings report the same path on every host

Gitleaks, semgrep and trivy each carried their own copy of the same
report-path normalisation, and all three answered wrongly on Windows for the
same reason: they asked the HOST whether a reported path was absolute. A
scanner report may have been produced on another machine, and the two flavours
disagree — `/checkout/project/app.env` is absolute to POSIX and not to Windows
(no drive), while `C:/checkout/project/app.env` is absolute to Windows and, to
POSIX, a relative path whose first segment is `C:`. Each host silently declined
to relativise half the reports and returned the absolute path instead.

The three copies are now one helper, `normalize_report_path`, which asks both
flavours. A path outside the scan target is still reported as-is rather than
relativised, and the comparison is by path segment, so a sibling directory
whose name merely starts with the scan root's is not mistaken for a child.

`ScanScope.to_dict()` now serialises its roots with `as_posix()` rather than
`str()`. That dict is hashed — it reaches `CleanRunAttestation.canonical_bytes()`,
which promises cross-machine byte equality — so the same scan of the same tree
sealed a different attestation hash into the lineage spine depending on which
runner produced it.

These are the four failures the Windows unit lane was stopping at (#5787).
The lane stays advisory and `-x` stays in place; promotion is a separate,
documented change.
