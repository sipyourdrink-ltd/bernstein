"""Tests for Issue #5028: tenant_id defaults to 'default' removal and UNSPECIFIED_TENANT sentinel."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.cli.commands.chat_cmd import _ChatTaskRequest
from bernstein.core.cost.cost_tracker import TokenUsage
from bernstein.core.identity.agent_jwt import AgentCredential
from bernstein.core.security.tenant_isolation import TenantIsolationManager
from bernstein.core.security.tenanting import (
    DEFAULT_TENANT_ID,
    InvalidTenantIdError,
    normalize_tenant_id,
)

# Import UNSPECIFIED_TENANT if already defined, or sentinel dummy for pre-implementation testing
try:
    from bernstein.core.security.tenanting import UNSPECIFIED_TENANT
except ImportError:
    UNSPECIFIED_TENANT = object()

if TYPE_CHECKING:
    from pathlib import Path


def test_record_without_tenant_is_not_attributed_to_the_default_tenant() -> None:
    """A record constructed without tenant_id must not be attributed to 'default'."""
    cred = AgentCredential(token_hash="hash-123")
    assert cred.tenant_id != DEFAULT_TENANT_ID
    assert cred.tenant_id is UNSPECIFIED_TENANT

    usage = TokenUsage(
        input_tokens=10,
        output_tokens=20,
        model="sonnet",
        cost_usd=0.05,
        agent_id="agent-1",
        task_id="task-1",
    )
    assert usage.tenant_id != DEFAULT_TENANT_ID
    assert usage.tenant_id is UNSPECIFIED_TENANT


def test_isolation_check_fails_closed_on_unspecified_tenant(tmp_path: Path) -> None:
    """Isolation checks must fail closed when encountering the unspecified sentinel."""
    with pytest.raises(InvalidTenantIdError):
        normalize_tenant_id(UNSPECIFIED_TENANT)  # type: ignore[arg-type]

    mgr = TenantIsolationManager(tmp_path / ".sdd")
    with pytest.raises(InvalidTenantIdError):
        mgr.get_context(UNSPECIFIED_TENANT)  # type: ignore[arg-type]

    @dataclass
    class FakeTask:
        tenant_id: Any = UNSPECIFIED_TENANT

    tasks = {
        "t1": FakeTask(tenant_id=UNSPECIFIED_TENANT),
        "t2": FakeTask(tenant_id="default"),
    }
    filtered = mgr.filter_tasks(tasks, "default")
    assert "t1" not in filtered
    assert set(filtered.keys()) == {"t2"}


def test_explicit_default_tenant_and_omitted_tenant_produce_different_records() -> None:
    """Distinguishing test: explicit default tenant and omitted tenant produce different records."""
    cred_explicit = AgentCredential(token_hash="hash-123", tenant_id="default")
    cred_omitted = AgentCredential(token_hash="hash-123")
    assert cred_explicit != cred_omitted
    assert cred_explicit.tenant_id != cred_omitted.tenant_id

    usage_explicit = TokenUsage(
        input_tokens=10,
        output_tokens=20,
        model="sonnet",
        cost_usd=0.05,
        agent_id="agent-1",
        task_id="task-1",
        tenant_id="default",
    )
    usage_omitted = TokenUsage(
        input_tokens=10,
        output_tokens=20,
        model="sonnet",
        cost_usd=0.05,
        agent_id="agent-1",
        task_id="task-1",
    )
    assert usage_explicit != usage_omitted
    assert usage_explicit.tenant_id != usage_omitted.tenant_id


def test_no_tenant_id_field_declares_a_string_default() -> None:
    """Static assertion that no targeted dataclass declares a string default for tenant_id."""
    target_classes = [AgentCredential, TokenUsage, _ChatTaskRequest]
    for cls in target_classes:
        for f in fields(cls):
            if f.name == "tenant_id":
                assert not isinstance(f.default, str), (
                    f"{cls.__name__}.tenant_id declares a string default: {f.default!r}"
                )
