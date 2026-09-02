"""SCIM 2.0 provisioning resources mapped onto agent principals (issue #4972).

An agent that outlives its purpose keeps its grants. Directories solved that
for humans with provisioning and deprovisioning; the mechanism transfers, and
the identity it operates on is ours. This adapter is the whole translation
layer: it turns the standard provisioning resources of RFC 7643 into calls on
:class:`bernstein.core.identity.principals.PrincipalLedger`.

Mapping
-------

======================  ==========================================
SCIM ``User``           Bernstein principal
``userName``            ``principal_id`` (falls back to ``externalId``, ``id``)
``externalId``          ``external_id`` (the directory's own id)
``displayName``         ``display_name``
``groups[].display``    one capability each, sorted and de-duplicated
``active``              ``true`` provisions, ``false`` deprovisions
======================  ==========================================

A SCIM ``Group`` is a capability: its ``displayName`` is the capability name
and its ``members`` are the principals that hold it. No third schema is
introduced, no vendor SDK is imported, and nothing here reaches the network --
the caller owns the transport and hands this module the decoded resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.identity.principals import PrincipalLedger, PrincipalReceipt

__all__ = [
    "GROUP_SCHEMA",
    "USER_SCHEMA",
    "DirectoryPrincipal",
    "DirectorySchemaError",
    "apply_user",
    "capability_from_group",
    "deprovision_user",
    "principal_from_user",
    "principals_in_group",
]

#: RFC 7643 core resource schema URIs.
USER_SCHEMA: Final[str] = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA: Final[str] = "urn:ietf:params:scim:schemas:core:2.0:Group"

#: Reason recorded when the directory deactivates or deletes the user.
DEACTIVATED_REASON: Final[str] = "directory_deactivated"


class DirectorySchemaError(ValueError):
    """Raised when a provisioning resource cannot be mapped onto a principal."""


@dataclass(frozen=True)
class DirectoryPrincipal:
    """A SCIM ``User`` resource expressed in Bernstein's principal vocabulary."""

    principal_id: str
    external_id: str
    display_name: str
    capability_ceiling: tuple[str, ...]
    active: bool


def _text(resource: Mapping[str, Any], key: str) -> str:
    value = resource.get(key)
    return str(value).strip() if isinstance(value, str | int) else ""


def _ceiling(groups: Iterable[Any]) -> tuple[str, ...]:
    """Return the sorted, de-duplicated capability names the groups imply."""
    names: set[str] = set()
    for entry in groups:
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            member = cast("Mapping[str, Any]", entry)
            name = _text(member, "display") or _text(member, "value")
        else:  # pragma: no cover - defensive
            name = ""
        if name:
            names.add(name)
    return tuple(sorted(names))


def principal_from_user(resource: Mapping[str, Any]) -> DirectoryPrincipal:
    """Map a SCIM ``User`` resource onto a :class:`DirectoryPrincipal`.

    Args:
        resource: The decoded SCIM ``User`` resource.

    Returns:
        The principal the resource describes, with the capability ceiling its
        group memberships imply.

    Raises:
        DirectorySchemaError: The resource carries no usable identifier.
    """
    principal_id = _text(resource, "userName") or _text(resource, "externalId") or _text(resource, "id")
    if not principal_id:
        raise DirectorySchemaError("SCIM User resource carries no userName, externalId, or id")
    groups = resource.get("groups")
    return DirectoryPrincipal(
        principal_id=principal_id,
        external_id=_text(resource, "externalId"),
        display_name=_text(resource, "displayName"),
        capability_ceiling=_ceiling(cast("list[Any]", groups) if isinstance(groups, list) else ()),
        active=bool(resource.get("active", True)),
    )


def capability_from_group(resource: Mapping[str, Any]) -> str:
    """Return the capability name a SCIM ``Group`` resource stands for.

    Raises:
        DirectorySchemaError: The group carries neither ``displayName`` nor ``id``.
    """
    name = _text(resource, "displayName") or _text(resource, "id")
    if not name:
        raise DirectorySchemaError("SCIM Group resource carries no displayName or id")
    return name


def principals_in_group(resource: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the principal ids the group's ``members`` name, in resource order."""
    members = resource.get("members")
    if not isinstance(members, list):
        return ()
    out: list[str] = []
    for entry in cast("list[Any]", members):
        if not isinstance(entry, dict):
            continue
        member = cast("Mapping[str, Any]", entry)
        name = _text(member, "display") or _text(member, "value")
        if name and name not in out:
            out.append(name)
    return tuple(out)


def apply_user(
    ledger: PrincipalLedger,
    resource: Mapping[str, Any],
    *,
    created: int | None = None,
) -> PrincipalReceipt:
    """Record the lifecycle event a SCIM ``User`` resource implies.

    ``active: true`` (the RFC default) appends a ``principal_provisioned``
    record carrying the ceiling the directory implies; ``active: false``
    appends a ``principal_deprovisioned`` record, which is what the grant
    validity path consults from then on.

    Args:
        ledger: The principal ledger to append to.
        resource: The decoded SCIM ``User`` resource.
        created: Optional unix timestamp (exposed for deterministic tests).

    Returns:
        The appended :class:`~bernstein.core.identity.principals.PrincipalReceipt`;
        its ``kind`` is :data:`~bernstein.core.identity.principals.PRINCIPAL_PROVISIONED`
        or :data:`~bernstein.core.identity.principals.PRINCIPAL_DEPROVISIONED`.
    """
    principal = principal_from_user(resource)
    if not principal.active:
        return deprovision_user(ledger, principal.principal_id, created=created)
    return ledger.provision(
        principal_id=principal.principal_id,
        display_name=principal.display_name,
        capability_ceiling=principal.capability_ceiling,
        external_id=principal.external_id,
        created=created,
    )


def deprovision_user(
    ledger: PrincipalLedger,
    principal_id: str,
    *,
    reason: str = DEACTIVATED_REASON,
    created: int | None = None,
) -> PrincipalReceipt:
    """Record the deprovision a SCIM delete (or deactivation) implies."""
    if not principal_id:
        raise DirectorySchemaError("cannot deprovision without a principal id")
    return ledger.deprovision(principal_id=principal_id, reason=reason, created=created)
