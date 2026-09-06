"""bernstein-bench: runnable, reproducibility-gated evaluation harness."""

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.goal_drift_suite import (
    DriftContract,
    DriftStepMeasurement,
    DriftTrajectoryCurve,
    GoalDriftReplayAdapter,
    GoalDriftTask,
    build_goal_drift_suite,
    evaluate_trajectory_drift,
    get_goal_drift_task_map,
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
from bernstein.eval.bench.runner import (
    BenchRunner,
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
    "BenchRunner",
    "BenchSuite",
    "BenchTask",
    "BenchVerifier",
    "BundleVerificationResult",
    "DriftContract",
    "DriftStepMeasurement",
    "DriftTrajectoryCurve",
    "GoalDriftReplayAdapter",
    "GoalDriftTask",
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
    "StochasticMockReplayAdapter",
    "StubReliabilitySigner",
    "SubmissionBundle",
    "TaskReliabilityResult",
    "TaskReliabilityVerification",
    "TaskResult",
    "TaskVerificationResult",
    "ToolSurfaceReplayAdapter",
    "VerificationStatus",
    "build_goal_drift_suite",
    "build_golden_suite_v1",
    "build_tool_surface_suite",
    "coordination_hash",
    "coordination_projection",
    "evaluate_trajectory_drift",
    "first_divergent_coordination_field",
    "get_goal_drift_task_map",
    "reliability_check",
    "validate_run_receipt",
]
