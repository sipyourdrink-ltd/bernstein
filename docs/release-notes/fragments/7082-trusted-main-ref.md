## Pin privileged release checkouts to main history

Release, SBOM and tunnel callers verify the selected ref in a read-only job
and pin privileged checkouts to the verified main-history commit (#7082).
