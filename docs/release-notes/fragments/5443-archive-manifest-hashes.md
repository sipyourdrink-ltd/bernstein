## A run archive can be checked, not just opened

An unattended run on an ephemeral workspace deletes the workspace when it ends,
so for a postmortem the archive is the evidence. Its `manifest.json` recorded a
file count and a byte total, which say how much was collected and nothing about
what — an archive that lost a file in transit, or had one edited afterwards,
read as intact.

The manifest now carries a sha256 per archived member, and `verify_archive`
re-hashes the archive against it. Verification is offline and self-contained:
the manifest travels inside the archive, so nothing consults the workspace,
which by then usually does not exist.

Three failures are reported separately, because they lead to different actions:
a **modified** member is evidence that changed after the fact, a **missing**
one is evidence that did not survive, and an **unexpected** one is a member no
hash covers.

An archive written before this reports `unverifiable` rather than passing —
nothing was checked, and "nothing failed" is not "everything matched". An
archive with no readable manifest is refused outright rather than treated as an
empty one, which would have verified clean having checked nothing.

`docs/observability/run-archive-retention.md` states the contract.
