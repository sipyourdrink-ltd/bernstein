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
from bernstein.core.volunteer.wall_clock import (
    TERM_GRACE_SECONDS,
    WallClockOutcome,
    run_under_wall_clock,
)

__all__ = [
    "BACKEND_PREFERENCE",
    "ISSUE_TEXT_FENCE_LABEL",
    "OSI_APPROVED_LICENSES",
    "PACKAGE_REGISTRY_HOSTS",
    "SANDBOX_ENV_ALLOWLIST",
    "SUPPORTED_SCHEMA_VERSIONS",
    "TERM_GRACE_SECONDS",
    "VOLUNTEER_MANIFEST_PATH",
    "VOLUNTEER_PROFILE_NAME",
    "GateCommand",
    "SandboxProfileRefusal",
    "UnenforcedManifestFieldWarning",
    "VolunteerManifest",
    "VolunteerManifestError",
    "VolunteerSandboxProfile",
    "WallClockOutcome",
    "backend_options",
    "build_volunteer_profile",
    "canonical_manifest_bytes",
    "describe_refusal",
    "effective_egress",
    "load_manifest",
    "load_manifest_from_repo",
    "manifest_digest",
    "normalize_untrusted_text",
    "profile_matches",
    "run_under_wall_clock",
    "sandbox_env",
    "sanitize_issue_text",
    "strip_html_comments",
]
