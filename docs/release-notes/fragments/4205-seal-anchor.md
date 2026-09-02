## Anchor a run's sealed head to a timestamping authority

`bernstein seal publish <run>` submits a finished run's sealed journal head
to an RFC 3161 timestamping authority and stores the reply as
`.sdd/runs/<run>/seal_anchor.json`. The TSA is shown only the head digest,
never the journal, and the head is refused if the journal chain does not
verify or disagrees with the seal in the run's lineage spine.

`bernstein seal verify <run> --rfc3161-trusted-tsa-bundle <roots.pem>`
re-checks the stored anchor entirely offline: the head is recomputed from the
artifacts, the anchor must witness exactly that head, and the token must chain
to roots the operator pinned. A head that moved since anchoring reports
`mismatched`; a missing trust bundle reports `unverifiable`, never a pass.

Both commands are opt-in and neither is on any default path. `publish` opens a
socket only when `--tsa-url` is given; `--token <file>` stores a reply obtained
on another host, so an air-gapped install can anchor too (#4205).
