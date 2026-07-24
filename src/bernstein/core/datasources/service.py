"""High-level wiring: build a receipt store + registry rooted at ``.sdd``.

Keeps the CLI (and any task-spawn integration) free of key-management detail.
The signing identity and operator HMAC key are provisioned exactly like the
rest of the lineage subsystem: a persisted Ed25519 keypair under
``<sdd>/datasources/identity`` and the shared audit key from
:func:`bernstein.core.security.audit.load_or_create_audit_key`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.datasources.connection import ConnectionRegistry
from bernstein.core.datasources.engine import DEFAULT_ROW_CAP
from bernstein.core.datasources.receipt import QueryReceiptStore
from bernstein.core.datasources.result import render_text
from bernstein.core.lineage.identity import AgentCard, load_or_create_signing_identity

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: Stable identity for receipt signatures.
_DS_AGENT_ID = "agent:datasource-receipts"
_DS_KID = "datasource-receipts-1"
_PRIVATE_KEY_NAME = "datasource_lineage.pem"
_PUBLIC_KEY_NAME = "datasource_lineage.pub"


def datasources_root(sdd_dir: Path) -> Path:
    """Return the datasource subsystem root under ``sdd_dir``."""
    return sdd_dir / "datasources"


def build_connection_registry(sdd_dir: Path) -> ConnectionRegistry:
    """Return the connection registry rooted at ``<sdd>/datasources``."""
    return ConnectionRegistry(datasources_root(sdd_dir))


def build_receipt_store(sdd_dir: Path) -> QueryReceiptStore:
    """Return a :class:`QueryReceiptStore` with provisioned key material.

    On first use this creates the Ed25519 signing identity (mode ``0600``) and
    the operator HMAC key; subsequent calls reuse them, so a receipt recorded in
    one invocation verifies in the next.
    """
    from bernstein.core.security.audit import load_or_create_audit_key

    root = datasources_root(sdd_dir)
    identity_dir = root / "identity"
    _priv, pub = load_or_create_signing_identity(
        identity_dir,
        private_name=_PRIVATE_KEY_NAME,
        public_name=_PUBLIC_KEY_NAME,
    )
    card = AgentCard(agent_id=_DS_AGENT_ID, kid=_DS_KID, public_key_pem=pub)
    return QueryReceiptStore(
        root,
        agent_card=card,
        private_key_pem=_priv,
        operator_hmac_key=load_or_create_audit_key(),
    )


@dataclass(frozen=True, slots=True)
class TaskDatasourceInput:
    """A datasource input a task declares: run this query, ground on the result.

    Attributes:
        connection_id: Registered connection to query.
        query: Read-only SQL to execute.
        params: Optional positional bind parameters.
        row_cap: Optional per-receipt row cap (defaults to the engine cap).
    """

    connection_id: str
    query: str
    params: list[object] = field(default_factory=list[object])
    row_cap: int = DEFAULT_ROW_CAP


@dataclass(frozen=True, slots=True)
class ResolvedDatasourceInput:
    """One resolved datasource input: the receipt plus the prompt-ready text.

    The spawn path injects ``prompt_text`` into the task context and records
    ``receipt_id`` on the task, so every downstream artifact can reference which
    result set grounded it. ``content_hash`` is the digest of the exact bytes.
    """

    connection_id: str
    receipt_id: str
    content_hash: str
    prompt_text: str
    row_count: int
    truncated: bool


def resolve_task_datasource_inputs(
    sdd_dir: Path,
    inputs: Sequence[TaskDatasourceInput],
    *,
    store_result_copy: bool = True,
) -> list[ResolvedDatasourceInput]:
    """Execute each declared datasource input and record its query receipt.

    This is the task-integration entry point: given the inputs a task declares,
    it runs each read-only query, canonicalises the result, records a signed
    receipt, and returns the prompt-ready text rendering alongside the receipt
    id. Any DML/DDL input surfaces the same typed error the CLI raises -- a task
    can never smuggle a write through a declared input.
    """
    registry = build_connection_registry(sdd_dir)
    store = build_receipt_store(sdd_dir)
    resolved: list[ResolvedDatasourceInput] = []
    for spec in inputs:
        connection = registry.get(spec.connection_id)
        engine = connection.open_engine()
        params = list(spec.params) if spec.params else None
        result = engine.execute(spec.query, params, row_cap=spec.row_cap)
        receipt = store.record(
            connection=connection,
            query_text=spec.query,
            params=params,
            result=result,
            store_result_copy=store_result_copy,
        )
        resolved.append(
            ResolvedDatasourceInput(
                connection_id=spec.connection_id,
                receipt_id=receipt.receipt_id,
                content_hash=receipt.content_hash,
                prompt_text=render_text(result),
                row_count=receipt.row_count,
                truncated=receipt.truncated,
            )
        )
    return resolved


__all__ = [
    "ResolvedDatasourceInput",
    "TaskDatasourceInput",
    "build_connection_registry",
    "build_receipt_store",
    "datasources_root",
    "resolve_task_datasource_inputs",
]
