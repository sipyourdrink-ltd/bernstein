"""Volunteer-workers substrate: opt-in project policy and its trust anchor.

A project joins the volunteer program by committing one file,
``.bernstein/volunteer.json``.  That file is the project's declared policy:
which gates a submission must pass, which paths a patch may touch, which
hosts the sandbox may reach, and how long a task may run.

Both artefacts here are content-addressed, and the digests chain:

    manifest bytes -> manifest digest -> sandbox profile digest -> receipt

A result receipt carries the manifest digest as ``manifest_sha256`` and the
profile digest as its sandbox identifier, so a maintainer verifying a
volunteer's submission can prove the volunteer ran *the policy this project
declared* behind *the containment that policy implies* -- without taking
anyone's word for either.
"""

from __future__ import annotations

from bernstein.core.volunteer.claim import (
    CLAIM_MARKER,
    DEFAULT_CLAIM_STALENESS,
    ClaimClient,
    ClaimComment,
    IssueClaimState,
    SkipDecision,
    SkipReason,
    find_own_claim,
    repo_slug,
    should_skip,
)
from bernstein.core.volunteer.issue_sanitize import (
    ISSUE_TEXT_FENCE_LABEL,
    normalize_untrusted_text,
    sanitize_issue_text,
    strip_html_comments,
)
from bernstein.core.volunteer.manifest import (
    OSI_APPROVED_LICENSES,
    SUPPORTED_SCHEMA_VERSIONS,
    VOLUNTEER_MANIFEST_PATH,
    GateCommand,
    UnenforcedManifestFieldWarning,
    VolunteerManifest,
    VolunteerManifestError,
    canonical_manifest_bytes,
    load_manifest,
    load_manifest_from_repo,
    manifest_digest,
)
from bernstein.core.volunteer.runner import (
    ALLOWED_REPO_SCHEMES,
    HOST_GIT_ENV_PASSTHROUGH,
    AgentArgvBuilder,
    AgentInvocation,
    ClaimedTask,
    DonorLimits,
    IssueTextSanitizer,
    RefusalStage,
    TaskDiff,
    TaskOutcome,
    TaskRefusal,
    WallClockBudget,
    build_prompt,
    host_git_env,
    mock_agent_argv,
    repo_url_problem,
    run_claimed_task,
)
from bernstein.core.volunteer.sandbox_profile import (
    BACKEND_PREFERENCE,
    PACKAGE_REGISTRY_HOSTS,
    SANDBOX_ENV_ALLOWLIST,
    VOLUNTEER_PROFILE_NAME,
    SandboxProfileRefusal,
    VolunteerSandboxProfile,
    backend_options,
    build_volunteer_profile,
    describe_refusal,
    effective_egress,
    profile_matches,
    sandbox_env,
)
from bernstein.core.volunteer.task_finish import (
    REFUSAL_REASONS,
    SignedResultBundle,
    TaskProvenance,
    VolunteerRefusal,
    enforce_allowed_paths,
    finish_volunteer_task,
)
from bernstein.core.volunteer.wall_clock import (
    TERM_GRACE_SECONDS,
    WallClockOutcome,
    run_under_wall_clock,
)

__all__ = [
    "ALLOWED_REPO_SCHEMES",
    "BACKEND_PREFERENCE",
    "CLAIM_MARKER",
    "DEFAULT_CLAIM_STALENESS",
    "HOST_GIT_ENV_PASSTHROUGH",
    "ISSUE_TEXT_FENCE_LABEL",
    "OSI_APPROVED_LICENSES",
    "PACKAGE_REGISTRY_HOSTS",
    "REFUSAL_REASONS",
    "SANDBOX_ENV_ALLOWLIST",
    "SUPPORTED_SCHEMA_VERSIONS",
    "TERM_GRACE_SECONDS",
    "VOLUNTEER_MANIFEST_PATH",
    "VOLUNTEER_PROFILE_NAME",
    "AgentArgvBuilder",
    "AgentInvocation",
    "ClaimClient",
    "ClaimComment",
    "ClaimedTask",
    "DonorLimits",
    "GateCommand",
    "IssueClaimState",
    "IssueTextSanitizer",
    "RefusalStage",
    "SandboxProfileRefusal",
    "SignedResultBundle",
    "SkipDecision",
    "SkipReason",
    "TaskDiff",
    "TaskOutcome",
    "TaskProvenance",
    "TaskRefusal",
    "UnenforcedManifestFieldWarning",
    "VolunteerManifest",
    "VolunteerManifestError",
    "VolunteerRefusal",
    "VolunteerSandboxProfile",
    "WallClockBudget",
    "WallClockOutcome",
    "backend_options",
    "build_prompt",
    "build_volunteer_profile",
    "canonical_manifest_bytes",
    "describe_refusal",
    "effective_egress",
    "enforce_allowed_paths",
    "find_own_claim",
    "finish_volunteer_task",
    "host_git_env",
    "load_manifest",
    "load_manifest_from_repo",
    "manifest_digest",
    "mock_agent_argv",
    "normalize_untrusted_text",
    "profile_matches",
    "repo_slug",
    "repo_url_problem",
    "run_claimed_task",
    "run_under_wall_clock",
    "sandbox_env",
    "sanitize_issue_text",
    "should_skip",
    "strip_html_comments",
]
