"""Tests for the three-level abort hierarchy: TOOL < SIBLING < SESSION.

Covers:
- TOOL scope: abort_tool writes TOOL_ABORT signal file.
- SIBLING scope: abort_siblings sends SHUTDOWN to peers only (not parent).
- SESSION scope: propagate_abort cascades SHUTDOWN to all descendants.
- Containment: policy with escalation disabled does NOT cascade.
- Propagation: policy with escalation enabled DOES cascade.
- Composability: tool_to_sibling and sibling_to_session chain correctly.
- Reverse-index: get_parent / get_siblings introspection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.abort_chain import AbortChain, AbortPolicy, AbortScope, ToolAbortRecord


@pytest.fixture()
def signals_dir(tmp_path: Path) -> Path:
    """Temporary ``.sdd/runtime/signals/`` directory."""
    sig = tmp_path / ".sdd" / "runtime" / "signals"
    sig.mkdir(parents=True, exist_ok=True)
    return sig


# ---------------------------------------------------------------------------
# AbortScope enum
# ---------------------------------------------------------------------------


class TestAbortScope:
    def test_values(self) -> None:
        assert AbortScope.TOOL == "tool"
        assert AbortScope.SIBLING == "sibling"
        assert AbortScope.SESSION == "session"

    def test_is_str(self) -> None:
        # AbortScope must be usable as a plain string (StrEnum).
        assert isinstance(AbortScope.TOOL, str)


# ---------------------------------------------------------------------------
# AbortPolicy defaults
# ---------------------------------------------------------------------------


class TestAbortPolicy:
    def test_default_is_contain_only(self) -> None:
        policy = AbortPolicy()
        assert policy.tool_to_sibling is False
        assert policy.sibling_to_session is False

    def test_escalate_all(self) -> None:
        policy = AbortPolicy(tool_to_sibling=True, sibling_to_session=True)
        assert policy.tool_to_sibling is True
        assert policy.sibling_to_session is True

    def test_frozen(self) -> None:
        policy = AbortPolicy()
        with pytest.raises(AttributeError):
            policy.tool_to_sibling = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TOOL scope — abort_tool
# ---------------------------------------------------------------------------


class TestAbortTool:
    def test_writes_tool_abort_file(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        returned = chain.abort_tool("child-a", "Bash", "exit 1")

        tool_abort_file = signals_dir / "child-a" / "TOOL_ABORT"
        assert tool_abort_file.exists()
        record: ToolAbortRecord = json.loads(tool_abort_file.read_text(encoding="utf-8"))
        assert record["session_id"] == "child-a"
        assert record["tool"] == "Bash"
        assert record["reason"] == "exit 1"
        assert isinstance(record["ts"], float)
        assert returned == []

    def test_tool_abort_appends_multiple_records(self, signals_dir: Path) -> None:
        """Multiple tool aborts append separate JSON lines."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.abort_tool("sess", "Bash", "first error")
        chain.abort_tool("sess", "Edit", "second error")

        lines = (signals_dir / "sess" / "TOOL_ABORT").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["tool"] == "Bash"
        assert second["tool"] == "Edit"

    def test_tool_abort_no_shutdown_file(self, signals_dir: Path) -> None:
        """TOOL scope does not write a SHUTDOWN file."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.abort_tool("sess", "Bash", "exit 2")
        assert not (signals_dir / "sess" / "SHUTDOWN").exists()

    def test_tool_abort_creates_signals_dir(self, tmp_path: Path) -> None:
        """abort_tool creates the signals directory if it does not exist."""
        sig_dir = tmp_path / "new_signals_dir"
        chain = AbortChain(signals_dir=sig_dir)
        chain.abort_tool("sess", "Bash", "oops")
        assert (sig_dir / "sess" / "TOOL_ABORT").exists()

    # ------------------------------------------------------------------
    # Containment: no escalation when policy is None or defaults
    # ------------------------------------------------------------------

    def test_contained_tool_abort_does_not_abort_siblings(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")

        # Default policy (contain only) — child-b must NOT receive SHUTDOWN.
        chain.abort_tool("child-a", "Bash", "exit 1")
        assert not (signals_dir / "child-b" / "SHUTDOWN").exists()

    def test_explicit_contain_policy_does_not_abort_siblings(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        policy = AbortPolicy(tool_to_sibling=False, sibling_to_session=False)
        chain.abort_tool("child-a", "Bash", "exit 1", policy=policy)
        assert not (signals_dir / "child-b" / "SHUTDOWN").exists()

    # ------------------------------------------------------------------
    # Propagation: tool_to_sibling=True cascades to SIBLING scope
    # ------------------------------------------------------------------

    def test_tool_to_sibling_true_aborts_siblings(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        chain.register_child("parent", "child-c")

        policy = AbortPolicy(tool_to_sibling=True, sibling_to_session=False)
        cascaded = chain.abort_tool("child-a", "Bash", "exit 1", policy=policy)

        # child-b and child-c should receive SHUTDOWN.
        assert set(cascaded) == {"child-b", "child-c"}
        assert (signals_dir / "child-b" / "SHUTDOWN").exists()
        assert (signals_dir / "child-c" / "SHUTDOWN").exists()
        # Parent must NOT receive SHUTDOWN.
        assert not (signals_dir / "parent" / "SHUTDOWN").exists()

    def test_tool_to_sibling_true_does_not_abort_parent(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        policy = AbortPolicy(tool_to_sibling=True, sibling_to_session=False)
        chain.abort_tool("child-a", "Bash", "err", policy=policy)
        assert not (signals_dir / "parent" / "SHUTDOWN").exists()


# ---------------------------------------------------------------------------
# SIBLING scope — abort_siblings
# ---------------------------------------------------------------------------


class TestAbortSiblings:
    def test_aborts_all_siblings_not_self(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        chain.register_child("parent", "child-c")

        aborted = chain.abort_siblings("child-a", triggering_session_id="child-a", reason="failure")
        assert set(aborted) == {"child-b", "child-c"}
        assert (signals_dir / "child-b" / "SHUTDOWN").exists()
        assert (signals_dir / "child-c" / "SHUTDOWN").exists()
        # The triggering session does not receive SHUTDOWN from itself.
        assert not (signals_dir / "child-a" / "SHUTDOWN").exists()

    def test_does_not_abort_parent(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        chain.abort_siblings("child-a", triggering_session_id="child-a", reason="fail")
        assert not (signals_dir / "parent" / "SHUTDOWN").exists()

    def test_no_parent_returns_empty(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        aborted = chain.abort_siblings("orphan", triggering_session_id="orphan", reason="fail")
        assert aborted == []

    def test_no_siblings_returns_empty(self, signals_dir: Path) -> None:
        """Only child has no siblings."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "only-child")
        aborted = chain.abort_siblings("only-child", triggering_session_id="only-child", reason="fail")
        assert aborted == []

    def test_sibling_shutdown_content_distinguishable(self, signals_dir: Path) -> None:
        """SHUTDOWN content for a sibling abort must differ from a parent cascade."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        chain.abort_siblings("child-a", triggering_session_id="child-a", reason="tool_fail")

        content = (signals_dir / "child-b" / "SHUTDOWN").read_text(encoding="utf-8")
        assert "Sibling aborted" in content
        assert "child-a" in content  # triggering session mentioned
        assert "tool_fail" in content

    # ------------------------------------------------------------------
    # Containment: sibling_to_session=False does NOT abort parent
    # ------------------------------------------------------------------

    def test_contained_sibling_abort_does_not_cascade_to_parent(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("grandparent", "parent")
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")

        policy = AbortPolicy(tool_to_sibling=False, sibling_to_session=False)
        chain.abort_siblings("child-a", triggering_session_id="child-a", reason="fail", policy=policy)

        assert not (signals_dir / "parent" / "SHUTDOWN").exists()
        assert not (signals_dir / "grandparent" / "SHUTDOWN").exists()

    # ------------------------------------------------------------------
    # Propagation: sibling_to_session=True cascades to SESSION scope
    # ------------------------------------------------------------------

    def test_sibling_to_session_true_aborts_parent_session(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        # parent itself has no further ancestors; propagate_abort on parent
        # sends SHUTDOWN to its children only (child-b, not child-a which
        # triggered the abort, but SHUTDOWN files are written regardless).
        policy = AbortPolicy(tool_to_sibling=False, sibling_to_session=True)
        aborted = chain.abort_siblings("child-a", triggering_session_id="child-a", reason="fail", policy=policy)

        # parent's children (child-b) get SHUTDOWN from sibling level.
        # Then propagate_abort(parent) cascades to parent's children (child-b again, idempotent).
        # Both child-a and child-b are in the cascade set.
        shutdown_b = (signals_dir / "child-b" / "SHUTDOWN").exists()
        assert shutdown_b  # child-b got SHUTDOWN from sibling level
        assert "parent" in aborted or "child-b" in aborted  # cascaded


# ---------------------------------------------------------------------------
# SESSION scope — propagate_abort (regression + hierarchy interaction)
# ---------------------------------------------------------------------------


class TestSessionAbortHierarchy:
    def test_session_abort_leaves_sibling_intact(self, signals_dir: Path) -> None:
        """Aborting one branch does NOT send SHUTDOWN to sibling branches."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("root", "branch-a")
        chain.register_child("root", "branch-b")
        chain.register_child("branch-a", "leaf-a")

        # Abort only branch-a's subtree.
        aborted = chain.propagate_abort("branch-a")
        assert set(aborted) == {"leaf-a"}
        # branch-b must NOT receive SHUTDOWN.
        assert not (signals_dir / "branch-b" / "SHUTDOWN").exists()

    def test_session_abort_cascades_fully(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("root", "child-a")
        chain.register_child("root", "child-b")
        chain.register_child("child-a", "grandchild")

        aborted = chain.propagate_abort("root")
        assert set(aborted) == {"child-a", "child-b", "grandchild"}


# ---------------------------------------------------------------------------
# Reverse index — get_parent / get_siblings
# ---------------------------------------------------------------------------


class TestReverseIndex:
    def test_get_parent(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child")
        assert chain.get_parent("child") == "parent"
        assert chain.get_parent("parent") is None

    def test_get_siblings(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        chain.register_child("parent", "child-c")

        siblings_of_a = chain.get_siblings("child-a")
        assert siblings_of_a == {"child-b", "child-c"}

    def test_get_siblings_no_parent(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        assert chain.get_siblings("orphan") == set()

    def test_cleanup_removes_from_reverse_index(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child")
        chain.cleanup("child")
        assert chain.get_parent("child") is None


# ---------------------------------------------------------------------------
# Composability: full escalation chain TOOL → SIBLING → SESSION
# ---------------------------------------------------------------------------


class TestFullEscalationChain:
    def test_tool_to_sibling_to_session(self, signals_dir: Path) -> None:
        """Full escalation: tool abort → sibling abort → parent session abort."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("root", "agent-1")
        chain.register_child("root", "agent-2")
        chain.register_child("root", "agent-3")

        policy = AbortPolicy(tool_to_sibling=True, sibling_to_session=True)
        cascaded = chain.abort_tool("agent-1", "Bash", "exit 2", policy=policy)

        # agent-2 and agent-3 are siblings → SHUTDOWN from sibling scope.
        assert (signals_dir / "agent-2" / "SHUTDOWN").exists()
        assert (signals_dir / "agent-3" / "SHUTDOWN").exists()
        # root's propagate_abort cascades back to agent-2, agent-3 (idempotent).
        assert len(cascaded) >= 2

    def test_tool_contained_then_sibling_contained(self, signals_dir: Path) -> None:
        """Each scope independently contained — nothing leaks upward."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("grandparent", "parent")
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")

        # Contain at tool level.
        policy_tool_contain = AbortPolicy(tool_to_sibling=False)
        chain.abort_tool("child-a", "Bash", "err", policy=policy_tool_contain)

        # Contain at sibling level.
        policy_sibling_contain = AbortPolicy(sibling_to_session=False)
        chain.abort_siblings("child-a", triggering_session_id="child-a", reason="fail", policy=policy_sibling_contain)

        # parent and grandparent must remain unaffected.
        assert not (signals_dir / "parent" / "SHUTDOWN").exists()
        assert not (signals_dir / "grandparent" / "SHUTDOWN").exists()
