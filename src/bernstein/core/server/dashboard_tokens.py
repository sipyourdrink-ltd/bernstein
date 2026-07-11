"""Scoped dashboard tokens and startup posture (issue #2366).

The dashboard's auth surface has three parts, all projections over signed
records rather than mutable in-memory grants:

Scoped token registry
---------------------
:class:`DashboardTokenRegistry` is an append-only JSONL journal of
HMAC-signed issuance and revocation records. The raw token is printed once
at issue time and never stored -- only its SHA-256 digest lands in the
journal, so the journal can be read (and shipped in a support bundle)
without leaking a credential. Validation is a pure projection over the
journal rows: find the signed issue row whose digest matches, confirm its
HMAC under the audit-chain key, and confirm no signed revocation row
follows. Editing a row (for example widening ``viewer`` to ``operator``)
breaks the row signature and the token stops validating -- the journal is
the grant, and the grant is tamper-evident.

Role bindings
-------------
:func:`dashboard_role_bindings` maps the two token scopes onto the RBAC
roles shipped in :mod:`bernstein.core.security.governance` (#2309):
``viewer`` reads, ``operator`` reads and writes. The bindings are signed
with the same audit-chain key, so ``bernstein governance verify`` can
recompute every dashboard authz verdict offline.

Governance decisions
--------------------
:class:`DashboardGovernance` projects each authz decision through
:func:`~bernstein.core.security.governance.decide_access` into the
``dashboard-auth`` lineage-spine run and mirrors it onto the audit chain.
A denied write is a signed record, not a log line.

Startup posture
---------------
:func:`resolve_dashboard_posture` decides what a dashboard entry point must
do for a given bind host: loopback binds without auth get a generated
operator token printed at startup; non-loopback binds refuse to start until
auth is configured. There is no silent open mode on a routable interface.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import ipaddress
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.security.governance import RoleBindings, decide_access

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.governance import GovernanceDecision

logger = logging.getLogger(__name__)

#: Read-only scope: may look at every dashboard surface, may change nothing.
SCOPE_VIEWER = "viewer"

#: Read-write scope: may trigger state-changing dashboard actions.
SCOPE_OPERATOR = "operator"

#: All scopes a dashboard token can carry.
DASHBOARD_SCOPES: tuple[str, ...] = (SCOPE_VIEWER, SCOPE_OPERATOR)

#: The lineage-spine run every dashboard authz decision anchors to.
DASHBOARD_AUTH_RUN_ID = "dashboard-auth"

#: Permission string for read access to dashboard routes.
ACTION_READ = "dashboard.read"

#: Permission string for state-changing dashboard actions.
ACTION_WRITE = "dashboard.write"

#: Permission string recorded on login decisions.
ACTION_LOGIN = "dashboard.login"

#: IDP-group prefix the token scope is projected through. A validated token
#: with scope ``viewer`` authenticates the subject into the single group
#: ``dashboard-scope:viewer``; the role bindings map that group to a role.
SCOPE_GROUP_PREFIX = "dashboard-scope:"

#: Journal record schema version. Bump only on a wire-format change.
TOKEN_RECORD_VERSION = 1

#: Length of the short token id (hex chars of the digest) used for listing
#: and revocation. 16 hex chars = 64 bits: ample for a per-install registry.
_TOKEN_ID_LEN = 16


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def resolve_dashboard_hmac_key(sdd_dir: Path) -> bytes:
    """Resolve the HMAC key the dashboard auth surface signs with.

    Mirrors the task server's key resolution exactly (env override first,
    then the workspace key file), so the CLI, the serve entry points, and
    the server middleware all sign and verify against the same key.

    Args:
        sdd_dir: The workspace ``.sdd`` directory.

    Returns:
        The raw key bytes.
    """
    from bernstein.core.security.audit import load_or_create_audit_key

    override = os.environ.get("BERNSTEIN_AUDIT_KEY_PATH", "")
    return load_or_create_audit_key(Path(override) if override else sdd_dir / "keys" / "audit.key")


def dashboard_role_bindings(hmac_key: bytes) -> RoleBindings:
    """Return the signed role bindings the dashboard projects scopes through.

    Deterministic: the same key always produces byte-identical signed
    bindings, so the policy identity (``bindings_hash``) is stable across
    restarts and across operators, and ``bernstein governance verify`` can
    be pointed at a freshly recomputed copy.

    Args:
        hmac_key: The audit-chain HMAC key that signs the bindings.

    Returns:
        A signed :class:`RoleBindings` mapping ``dashboard-scope:*`` groups
        onto the ``viewer`` / ``operator`` roles.
    """
    return RoleBindings(
        group_to_role={
            f"{SCOPE_GROUP_PREFIX}{SCOPE_VIEWER}": SCOPE_VIEWER,
            f"{SCOPE_GROUP_PREFIX}{SCOPE_OPERATOR}": SCOPE_OPERATOR,
        },
        role_permissions={
            SCOPE_VIEWER: (ACTION_LOGIN, ACTION_READ),
            SCOPE_OPERATOR: (ACTION_LOGIN, ACTION_READ, ACTION_WRITE),
        },
    ).sign(hmac_key)


# ---------------------------------------------------------------------------
# Token journal records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DashboardTokenRecord:
    """One signed row of the dashboard token journal.

    Attributes:
        kind: ``issue`` or ``revoke``.
        token_id: Short hex id (digest prefix) used for listing / revocation.
        token_sha256: Hex SHA-256 of the raw token. The raw token itself is
            never stored.
        principal: The human / seat the token attributes actions to.
        scope: One of :data:`DASHBOARD_SCOPES` (empty on ``revoke`` rows).
        issued_at: Integer timestamp the row was appended at.
        signature: HMAC-SHA256 over the row body under the audit-chain key.
    """

    kind: str
    token_id: str
    token_sha256: str
    principal: str
    scope: str
    issued_at: int
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        return {
            "v": TOKEN_RECORD_VERSION,
            "kind": self.kind,
            "token_id": self.token_id,
            "token_sha256": self.token_sha256,
            "principal": self.principal,
            "scope": self.scope,
            "issued_at": self.issued_at,
        }

    def sign(self, key: bytes) -> DashboardTokenRecord:
        """Return a copy carrying the HMAC signature over the row body."""
        sig = _hmac.new(key, _canonical_bytes(self._body()), hashlib.sha256).hexdigest()
        return DashboardTokenRecord(
            kind=self.kind,
            token_id=self.token_id,
            token_sha256=self.token_sha256,
            principal=self.principal,
            scope=self.scope,
            issued_at=self.issued_at,
            signature=sig,
        )

    def verify_signature(self, key: bytes) -> bool:
        """Return True when ``signature`` matches the row body under ``key``."""
        if not self.signature:
            return False
        want = _hmac.new(key, _canonical_bytes(self._body()), hashlib.sha256).hexdigest()
        return _hmac.compare_digest(self.signature, want)

    def to_dict(self) -> dict[str, Any]:
        return self._body() | {"signature": self.signature}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> DashboardTokenRecord:
        return cls(
            kind=str(row["kind"]),
            token_id=str(row["token_id"]),
            token_sha256=str(row["token_sha256"]),
            principal=str(row.get("principal", "")),
            scope=str(row.get("scope", "")),
            issued_at=int(row["issued_at"]),
            signature=str(row.get("signature", "")),
        )


class DashboardTokenRegistry:
    """Append-only journal of signed dashboard token grants.

    Args:
        path: The journal JSONL path (created on first issue).
        hmac_key: The audit-chain HMAC key signing every row.
    """

    def __init__(self, path: Path, hmac_key: bytes) -> None:
        self._path = path
        self._key = hmac_key

    @property
    def path(self) -> Path:
        """The journal path."""
        return self._path

    @staticmethod
    def _digest(raw_token: str) -> str:
        """Hex SHA-256 of the raw token, taken verbatim (never stripped)."""
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def issue(self, *, principal: str, scope: str, now: int) -> tuple[str, DashboardTokenRecord]:
        """Generate a token, append its signed issue row, return both.

        Args:
            principal: The seat / person the token attributes actions to.
            scope: One of :data:`DASHBOARD_SCOPES`.
            now: Integer timestamp recorded on the row.

        Returns:
            ``(raw_token, signed_record)``. The raw token exists only in the
            return value -- persist it nowhere.

        Raises:
            ValueError: On an unknown scope or empty principal.
        """
        if scope not in DASHBOARD_SCOPES:
            raise ValueError(f"unknown dashboard token scope {scope!r}; expected one of {DASHBOARD_SCOPES}")
        if not principal:
            raise ValueError("dashboard token principal must be non-empty")
        raw = secrets.token_urlsafe(32)
        digest = self._digest(raw)
        record = DashboardTokenRecord(
            kind="issue",
            token_id=digest[:_TOKEN_ID_LEN],
            token_sha256=digest,
            principal=principal,
            scope=scope,
            issued_at=now,
        ).sign(self._key)
        self._append(record)
        return raw, record

    def revoke(self, token_id: str, *, now: int) -> bool:
        """Append a signed revocation row for *token_id*.

        Returns:
            True when a matching live issue row existed, False otherwise
            (nothing is appended in that case).
        """
        live = {r.token_id: r for r in self._live_issue_records()}
        target = live.get(token_id)
        if target is None:
            return False
        record = DashboardTokenRecord(
            kind="revoke",
            token_id=target.token_id,
            token_sha256=target.token_sha256,
            principal=target.principal,
            scope="",
            issued_at=now,
        ).sign(self._key)
        self._append(record)
        return True

    def validate(self, raw_token: str) -> DashboardTokenRecord | None:
        """Project *raw_token* onto its signed grant, or ``None``.

        The token is hashed verbatim -- no stripping, no normalisation -- and
        matched against signed issue rows. A row whose signature does not
        verify is ignored (a tampered grant is no grant), and a signed
        revocation row extinguishes the grant.
        """
        if not raw_token:
            return None
        digest = self._digest(raw_token)
        matched: DashboardTokenRecord | None = None
        for record in self.records():
            if not _hmac.compare_digest(record.token_sha256, digest):
                continue
            if not record.verify_signature(self._key):
                continue
            if record.kind == "issue":
                matched = record
            elif record.kind == "revoke":
                matched = None
        return matched

    def records(self) -> list[DashboardTokenRecord]:
        """Load every journal row in append order (malformed rows skipped)."""
        if not self._path.exists():
            return []
        rows: list[DashboardTokenRecord] = []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            # Key/credential-adjacent path: log the exception type only.
            logger.warning("dashboard tokens: journal unreadable (%s)", type(exc).__name__)
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(DashboardTokenRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("dashboard tokens: skipping malformed journal row (%s)", type(exc).__name__)
                continue
        return rows

    def has_tokens(self) -> bool:
        """Return True when at least one signed, unrevoked grant exists."""
        return bool(self._live_issue_records())

    def _live_issue_records(self) -> list[DashboardTokenRecord]:
        """Signed issue rows that no signed revocation row extinguishes."""
        live: dict[str, DashboardTokenRecord] = {}
        for record in self.records():
            if not record.verify_signature(self._key):
                continue
            if record.kind == "issue":
                live[record.token_sha256] = record
            elif record.kind == "revoke":
                live.pop(record.token_sha256, None)
        return list(live.values())

    def _append(self, record: DashboardTokenRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Governance projection: every authz decision is an anchored record
# ---------------------------------------------------------------------------


class DashboardGovernance:
    """Projects dashboard authz decisions into the governance spine.

    Every decision goes through :func:`decide_access` (#2309): the subject's
    scope becomes an IDP group, the signed bindings resolve it to a role,
    and the verdict is anchored in the ``dashboard-auth`` spine run. When an
    audit chain is attached, the decision is also mirrored as a
    ``governance.decision`` event, so the acting principal reaches the
    chain through the same seat-attribution path budget and RBAC decisions
    already use.

    Args:
        lineage_root: The spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key.
        audit_chain: Optional chain store the decisions mirror into.
    """

    def __init__(
        self,
        lineage_root: Path,
        hmac_key: bytes,
        audit_chain: AuditChainStore | None = None,
    ) -> None:
        self._lineage_root = lineage_root
        self._key = hmac_key
        self._chain = audit_chain
        self._bindings = dashboard_role_bindings(hmac_key)

    @property
    def bindings(self) -> RoleBindings:
        """The signed role bindings decisions project over."""
        return self._bindings

    def decide(self, *, subject: str, scope: str, action: str, now: int) -> GovernanceDecision:
        """Record one authz decision and return the anchored record.

        Args:
            subject: The acting principal (or ``anonymous``).
            scope: The validated credential scope; empty when the request
                carried no valid credential (projects to a denial).
            action: One of the ``dashboard.*`` permission strings.
            now: Integer timestamp recorded on the decision.

        Returns:
            The anchored :class:`GovernanceDecision`; ``verdict`` is
            ``allow`` or ``deny``.
        """
        groups: tuple[str, ...] = (f"{SCOPE_GROUP_PREFIX}{scope}",) if scope in DASHBOARD_SCOPES else ()
        decision = decide_access(
            run_id=DASHBOARD_AUTH_RUN_ID,
            lineage_root=self._lineage_root,
            hmac_key=self._key,
            subject=subject,
            idp_groups=groups,
            action=action,
            bindings=self._bindings,
            now=now,
        )
        if self._chain is not None:
            from bernstein.core.security.audit_chain import record_governance_decision

            record_governance_decision(
                chain=self._chain,
                subject=decision.subject,
                action=decision.action,
                verdict=decision.verdict,
                inputs_hash=decision.inputs_hash,
                journal_entry_hash=decision.journal_entry_hash,
                run_id=decision.run_id,
                actor="dashboard",
            )
        return decision


# ---------------------------------------------------------------------------
# Startup posture
# ---------------------------------------------------------------------------

DashboardPosture = Literal["configured", "generate", "refuse"]


def is_loopback_host(host: str) -> bool:
    """Return True when *host* can only be reached from this machine.

    Recognises ``localhost``, the ``127.0.0.0/8`` block, and ``::1``. A
    hostname other than ``localhost`` is treated as routable -- posture
    fails closed on names it cannot prove local.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_dashboard_posture(host: str, *, auth_configured: bool) -> DashboardPosture:
    """Decide the startup posture for a dashboard bind on *host*.

    Returns:
        ``configured`` when auth is already configured (any host);
        ``generate`` for an unconfigured loopback bind (the entry point must
        issue and print an operator token); ``refuse`` for an unconfigured
        non-loopback bind (the entry point must not start).
    """
    if auth_configured:
        return "configured"
    if is_loopback_host(host):
        return "generate"
    return "refuse"


__all__ = [
    "ACTION_LOGIN",
    "ACTION_READ",
    "ACTION_WRITE",
    "DASHBOARD_AUTH_RUN_ID",
    "DASHBOARD_SCOPES",
    "SCOPE_GROUP_PREFIX",
    "SCOPE_OPERATOR",
    "SCOPE_VIEWER",
    "TOKEN_RECORD_VERSION",
    "DashboardGovernance",
    "DashboardPosture",
    "DashboardTokenRecord",
    "DashboardTokenRegistry",
    "dashboard_role_bindings",
    "is_loopback_host",
    "resolve_dashboard_hmac_key",
    "resolve_dashboard_posture",
]
