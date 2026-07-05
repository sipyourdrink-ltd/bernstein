"""Named team manifests - pinned bundles of roles, model policies, and response profiles.

See :mod:`bernstein.core.teams.manifest` for the format and canonical
serialization rules, :mod:`bernstein.core.teams.drift` for role template
drift detection, :mod:`bernstein.core.teams.lockfile` for ``teams.lock``,
and :mod:`bernstein.core.teams.audit` for the audit-chain anchoring.
"""

from bernstein.core.teams.audit import (
    EVENT_DRIFT,
    EVENT_RESOLVE,
    TeamManifestAuditor,
    record_run_team_manifest,
)
from bernstein.core.teams.drift import (
    MISSING_TEMPLATE,
    RoleDriftFinding,
    classify_role_template_drift,
    compute_role_digests,
    detect_role_template_drift,
    resolve_roles_dir,
    role_template_digest,
)
from bernstein.core.teams.lockfile import (
    TEAMS_LOCK_FILENAME,
    TeamLockEntry,
    TeamLockState,
    read_state,
    upsert_team_pin,
    write_state,
)
from bernstein.core.teams.manifest import (
    TEAM_MANIFEST_DIR_NAME,
    ExpandedTeam,
    TeamCoordination,
    TeamManifest,
    TeamManifestDigestMismatchError,
    TeamManifestError,
    TeamManifestNotFoundError,
    TeamManifestSignatureError,
    TeamManifestValidationError,
    TeamRoleSpec,
    canonical_toml,
    discover_team_manifest_paths,
    expand_manifest,
    load_team_manifest,
    parse_manifest_ref,
    resolve_team_manifest,
    sign_team_manifest,
    team_manifest_search_dirs,
    verify_team_manifest,
)

__all__ = [
    "EVENT_DRIFT",
    "EVENT_RESOLVE",
    "MISSING_TEMPLATE",
    "TEAMS_LOCK_FILENAME",
    "TEAM_MANIFEST_DIR_NAME",
    "ExpandedTeam",
    "RoleDriftFinding",
    "TeamCoordination",
    "TeamLockEntry",
    "TeamLockState",
    "TeamManifest",
    "TeamManifestAuditor",
    "TeamManifestDigestMismatchError",
    "TeamManifestError",
    "TeamManifestNotFoundError",
    "TeamManifestSignatureError",
    "TeamManifestValidationError",
    "TeamRoleSpec",
    "canonical_toml",
    "classify_role_template_drift",
    "compute_role_digests",
    "detect_role_template_drift",
    "discover_team_manifest_paths",
    "expand_manifest",
    "load_team_manifest",
    "parse_manifest_ref",
    "read_state",
    "record_run_team_manifest",
    "resolve_roles_dir",
    "resolve_team_manifest",
    "role_template_digest",
    "sign_team_manifest",
    "team_manifest_search_dirs",
    "upsert_team_pin",
    "verify_team_manifest",
    "write_state",
]
