## OSV-Scanner adapter, pinned to a local database

`get_scanner("osv-scanner")` returns a feed-pinned SCA adapter. It runs
`osv-scanner scan source --offline` against a local OSV database
(`OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY`), never against osv.dev, whose answer
changes as advisories are published. The caller pins the database in
`scope.config["db_pin"]`. The adapter digests every `osv-scanner/<ecosystem>/all.zip`
under the root and refuses to scan when the digest differs, as the Trivy
adapter does for `trivy.db`.

A finding is the vulnerability id, the lockfile path and the affected
package, with severity from the advisory's CVSS score. Any `osv-scanner.toml`
ignore list in the target tree feeds the recorded invocation hash.
