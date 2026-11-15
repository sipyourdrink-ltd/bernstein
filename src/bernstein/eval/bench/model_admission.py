"""Offline qualification of operator-selected model admission evidence (#5038).

The suite, scoring adapter, trusted keys and qualified task classes are trusted
operator inputs. None is taken from the submitted bundle's scheduler config.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from bernstein.eval.bench.admission_subject import task_class_set
from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.verifier import BenchVerifier

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bernstein.eval.bench.runner import ReplayAdapter
    from bernstein.eval.bench.suite import BenchSuite


@dataclass(frozen=True)
class ModelAdmissionEvidence:
    """Evidence plus the independent operator qualification context.

    ``qualified_task_classes`` declares what the operator-selected suite
    qualifies. A signed bundle cannot authorize its own suite or task scope.
    There is no stub-signature or synthetic-scoring escape hatch.
    """

    bundle: SubmissionBundle
    suite: BenchSuite
    adapter: ReplayAdapter
    trusted_keys: Mapping[str, bytes]
    qualified_task_classes: tuple[str, ...]

    def verify(self, *, provider: str, model: str, version: str | None, task_classes: Sequence[str]) -> dict[str, Any]:
        """Raise ValueError on any mismatch; otherwise return audit-bound evidence."""
        # Round-trip into a private snapshot: mutable bundles cache their hash.
        # Reusing an earlier cached hash after mutation must never retain trust.
        if type(self.bundle.schema_version) is not int or self.bundle.schema_version != 3:
            raise ValueError("model admission requires bundle schema_version 3")
        bundle = SubmissionBundle.from_dict(json.loads(json.dumps(self.bundle.to_dict())))
        subject = bundle.admission_subject
        if subject is None:
            raise ValueError("model admission requires a signed admission_subject")
        ref = subject.model_ref
        if (ref.provider, ref.model_requested, ref.version) != (provider, model, version):
            raise ValueError("admission subject does not match the requested model_ref exactly")
        if ref.model_reported is not None and ref.model_reported != model:
            raise ValueError("admission subject reports a different model identity")
        classes = task_class_set(task_classes)
        qualified = task_class_set(self.qualified_task_classes)
        # Finite evidence must not expand to '*' accidentally. A blanket scope
        # requires an explicit wildcard in BOTH independent declarations.
        for declared, label in ((subject.task_classes, "signed subject"), (qualified, "qualified suite")):
            if "*" not in declared and not set(classes).issubset(declared):
                raise ValueError(f"requested task classes are not covered by the {label}")
        if getattr(self.adapter, "synthetic", False) or bundle.scheduler_config.get("adapter") == "mock":
            raise ValueError("synthetic benchmark results cannot qualify model admission")
        # Reset the operator suite's cached hash too; current tasks, not stale
        # identity from a prior read, must match the signed suite commitment.
        suite = replace(self.suite, tasks=deepcopy(self.suite.tasks))
        if suite.holdout_hash or suite.holdout_tasks or bundle.holdout_hash:
            raise ValueError("model admission does not support unreplayed holdout tasks")
        if len({task.id for task in suite.tasks}) != len(suite.tasks):
            raise ValueError("qualified suite must have unique task IDs")
        if bundle.suite_version != suite.version:
            raise ValueError("bundle does not match the qualified suite version")
        result = BenchVerifier(suite=suite, adapter=self.adapter, trusted_keys=self.trusted_keys).verify(bundle)
        if not result.passed:
            raise ValueError(f"model admission evidence failed verification: {result.status.value}: {result.detail}")
        # MATCH establishes receipt integrity, and legitimately includes failed
        # tasks. Admission requires the selected suite to have actually passed.
        if not result.task_results or any(task.replayed_passed is not True for task in result.task_results):
            raise ValueError("model admission requires every qualified suite task to pass")
        return {
            "schema_version": 1,
            "bundle_hash": result.bundle_hash,
            "suite_hash": result.suite_hash,
            "signer_fingerprint": bundle.signer_fingerprint,
            "admission_subject": subject.to_dict(),
            "qualified_task_classes": list(qualified),
        }
