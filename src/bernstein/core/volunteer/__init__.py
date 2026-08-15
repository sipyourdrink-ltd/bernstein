"""Volunteer-workers substrate: opt-in project policy and its trust anchor.

A project joins the volunteer program by committing one file,
``.bernstein/volunteer.json``.  That file is the project's declared policy:
which gates a submission must pass, which paths a patch may touch, which
hosts the sandbox may reach, and how long a task may run.

The manifest is content-addressed.  Its canonical digest is the value a
result receipt carries as ``manifest_sha256``, so a maintainer verifying a
volunteer's submission can prove the volunteer ran *the policy this project
declared at that revision* rather than a policy the volunteer chose.
"""

from __future__ import annotations

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

__all__ = [
    "OSI_APPROVED_LICENSES",
    "SUPPORTED_SCHEMA_VERSIONS",
    "VOLUNTEER_MANIFEST_PATH",
    "GateCommand",
    "UnenforcedManifestFieldWarning",
    "VolunteerManifest",
    "VolunteerManifestError",
    "canonical_manifest_bytes",
    "load_manifest",
    "load_manifest_from_repo",
    "manifest_digest",
]
