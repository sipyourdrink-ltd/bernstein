## Pin an external key when verifying an authority envelope

`bernstein-verify-envelope verify` accepts `--jwk` or `--public-key` to pin a
signing key obtained out of band. With a key pinned, the pinned key becomes
the verifying key and an envelope re-signed by any other key is rejected
instead of passing; an attacker who can replace the whole file can no longer
also replace the key it carries and still pass. With no pin, verification
still falls back to the key the envelope carries.

Every run now prints a `TRUST:` line, and the machine-readable result on
stderr carries a `trust` field: `pinned-jwk` / `pinned-public-key` when a pin
was checked, `trust-on-first-use` when the pass only shows the file was not
edited after signing, not who signed it. Passing both flags is refused rather
than one being silently ignored. Where the pinned key comes from is still the
operator's problem; the envelope does not solve key distribution. (#5064)
