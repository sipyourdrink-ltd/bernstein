"""Five deterministic probes of the real benchmark-envelope verifier.

The inner keys and receipts are PUBLIC, throwaway fixtures, not install
identity, journal evidence, or measurements of agent/runtime isolation.
The outer benchmark bundle uses the ordinary signing and trust flow.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.signer import AgentCardSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

VERSION = "self-conformance-bench-v1"
_EXPECTED = {
    "receipt-byte-flip": VerificationStatus.DIVERGED,
    "signed-result-reorder": VerificationStatus.UNSIGNED,
    "result-deletion": VerificationStatus.COVERAGE_MISMATCH,
    "old-context-receipt": VerificationStatus.DIVERGED,
    "signer-trust-change": VerificationStatus.UNSIGNED,
}
_EXPECTED_TASKS = {
    "receipt-byte-flip": (VerificationStatus.HASH_MISMATCH, VerificationStatus.MATCH),
    "signed-result-reorder": (VerificationStatus.MATCH, VerificationStatus.MATCH),
    "result-deletion": (VerificationStatus.MATCH,),
    "old-context-receipt": (VerificationStatus.MATCH, VerificationStatus.FABRICATED_SCORE),
    "signer-trust-change": (VerificationStatus.MATCH, VerificationStatus.MATCH),
}


def build_self_conformance_bench_suite() -> BenchSuite:
    """Measure only benchmark-envelope conformance, with a positive control per probe."""
    return BenchSuite(
        version=VERSION,
        tasks=[
            BenchTask(
                id=case,
                description=f"Verify a valid fixture, then detect {case} in its benchmark envelope.",
                steps=("verify untouched control", f"verify {case} fixture"),
                assertions=({"kind": "bench-envelope", "probe_status": status.value},),
                category="self_conformance_bench",
            )
            for case, status in _EXPECTED.items()
        ],
    )


def _fixture_signer() -> AgentCardSigner:
    # Reproducible PUBLIC fixture material. Never used as an outer signer or
    # added to an installation trust store.
    seed = hashlib.sha256(b"PUBLIC self-conformance-bench-v1 fixture key").digest()
    private = Ed25519PrivateKey.from_private_bytes(seed)
    return AgentCardSigner(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
    )


def _fixture_suite() -> BenchSuite:
    return BenchSuite(
        version="self-conformance-envelope-fixture-v1",
        tasks=[
            BenchTask(
                id=name,
                description=f"Typed receipt bound to fixture context {name}.",
                steps=("check fixture context",),
                assertions=({"kind": "fixture-context", "expected": name},),
                category="fixture",
            )
            for name in ("fixture-a", "fixture-b")
        ],
    )


class _ContextAdapter:
    """Explicit fixture semantics used by the production verifier's replay step."""

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        passed = receipt == {"fixture_context": task.id, "value": "accepted"}
        return passed, float(passed), {"fixture_context_matches": passed}

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        return {"fixture_context": task.id, "value": "accepted"}


def _payload(case: str) -> dict[str, Any]:
    suite = _fixture_suite()
    signer = _fixture_signer()
    control = signer.sign(
        SubmissionBundle(
            suite_hash=suite.suite_hash,
            suite_version=suite.version,
            task_results=[
                TaskResult(task.id, task.content_hash(), _ContextAdapter().run_task(task, {}), True, 1.0)
                for task in suite.tasks
            ],
            scheduler_config={"fixture_schema": 1},
            # The INNER fixture has no wall clock: no normalization of an
            # observed result or signature is needed for replay.
            submitted_at=0.0,
        )
    )
    # Recompute the public content hash after mutation, keeping the original
    # signature unless noted below. This exercises the verifier beyond the
    # serialized loader's separate stored-bundle-hash check.
    probe = replace(control, task_results=copy.deepcopy(control.task_results))
    if case == "receipt-byte-flip":
        probe.task_results[0].receipt["value"] = "bccepted"
    elif case == "signed-result-reorder":
        probe.task_results.reverse()
    elif case == "result-deletion":
        probe.task_results.pop()
    elif case == "old-context-receipt":
        result = probe.task_results[1]
        result.receipt = copy.deepcopy(probe.task_results[0].receipt)
        result.stored_receipt_hash = TaskResult._compute_receipt_hash(result.receipt)
    if case in {"result-deletion", "old-context-receipt"}:
        # Deliberately valid signatures isolate coverage and receipt replay
        # checks from the signature check. This is not attacker authority.
        probe = signer.sign(probe)
    return {"schema": 1, "case": case, "control": control.to_dict(), "probe": probe.to_dict()}


class SelfConformanceBenchReplayAdapter:
    """Replay the actual verifier; a claimed pass flag never earns a score."""

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        self._validate_task(task)
        return _payload(task.id)

    @staticmethod
    def _validate_task(task: BenchTask) -> None:
        expected = next((item for item in build_self_conformance_bench_suite().tasks if item.id == task.id), None)
        if expected is None or expected.content_hash() != task.content_hash():
            raise ValueError("Task is not a canonical benchmark-envelope conformance probe")

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        self._validate_task(task)
        # Bind the mutation, control and schema to the versioned case; an
        # arbitrary invalid envelope is not evidence for this measurement.
        try:
            supplied = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
            expected = json.dumps(_payload(task.id), sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return False, 0.0, {"error": "Invalid conformance fixture receipt"}
        if supplied != expected:
            return False, 0.0, {"error": "Noncanonical conformance fixture receipt"}
        signer = _fixture_signer()
        keys = {signer.fingerprint(): signer.public_key_pem()}
        control = BenchVerifier(_fixture_suite(), _ContextAdapter(), trusted_keys=keys).verify(
            SubmissionBundle.from_dict(receipt["control"])
        )
        probe = BenchVerifier(
            _fixture_suite(),
            _ContextAdapter(),
            trusted_keys={} if task.id == "signer-trust-change" else keys,
        ).verify(SubmissionBundle.from_dict(receipt["probe"]))
        passed = (
            control.status == VerificationStatus.MATCH
            and probe.status == _EXPECTED[task.id]
            and tuple(item.status for item in probe.task_results) == _EXPECTED_TASKS[task.id]
        )
        evidence = {
            "scope": "benchmark-envelope-fixtures-only",
            "control_status": control.status.value,
            "probe_status": probe.status.value,
            "probe_task_statuses": [item.status.value for item in probe.task_results],
        }
        return passed, float(passed), evidence
