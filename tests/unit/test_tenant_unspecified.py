"""An omitted tenant is not the tenant named ``default`` (issue #5028).

The records in this suite are signed and replayed as attribution: a cost row
says whose spend it was, a credential says which scope it authenticates. A
field default turns "the caller never said" into "the caller said
``default``", and the two are then indistinguishable in the record and in
every rollup built from it.

These tests pin the distinction itself rather than any one call site: a
record written without a tenant carries the unspecified marker, the marker
is refused everywhere a real tenant is required, and the two records - one
omitting the tenant, one naming ``default`` - do not serialise the same.
"""

from __future__ import annotations

import dataclasses
import inspect
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.tenant_isolation import TenantIsolationManager
from bernstein.core.tenanting import (
    DEFAULT_TENANT_ID,
    UNSPECIFIED_TENANT_ID,
    InvalidTenantIdError,
    is_unspecified_tenant,
    normalize_tenant_id,
    try_normalize_tenant_id,
)

from bernstein.cli.commands import chat_cmd
from bernstein.core.agents import agent_identity
from bernstein.core.agents.agent_identity import AgentCredential
from bernstein.core.cost import cost_tracker
from bernstein.core.cost.cost_tracker import TokenUsage, _usage_tenant_scope

if TYPE_CHECKING:
    from pathlib import Path


def _usage(**overrides: Any) -> TokenUsage:
    """Build a usage row, leaving the tenant to the field default."""
    fields: dict[str, Any] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "model": "sonnet",
        "cost_usd": 0.01,
        "agent_id": "agent-1",
        "task_id": "task-1",
        "timestamp": 1.0,
    }
    fields.update(overrides)
    return TokenUsage(**fields)


class TestOmittedTenantIsNotTheDefaultTenant:
    """A record nobody gave a tenant does not claim to belong to one."""

    def test_record_without_tenant_is_not_attributed_to_the_default_tenant(self) -> None:
        """Neither the in-memory record nor the persisted one names ``default``.

        Both directions matter. The field default decides what a caller that
        forgot the tenant writes; the deserialiser decides what a record
        stored before the field existed is read back as. Either one answering
        ``default`` re-introduces the ambiguity the other removed.
        """
        credential = AgentCredential(token_hash="abc")
        assert credential.tenant_id == UNSPECIFIED_TENANT_ID
        assert credential.tenant_id != DEFAULT_TENANT_ID

        usage = _usage()
        assert usage.tenant_id == UNSPECIFIED_TENANT_ID
        assert usage.tenant_id != DEFAULT_TENANT_ID

        # Persisted records with no ``tenant_id`` key at all.
        assert AgentCredential.from_dict({"token_hash": "abc"}).tenant_id == UNSPECIFIED_TENANT_ID
        assert TokenUsage.from_dict(usage.to_dict() | {"tenant_id": None}).tenant_id == UNSPECIFIED_TENANT_ID
        assert _usage_tenant_scope(None) == UNSPECIFIED_TENANT_ID

    def test_unspecified_marker_survives_a_round_trip(self) -> None:
        """The marker is data, not an error, on the paths that record it.

        A usage row read back as unreadable would be dropped from the replay
        and the run would under-report its own spend, so the marker has to
        deserialise as itself rather than be refused with the malformed
        values.
        """
        stored = _usage().to_dict()
        assert stored["tenant_id"] == UNSPECIFIED_TENANT_ID
        assert TokenUsage.from_dict(stored).tenant_id == UNSPECIFIED_TENANT_ID
        assert _usage_tenant_scope(stored["tenant_id"]) == UNSPECIFIED_TENANT_ID

        credential = AgentCredential(token_hash="abc")
        assert AgentCredential.from_dict(credential.to_dict()).tenant_id == UNSPECIFIED_TENANT_ID

    def test_explicit_default_tenant_and_omitted_tenant_produce_different_records(self) -> None:
        """The distinguishing test: the two serialise differently.

        A caller that genuinely runs in the ``default`` tenant and a caller
        that never threaded one through produce records an auditor can tell
        apart. If these compare equal the signature is attesting to a fact
        nobody asserted.
        """
        omitted = AgentCredential(token_hash="abc")
        asserted = AgentCredential(token_hash="abc", tenant_id=DEFAULT_TENANT_ID)
        assert omitted.to_dict() != asserted.to_dict()
        assert omitted.to_dict()["tenant_id"] != asserted.to_dict()["tenant_id"]

        omitted_usage = _usage()
        asserted_usage = _usage(tenant_id=DEFAULT_TENANT_ID)
        assert omitted_usage.to_dict() != asserted_usage.to_dict()
        assert omitted_usage.to_dict()["tenant_id"] != asserted_usage.to_dict()["tenant_id"]


