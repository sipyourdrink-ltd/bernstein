Expose all existing CMP policy-library observations through `govern audit
--only CMP` and include identical workspace findings in `compliance check`
JSON. Policy snapshots now use project observations unless explicitly
overridden by declaration flags. Framework profiles mark required check IDs
without changing verdicts or asserting conformance.

CMP execution errors retain their check area and use the shared registry's
exception isolation on both JSON surfaces. Under `govern audit --strict`, a
crashed CMP check returns exit 1 rather than the incompleteness exit 2.
