#!/usr/bin/env python3
"""Emit a deterministic TRACE v0.2 trust record from the conformance fixture journal.

Usage::

    uv run python scripts/trust_record_conformance_emit.py > tr.json

The output is byte-reproducible across runs and environments because all
three injectable dependencies are pinned:

- ``install_rev`` (the key id in ``cnf.jwk.kid``) is the literal
  ``_FIXTURE_INSTALL_REV`` string.
- ``private_key_pem`` is the deterministic Ed25519 key seeded by
  ``_FIXTURE_KEY_SEED``, never the install keystore's own key.
- ``installed_digest`` (``build_provenance.digest``) is a fixed stand-in
  derived from fixture content, never the real installed-build digest.

The journal's hash chain is verified by the emitter before it trusts the
journal content, so this script fails closed on a corrupted fixture.

The fixture clock is frozen by the generator script that produced the
journal; the emitter itself never reads wall-clock, so ``iat`` and
``appraisal.timestamp`` are sourced from the journal's own ``ts`` field
and are stable across runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from bernstein.core.observability.trust_record import TrustRecordEmitter  # noqa: E402

#: Deterministic constants -- never reused outside this fixture script.
_SIGN_SEED = b"k" * 32
_INSTALL_REV = "fixturefixture01"

#: Deterministic stand-in for ``build_provenance.digest``, derived from
#: fixture content rather than the installed build -- stable across every
#: machine that runs this script.
_FIXTURE_BUILD_DIGEST = "sha256:" + "0" * 64

JOURNAL_PATH = REPO_ROOT / "tests/fixtures/trust-record-conformance/journal.jsonl"


def main() -> int:
    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    emitter = TrustRecordEmitter(
        install_rev_getter=lambda: _INSTALL_REV,
        get_private_key_pem=lambda: private_pem,
        get_installed_digest=lambda: _FIXTURE_BUILD_DIGEST,
    )

    # ``exec_id`` is recovered from the journal path's own directory name
    # (``journal_path.parent.name``): the fixture lives at
    # ``tests/fixtures/trust-record-conformance/journal.jsonl``, so the
    # exec id is ``trust-record-conformance``. ``run_id`` is the wider run
    # grouping identifier and is caller-supplied here.
    exec_id = JOURNAL_PATH.parent.name
    run_id = "trust-record-conformance"

    record = emitter.emit_trust_record(JOURNAL_PATH, run_id, exec_id)

    # No trailing newline: stdout is piped to trace-tests, which reads the
    # record file as-is.
    sys.stdout.write(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
