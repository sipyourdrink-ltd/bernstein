## POSIX regression coverage for the transitive `filelock` 4.2.1 security update

Added a POSIX regression and lock-pin test suite
(`tests/unit/security/test_filelock_posix_regression.py`) guarding the
transitive `filelock` 4.2.1 upgrade (#6788) against `CVE-2026-112940` /
`GHSA-v4x8-3wr5-3wq8`: `ReadWriteLock` refuses symlink lock paths without
touching or creating sentinel targets, isolates SQLite from planted `-wal` /
`-journal` side files in shared directories, rejects WAL-mode lock databases,
and preserves private-directory `ReadWriteLock`, `FileLock`, and `SoftFileLock`
compatibility while keeping `filelock` transitive. Windows advisories
`CVE-2026-112907` and `CVE-2026-112913` remain covered by the `>= 4.2.1` pin
without claiming POSIX reproduction of Windows oplock or SQLite side-file
behavior (#6759).