class TestTheMarkerIsNotATenant:
    """Everywhere a real tenant is required, the marker is refused."""

    def test_normalize_refuses_the_unspecified_marker(self) -> None:
        with pytest.raises(InvalidTenantIdError, match="unspecified"):
            normalize_tenant_id(UNSPECIFIED_TENANT_ID)

    def test_marker_never_normalizes_into_a_tenant(self) -> None:
        """A stored marker is a row no reader can attribute, not a tenant."""
        assert try_normalize_tenant_id(UNSPECIFIED_TENANT_ID) is None
        assert is_unspecified_tenant(UNSPECIFIED_TENANT_ID)
        assert not is_unspecified_tenant(DEFAULT_TENANT_ID)

    def test_isolation_check_fails_closed_on_unspecified_tenant(self, tmp_path: Path) -> None:
        """The isolation manager refuses the marker instead of scoping it.

        Fail-closed here means two things: no tenant subtree is created for
        the marker, and a record carrying it is not handed to any tenant's
        view - including the default tenant's, which is the one it would
        previously have collapsed into.
        """
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        manager = TenantIsolationManager(sdd_dir)

        with pytest.raises(InvalidTenantIdError):
            manager.get_context(UNSPECIFIED_TENANT_ID)
        assert not (sdd_dir / UNSPECIFIED_TENANT_ID).exists()

        with pytest.raises(InvalidTenantIdError):
            manager.register_quota(UNSPECIFIED_TENANT_ID, manager.get_context(DEFAULT_TENANT_ID).quota)

        @dataclasses.dataclass
        class _Task:
            tenant_id: str

        tasks: dict[str, Any] = {
            "unattributed": _Task(tenant_id=UNSPECIFIED_TENANT_ID),
            "attributed": _Task(tenant_id=DEFAULT_TENANT_ID),
        }
        assert manager.filter_tasks(tasks, DEFAULT_TENANT_ID) == {"attributed": tasks["attributed"]}

        # A task object with no tenant attribute at all is unattributed too.
        assert manager.filter_tasks({"no-field": object()}, DEFAULT_TENANT_ID) == {}


class TestNoDeclaredStringDefault:
    """Static guard so a fourth site cannot reintroduce the silent default."""

    @staticmethod
    def _tenant_defaults(module: ModuleType) -> list[tuple[str, Any]]:
        """Return ``(qualname, default)`` for every ``tenant_id`` field."""
        found: list[tuple[str, Any]] = []
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if not dataclasses.is_dataclass(obj) or obj.__module__ != module.__name__:
                continue
            for field in dataclasses.fields(obj):
                if field.name == "tenant_id":
                    found.append((f"{module.__name__}.{name}", field.default))
        return found

    def test_no_tenant_id_field_declares_a_string_default(self) -> None:
        """No dataclass in these modules defaults ``tenant_id`` to a tenant name.

        Scoped to the three modules this change owns. A field that has no
        default forces the caller to say which tenant it means; a field that
        defaults to the marker says the caller did not. Anything else is a
        tenant name nobody asserted.
        """
        modules = (agent_identity, cost_tracker, chat_cmd)
        declared = [entry for module in modules for entry in self._tenant_defaults(module)]
        assert declared, "expected these modules to declare tenant_id fields"
        offenders = [
            (qualname, default)
            for qualname, default in declared
            if isinstance(default, str) and default != UNSPECIFIED_TENANT_ID
        ]
        assert offenders == []
