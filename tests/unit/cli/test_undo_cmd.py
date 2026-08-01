"""Tests for ``bernstein.cli.commands.undo_cmd`` audit logging.

``_log_undo_audit`` must append a ``git.undo`` entry to whichever ``AuditLog``
is currently wired via ``bernstein.core.tasks.lifecycle.set_audit_log``. It
gets there through ``get_audit_log``, which the module previously imported
from ``bernstein.core.lifecycle`` - a compatibility alias
(``bernstein/core/lifecycle/__init__.py`` re-points
``sys.modules["bernstein.core.lifecycle"]`` at the real
``bernstein.core.tasks.lifecycle`` module) rather than the module's actual
home. The alias keeps this working at runtime, but static analysis can't see
through it (``mypy`` flags ``attr-defined`` on the import), and the
surrounding ``contextlib.suppress(Exception)`` would have silently eaten any
future failure to resolve it. These tests pin the observable behavior -
importing from the canonical module and writing the audit entry - so a
regression here is caught instead of swallowed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bernstein.cli.commands.undo_cmd import _log_undo_audit
from bernstein.core.tasks.lifecycle import set_audit_log


def test_log_undo_audit_writes_entry() -> None:
    """undo must append a ``git.undo`` entry to the wired audit log."""
    mock_audit = MagicMock()
    set_audit_log(mock_audit)
    try:
        _log_undo_audit("task-123", False, 2)
    finally:
        set_audit_log(None)

    assert mock_audit.log.called, "undo did not write an audit entry"
    _args, kwargs = mock_audit.log.call_args
    assert kwargs["event_type"] == "git.undo"
    assert kwargs["resource_id"] == "task-123"
    assert kwargs["details"]["commit_count"] == 2
    assert kwargs["details"]["revert_all"] is False


def test_log_undo_audit_writes_entry_for_revert_all() -> None:
    """The ``--all`` path logs too, with ``revert_all`` reflected in details."""
    mock_audit = MagicMock()
    set_audit_log(mock_audit)
    try:
        _log_undo_audit(None, True, 5)
    finally:
        set_audit_log(None)

    assert mock_audit.log.called, "undo --all did not write an audit entry"
    _args, kwargs = mock_audit.log.call_args
    assert kwargs["resource_id"] == "all"
    assert kwargs["details"]["commit_count"] == 5
    assert kwargs["details"]["revert_all"] is True


def test_log_undo_audit_noop_when_no_audit_log_wired() -> None:
    """No audit log configured means no crash and simply nothing to write."""
    set_audit_log(None)  # ensure clean baseline
    _log_undo_audit("task-789", False, 1)  # must not raise


def test_log_undo_audit_swallows_backend_failure() -> None:
    """A misbehaving audit backend must not fail the undo command itself."""
    mock_audit = MagicMock()
    mock_audit.log.side_effect = RuntimeError("audit backend unavailable")
    set_audit_log(mock_audit)
    try:
        _log_undo_audit("task-999", False, 1)  # must not raise
    finally:
        set_audit_log(None)
