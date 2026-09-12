## Evidence packs carry signed benchmark bundles and NIST OSCAL export

Compliance evidence packs (`bernstein compliance pack`) now embed the signed benchmark bundles found under `.sdd/bench/bundles/*.json`, byte-for-byte as `bernstein-bench` saved them, and report per control in `controls.json` whether a bundle from a suite that declares that control was found (`measured`) or not (`declared_not_measured`). A bundle that does not load -- corrupt JSON, a bundle-hash or receipt-hash mismatch -- is listed in `controls.json` under `bench_bundles_unreadable` with the reason, not dropped.

`verify_evidence_pack` checks the manifest's artefact hashes and re-runs every embedded bundle through `SubmissionBundle.from_dict`, the same hash check `bernstein-bench verify` starts with. It does **not** verify bundle signatures; nothing in the bench tooling does today, and the pack does not claim otherwise.

Operators can also export assessment results in NIST OSCAL v1.1.0 JSON via `bernstein compliance oscal [--standard <id>] [--out <file>]`. The command refuses to export over a bundle it could not read (#5456).
