"""bernstein-bench: runnable, reproducibility-gated evaluation harness."""

from bernstein.eval.bench.authority_levels import (
    AuthorityAction,
    AuthorityLevel,
    AuthorityReceipt,
    ContainmentOutcome,
    evaluate_authority_action,
    evaluate_subtask_delegation,
    verify_authority_receipt,
)
from bernstein.eval.bench.authority_suite import (
    AuthorityContainmentSummary,
    build_authority_suite_v1,
    summarize_containment,
)
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult, harness_fingerprint
from bernstein.eval.bench.contamination import (
    ContaminationVerdict,
    admit_task,
    check_solution_contamination,
    extract_ngrams,
)
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.leaderboard import Leaderboard, LeaderboardEntry
from bernstein.eval.bench.reliability import (
    InstallIdentityReliabilitySigner,
    ReliabilityCheckResult,
    ReliabilityReceipt,
    ReliabilityRunner,
    ReliabilityVerificationResult,
    ReliabilityVerificationStatus,
    ReliabilityVerifier,
    StubReliabilitySigner,
    TaskReliabilityResult,
    TaskReliabilityVerification,
    coordination_hash,
    coordination_projection,
    first_divergent_coordination_field,
    reliability_check,
    validate_run_receipt,
)
from bernstein.eval.bench.rotation import (
    RotationStatus,
    check_suite_saturation,
)
from bernstein.eval.bench.runner import (
    BenchRunner,
    HoldoutBenchRunner,
    HoldoutIsolationError,
    MockReplayAdapter,
    ReplayAdapter,
    StochasticMockReplayAdapter,
)
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.tool_surface_suite import (
    ToolSurfaceReplayAdapter,
    build_tool_surface_suite,
)
from bernstein.eval.bench.verifier import (
    BenchVerifier,
    BundleVerificationResult,
    TaskVerificationResult,
    VerificationStatus,
)

__all__ = [
    "AuthorityAction",
    "AuthorityContainmentSummary",
    "AuthorityLevel",
    "AuthorityReceipt",
    "BenchRunner",
    "BenchSuite",
    "BenchTask",
    "BenchVerifier",
    "BundleVerificationResult",
    "ContainmentOutcome",
    "ContaminationVerdict",
    "HoldoutBenchRunner",
    "HoldoutIsolationError",
    "InstallIdentityReliabilitySigner",
    "Leaderboard",
    "LeaderboardEntry",
    "MockReplayAdapter",
    "ReliabilityCheckResult",
    "ReliabilityReceipt",
    "ReliabilityRunner",
    "ReliabilityVerificationResult",
    "ReliabilityVerificationStatus",
    "ReliabilityVerifier",
    "ReplayAdapter",
    "RotationStatus",
    "StochasticMockReplayAdapter",
    "StubReliabilitySigner",
    "SubmissionBundle",
    "TaskReliabilityResult",
    "TaskReliabilityVerification",
    "TaskResult",
    "TaskVerificationResult",
    "ToolSurfaceReplayAdapter",
    "VerificationStatus",
    "admit_task",
    "build_authority_suite_v1",
    "build_golden_suite_v1",
    "build_tool_surface_suite",
    "check_solution_contamination",
    "check_suite_saturation",
    "coordination_hash",
    "coordination_projection",
    "evaluate_authority_action",
    "evaluate_subtask_delegation",
    "extract_ngrams",
    "first_divergent_coordination_field",
    "harness_fingerprint",
    "reliability_check",
    "summarize_containment",
    "validate_run_receipt",
    "verify_authority_receipt",
]
