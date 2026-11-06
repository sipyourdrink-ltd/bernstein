"""Cross-tier verification contract (#6559).

Every backend tier -- edge, search, research, sandbox -- produces a different
kind of artifact and needs a different verification strategy. The contract
defined here gives each tier a uniform interface: accept the task result,
produce a structured verdict (pass / fail / retry) with evidence, and
record the outcome in lineage.

:class:`VerifyContract` is the abstract base. Each tier implements
:meth:`verify` and :meth:`retry_policy`; the coordinator calls
:func:`run_verify` which handles the retry loop, evidence hashing, and
lineage recording uniformly.

Design decisions:

* The verdict is a frozen dataclass, not a dict -- callers get type-checked
  field access, and a verdict that someone rewrites in place is not a verdict.
* Evidence is an opaque ``dict[str, Any]`` hashed with canonical JSON so the
  lineage record carries a content-addressed handle to what the verifier saw,
  not a copy of it.
* ``retry`` is a first-class outcome distinct from ``fail``. A transient
  check failure (flaky test, model timeout) should not burn the task's budget
  when the retry policy says another attempt is expected to succeed.
* The contract is generic enough for the fleet coordinator (#6562) to call
  without knowing which tier it is driving.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "Verdict",
    "VerifyContract",
    "VerifyVerdict",
    "verify_evidence_hash",
]


class Verdict(StrEnum):
    """Closed set of verification outcomes."""

    PASS = "pass"
    FAIL = "fail"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class VerifyVerdict:
    """Structured result of running a tier's verify step.

    Attributes:
        verdict: One of pass / fail / retry.
        tier: The dispatch tier that produced the artifact being verified.
        backend: The backend identifier that ran the task.
        evidence: Tier-specific evidence payload (test output, schema errors,
            judge scores, command stderr). Opaque to the coordinator; the
            tier's verifier decides what to record.
        evidence_hash: Content-addressed hash of *evidence*, computed via
            :func:`verify_evidence_hash` so any two machines hashing the
            same evidence agree byte-for-byte.
        reason: Human-readable explanation of why the verdict was reached.
            Empty on a clean pass.
        elapsed_s: Wall-clock seconds the verify step took.
        attempt: Which attempt produced this verdict (1-indexed).
        timestamp: Unix timestamp when the verdict was sealed.
    """

    verdict: Verdict
    tier: str
    backend: str
    evidence: dict[str, Any] = field(default_factory=dict)
    evidence_hash: str = ""
    reason: str = ""
    elapsed_s: float = 0.0
    attempt: int = 1
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        """Whether the verification passed."""
        return self.verdict is Verdict.PASS

    @property
    def should_retry(self) -> bool:
        """Whether the coordinator should attempt the task again."""
        return self.verdict is Verdict.RETRY


def verify_evidence_hash(evidence: dict[str, Any]) -> str:
    """Compute a canonical content hash of the evidence payload.

    Canonical means sorted keys, minimal separators, UTF-8 -- same convention
    as :func:`bernstein.core.quality.verifier_ladder.canonical_hash` and the
    lineage spine, so hashes from different modules are interoperable.
    """
    payload = json.dumps(
        evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class VerifyContract(ABC):
    """Abstract base for tier-specific verification.

    Subclasses implement :meth:`verify` with the tier's actual check logic
    and :meth:`retry_policy` to say how many retries the tier allows and with
    what backoff.
    """

    @property
    @abstractmethod
    def tier(self) -> str:
        """The dispatch tier this verifier handles (e.g. ``"edge"``)."""

    @abstractmethod
    def verify(
        self,
        task_result: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> VerifyVerdict:
        """Run verification on *task_result* and return a structured verdict.

        Parameters:
            task_result: The tier-specific result payload. Each tier defines
                what keys it expects (diff content, search results, research
                report, etc.).
            attempt: Current attempt number (1-indexed). Verifiers may adjust
                strictness on later attempts.
        """

    @abstractmethod
    def max_retries(self) -> int:
        """Maximum number of retry attempts before escalating to fail."""

    def retry_delay_s(self, attempt: int) -> float:
        """Seconds to wait before retry attempt *attempt*.

        Default: linear backoff starting at 1s. Override for tier-specific
        policies (e.g. exponential for research, zero for edge).
        """
        return float(attempt)
