## OSV-Scanner adapter, pinned to a local database

`get_scanner("osv-scanner")` returns a feed-pinned SCA adapter. It runs
`osv-scanner scan source --offline` against a local OSV database
(`OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY`), never against osv.dev, whose answer
changes as advisories are published. The caller pins the database in
`scope.config["db_pin"]`. The adapter digests every `osv-scanner/<ecosystem>/all.zip`
under the root and refuses to scan when the digest differs, as the Trivy
adapter does for `trivy.db`.

Finding identity includes the vulnerability id, normalized lockfile path,
severity, summary and affected package metadata. Windows drive and UNC file
URIs use the shared path normalizer so checkout locations do not change
in-scope finding hashes. Any `osv-scanner.toml` ignore list in the target tree
feeds the recorded invocation hash. Include/exclude globs and max-depth scope
limits are refused rather than ignored. Partial implementation of #2958.

SARIF reports that explicitly mark any invocation as unsuccessful are
rejected, including reports with no findings. Absent invocation metadata
and successful invocations preserve the existing finding normalization.

Config identity uses the versioned `osv-scanner-config-v2` digest of sorted
target-relative POSIX paths and exact file bytes: moving an ignore list between
package directories changes invocation identity, while moving the entire checkout
does not. Before any vendor process, the adapter parses TOML and rejects explicit
`IgnoredVulns.ignoreUntil` and `PackageOverrides.effectiveUntil` fields (including
case variants), because OSV-Scanner evaluates them against the current time.
Timeless configs and temporal words in comments or reasons remain supported;
unreadable or invalid TOML configs are refused rather than left unvalidated.
