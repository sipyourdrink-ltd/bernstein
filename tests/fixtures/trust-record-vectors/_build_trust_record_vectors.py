#!/usr/bin/env python3
"""Re-mint the trust-record test vectors in this directory (issues #4760-#4764).

Run by hand from a source checkout, never by the test suite::

    uv run python tests/fixtures/trust-record-vectors/_build_trust_record_vectors.py

Records two real runs through the actual ``EventJournal`` write path (not a
hand-built journal): a single-execution Trust Record for the first, and a
delegated parent+child pair (linked by ``delegation.parent_record_hash``)
for the second. A fourth, run-level aggregate record (issue #4763) is then
built over the parent+child pair -- not from a journal, since there is none
for "the whole run" as such. All four are written alongside the
deterministic Ed25519 key that signed them.

Why the vectors are committed rather than generated at test time
------------------------------------------------------------------
Mirrors ``tests/fixtures/receipt-vectors/_build_audit_receipt_vectors.py``: a
generator that drifts along with the encoder cannot detect the drift.
``tests/unit/test_trust_record_format_vectors.py`` re-verifies the
*committed* signatures and field surface with today's encoder; regenerating
inside that test would move both sides of the comparison at once and prove
nothing.

Running this script is therefore a deliberate re-mint, not a reproduction.
Re-mint only when the Trust Record format itself changed, and review the
result as new evidence -- the diff cannot tell you which part of it was the
encoder.

Determinism
-----------
Unlike the audit-receipt generator, this one is required to produce
byte-identical output across repeated runs (issues #4760-#4762 acceptance
criteria): a Trust Record signs over ``iat``, which is sourced from the
journal's own wall-clock ``ts`` field. ``EventJournal.record`` stamps that
field with ``time.time()``, which would make every re-mint differ for no
reason connected to the format. This script therefore monkeypatches
``time.time`` for the duration of journal writing to a fixed, deterministic
clock (one synthetic second per event) -- the only wall-clock stand-in
anywhere in this file, and it is never real wall-clock. Signing itself
(Ed25519) and JSON canonicalisation are already deterministic given
deterministic inputs, so freezing the clock is the only thing needed.
``build_provenance.digest`` is a second, separate deterministic stand-in
(see ``_FIXTURE_BUILD_DIGEST`` below): the real installed-build digest
varies by environment and would break reproducibility across machines, not
just across runs.
"""

from __future__ import annotations

import hashlib
import itertools
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.observability.trust_record import TrustRecordEmitter
from bernstein.core.replay.journal import EventJournal

# Deterministic constants -- never reused outside this fixture generator.
_SIGN_SEED = b"k" * 32
_INSTALL_REV = "fixturefixture01"

#: Deterministic stand-in for ``build_provenance.digest``, derived from
#: fixture content (this file's own docstring marker) rather than the
#: installed build -- never a wall-clock artefact, and stable across every
#: machine that re-mints these vectors.
_FIXTURE_BUILD_DIGEST = f"sha256:{hashlib.sha256(b'trust-record-vector-fixture-build').hexdigest()}"

#: Frozen fixture clock: one synthetic second per ``time.time()`` call,
#: starting at a fixed epoch. Never real wall-clock.
_FIXTURE_EPOCH_START = 1_700_000_000.0

OUT_DIR = Path(__file__).resolve().parent


def _frozen_clock() -> mock._patch:
    """Patch ``time.time`` (as seen by ``EventJournal``) to a fixed, deterministic sequence."""
    counter = itertools.count()
    return mock.patch(
        "bernstein.core.replay.journal.time.time",
        side_effect=lambda: _FIXTURE_EPOCH_START + next(counter),
    )


