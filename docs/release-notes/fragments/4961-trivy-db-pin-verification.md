## Feed-pinned Trivy scans verify the database they were pinned to

A feed-pinned Trivy scan used to record whichever database digest the caller
supplied, without checking it against the database Trivy would actually load.
The adapter now hashes `<cache-dir>/db/trivy.db` before the scan and compares
it to the pin, so a record can no longer assert a database that was never
present.

Two failure modes are reported separately, because they are opposite
situations: a missing database raises `TrivyError` naming the path to download
to, and a database whose bytes do not match the pin raises `TrivyError` naming
both the expected and the observed digest. Both fail before Trivy runs, so a
mismatched scan produces no findings and no record.

The scope key is `scope.config["db_pin"]`, and the recorded invocation hash now
binds the observed `db_identity` rather than the caller's assertion. #3618