def main() -> None:
    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    emitter = TrustRecordEmitter(
        install_rev_getter=lambda: _INSTALL_REV,
        get_private_key_pem=lambda: private_pem,
        get_installed_digest=lambda: _FIXTURE_BUILD_DIGEST,
    )

    run_id = "trust-record-vector-run"

    with tempfile.TemporaryDirectory() as tmp, _frozen_clock():
        sdd_dir = Path(tmp) / ".sdd"

        # 1. Single-execution run: a real 4-event journal through the
        # production EventJournal write path, including one tool call and
        # one produced artifact so the solo vector exercises the full
        # references[]/tool_transcript surface, not just the empty case.
        solo_journal = EventJournal("trust-record-vector-solo", sdd_dir)
        solo_journal.record(
            "run_started",
            role="backend",
            model_provider="anthropic",
            model_id="claude-sonnet-5",
            data_class="internal",
            gate_config={"rules": ["deny-network", "deny-exfil"], "version": 1},
        )
        solo_journal.record("tool_call", tool="fs.read", args={"path": "README.md"})
        solo_journal.record(
            "artifact_produced",
            artifact_id="solo-report",
            resolver="urn:bernstein:artifacts",
            digest=f"sha256:{hashlib.sha256(b'solo-report').hexdigest()}",
        )
        solo_journal.record("run_completed", status="ok")

        solo_output = emitter.emit_trust_record(solo_journal.path, run_id, "trust-record-vector-solo")
        solo_path = OUT_DIR / "single-execution-trust-record.json"
        solo_path.write_text(solo_output + "\n", encoding="utf-8")
        print(f"Wrote single-execution vector: {solo_path}  ({len(solo_output)} bytes)")

        # 2. Delegated pair: a parent run that spawns a child, one Trust
        # Record per execution hop, linked by delegation.parent_record_hash.
        # The two hops run under two DIFFERENT resolved gate configs (the
        # child's is a narrowed subset of the parent's), so their
        # policy.bundle_hash values differ -- pinning that a delegated hop
        # is not assumed to inherit its parent's policy bundle verbatim.
        parent_journal = EventJournal("trust-record-vector-parent", sdd_dir)
        parent_journal.record(
            "run_started",
            role="orchestrator",
            model_provider="anthropic",
            model_id="claude-sonnet-5",
            data_class="internal",
            gate_config={"rules": ["deny-network", "deny-exfil", "deny-shell"], "version": 1},
        )
        parent_journal.record("agent_spawned", agent="child-1")

        parent_output = emitter.emit_trust_record(parent_journal.path, run_id, "trust-record-vector-parent")
        parent_path = OUT_DIR / "delegated-parent-trust-record.json"
        parent_path.write_text(parent_output + "\n", encoding="utf-8")
        print(f"Wrote delegated-parent vector: {parent_path}  ({len(parent_output)} bytes)")

        child_journal = EventJournal("trust-record-vector-child", sdd_dir)
        child_journal.record(
            "run_started",
            role="backend",
            model_provider="anthropic",
            model_id="claude-haiku-5",
            data_class="internal",
            # Narrowed relative to the parent's bundle: a delegated hop runs
            # under a subset of its delegator's rules, never a superset.
            gate_config={"rules": ["deny-network", "deny-exfil"], "version": 1},
        )
        child_journal.record("run_completed", status="ok")

        child_output = emitter.emit_trust_record(
            child_journal.path,
            run_id,
            "trust-record-vector-child",
            parent_record=parent_output,
            # Scoped below the parent's own (implicit, unbounded) authority --
            # the credential id names the narrower grant this hop acted under.
            credential_id="trust-record-vector-delegation-credential:scope=narrow",
        )
        child_path = OUT_DIR / "delegated-child-trust-record.json"
        child_path.write_text(child_output + "\n", encoding="utf-8")
        print(f"Wrote delegated-child vector: {child_path}  ({len(child_output)} bytes)")

        # 2b. Grandchild hop: extends the delegation chain to depth 2 so the
        # depth_exceeded rule has a bound to cross (issue #4782). Carries a
        # data_class narrowed below the child's, so data_class_widened has a
        # change of direction to detect rather than an absence of values.
        grandchild_journal = EventJournal("trust-record-vector-grandchild", sdd_dir)
        grandchild_journal.record(
            "run_started",
            role="backend",
            model_provider="anthropic",
            model_id="claude-haiku-5",
            data_class="restricted",
            gate_config={"rules": ["deny-network"], "version": 1},
        )
        grandchild_journal.record("run_completed", status="ok")

        grandchild_output = emitter.emit_trust_record(
            grandchild_journal.path,
            run_id,
            "trust-record-vector-grandchild",
            parent_record=child_output,
            credential_id="trust-record-vector-delegation-credential:scope=restricted",
        )
        grandchild_path = OUT_DIR / "delegated-grandchild-trust-record.json"
        grandchild_path.write_text(grandchild_output + "\n", encoding="utf-8")
        print(f"Wrote delegated-grandchild vector: {grandchild_path}  ({len(grandchild_output)} bytes)")

        # 3. Run-level aggregate: rolls up the parent + child + grandchild
        # execution records under one run-scoped record (issue #4763, #4782).
        # Built from the three already-minted member records, not from a
        # journal -- there is no journal for "the whole run" as such. Extended
        # to cover all three hops so the aggregate's references[] surface
        # exercises the full delegation chain, not just the first two hops.
        aggregate_output = emitter.emit_aggregate_trust_record(run_id, [parent_output, child_output, grandchild_output])
        aggregate_path = OUT_DIR / "aggregate-trust-record.json"
        aggregate_path.write_text(aggregate_output + "\n", encoding="utf-8")
        print(f"Wrote run-level aggregate vector: {aggregate_path}  ({len(aggregate_output)} bytes)")

    # 3. Public key PEM -- pinned alongside as a second, independent check
    # that it agrees with the key recoverable from cnf.jwk. Not required
    # for verification itself.
    pubkey_path = OUT_DIR / "trust-record-vectors-key.pem"
    pubkey_path.write_bytes(public_pem)
    print(f"Wrote public key: {pubkey_path}")


if __name__ == "__main__":
    main()
