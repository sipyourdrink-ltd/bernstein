"""Tests for abort_chain — registration, propagation, cleanup, and SHUTDOWN writing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bernstein.core.abort_chain import AbortChain

# --- Fixtures ---


@pytest.fixture()
def signals_dir(tmp_path: Path) -> Path:
    """Create a temporary .sdd/runtime/signals/ directory."""
    sig = tmp_path / ".sdd" / "runtime" / "signals"
    sig.mkdir(parents=True, exist_ok=True)
    return sig


# --- TestAbortChain ---


class TestAbortChain:
    def test_register_child_and_get_children(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent-1", "child-1")
        chain.register_child("parent-1", "child-2")
        chain.register_child("child-1", "grandchild-1")

        assert chain.get_children("parent-1") == {"child-1", "child-2"}
        assert chain.get_children("child-1") == {"grandchild-1"}
        assert chain.get_children("nonexistent") == set()

    def test_propagate_abort_immediate_children(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent-1", "child-a")
        chain.register_child("parent-1", "child-b")

        aborted = chain.propagate_abort("parent-1")
        assert set(aborted) == {"child-a", "child-b"}

        # SHUTDOWN files should exist
        assert (signals_dir / "child-a" / "SHUTDOWN").exists()
        assert (signals_dir / "child-b" / "SHUTDOWN").exists()
        # Content should mention the parent
        content = (signals_dir / "child-a" / "SHUTDOWN").read_text(encoding="utf-8")
        assert "parent-1" in content

    def test_propagate_abort_cascades_to_grandchildren(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent-1", "child-1")
        chain.register_child("child-1", "grandchild-1")
        chain.register_child("grandchild-1", "great-grandchild-1")

        aborted = chain.propagate_abort("parent-1")
        assert set(aborted) == {"child-1", "grandchild-1", "great-grandchild-1"}

        assert (signals_dir / "child-1" / "SHUTDOWN").exists()
        assert (signals_dir / "grandchild-1" / "SHUTDOWN").exists()
        assert (signals_dir / "great-grandchild-1" / "SHUTDOWN").exists()

    def test_propagate_abort_multiple_parents(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent-1", "child-1")
        chain.register_child("parent-2", "child-2")

        aborted = chain.propagate_abort("parent-1")
        assert aborted == ["child-1"]
        assert (signals_dir / "child-1" / "SHUTDOWN").exists()
        assert not (signals_dir / "child-2" / "SHUTDOWN").exists()

    def test_propagate_abort_no_children(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        aborted = chain.propagate_abort("orphan")
        assert aborted == []

    def test_propagate_abort_diamond_graph(self, signals_dir: Path) -> None:
        """Avoid sending SHUTDOWN twice in a diamond DAG.

        parent -> child-a -> shared-grandchild
        parent -> child-b -> shared-grandchild
        """
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child-a")
        chain.register_child("parent", "child-b")
        chain.register_child("child-a", "shared-grandchild")
        chain.register_child("child-b", "shared-grandchild")

        aborted = chain.propagate_abort("parent")
        assert set(aborted) == {"child-a", "child-b", "shared-grandchild"}
        # Only one SHUTDOWN file for the shared grandchild
        shutdown_file = signals_dir / "shared-grandchild" / "SHUTDOWN"
        assert shutdown_file.exists()

    def test_cleanup_removes_parent_key(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent-1", "child-1")
        chain.cleanup("parent-1")

        assert chain.get_children("parent-1") == set()

    def test_cleanup_removes_child_edges(self, signals_dir: Path) -> None:
        """Cleanup should remove session_id as a child from any parent."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("grandparent", "parent-1")
        chain.register_child("parent-1", "child-1")

        chain.cleanup("parent-1")
        # grandparent's children should no longer include parent-1
        assert "parent-1" not in chain.get_children("grandparent")
        # parent-1 as parent key should be removed
        assert chain.get_children("parent-1") == set()

    def test_cleanup_nonexistent_is_noop(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.cleanup("does-not-exist")  # Should not raise

    def test_size(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        assert chain.size() == 0
        chain.register_child("p1", "c1")
        assert chain.size() == 1
        chain.register_child("p1", "c2")
        assert chain.size() == 2
        chain.register_child("c1", "gc1")
        assert chain.size() == 3

    def test_snapshot_returns_copy(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("p1", "c1")
        snap = chain.snapshot()
        assert snap == {"p1": {"c1"}}
        # Mutating the snapshot should not affect the chain
        snap["p1"].add("c2")
        assert chain.get_children("p1") == {"c1"}

    def test_shutdown_file_content_format(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent-id", "child-id")
        chain.propagate_abort("parent-id")

        content = (signals_dir / "child-id" / "SHUTDOWN").read_text(encoding="utf-8")
        assert "ABORT CHAIN" in content
        assert "Parent session: parent-id" in content
        assert "Your session: child-id" in content

    def test_propagate_abort_all_levels(self, signals_dir: Path) -> None:
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("root", "a")
        chain.register_child("root", "b")
        chain.register_child("a", "a1")
        chain.register_child("a", "a2")
        chain.register_child("b", "b1")
        chain.register_child("a1", "a1x")

        aborted = chain.propagate_abort("root")
        assert set(aborted) == {"a", "b", "a1", "a2", "b1", "a1x"}

    def test_propagate_abort_cleanup_parent_key(self, signals_dir: Path) -> None:
        """propagate_abort + cleanup removes the parent key from the graph."""
        chain = AbortChain(signals_dir=signals_dir)
        chain.register_child("parent", "child")
        chain.propagate_abort("parent")
        # propagate_abort does NOT clean up; caller must call cleanup
        assert chain.get_children("parent") == {"child"}
        chain.cleanup("parent")
        # Now the parent is removed
        assert chain.get_children("parent") == set()

    def test_concurrent_registration(self, signals_dir: Path) -> None:
        """Multiple threads registering children should not corrupt state."""
        import threading

        chain = AbortChain(signals_dir=signals_dir)
        errors: list[Exception] = []

        def register(parent: str, child: str) -> None:
            try:
                chain.register_child(parent, child)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register, args=(f"parent-{i % 5}", f"child-{i}")) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert chain.size() == 50


# --- Test_propagate_abort_to_children (agent_lifecycle integration) ---


class TestPropagateAbortToChildren:
    """Tests for the _propagate_abort_to_children helper in agent_lifecycle."""

    def test_propagate_when_abort_chain_exists(self) -> None:
        """When _abort_chain is set, propagate_abort + cleanup are called."""
        from bernstein.core import agent_lifecycle as al

        mock_chain = MagicMock(spec=AbortChain)
        orch = MagicMock()
        orch._abort_chain = mock_chain

        al._propagate_abort_to_children(orch, "session-abc")  # type: ignore[reportPrivateUsage]
        mock_chain.propagate_abort.assert_called_once_with("session-abc")
        mock_chain.cleanup.assert_called_once_with("session-abc")

    def test_noop_when_abort_chain_missing(self) -> None:
        """When _abort_chain is not set, function returns silently."""
        from bernstein.core import agent_lifecycle as al

        orch = MagicMock()
        # No _abort_chain attribute
        del orch._abort_chain

        al._propagate_abort_to_children(orch, "session-abc")  # type: ignore[reportPrivateUsage]
        # No exception, no calls

    def test_cleanup_called_even_if_propagate_raises(self) -> None:
        """Ensure cleanup runs even when propagate_abort raises an exception."""
        from bernstein.core import agent_lifecycle as al

        mock_chain = MagicMock(spec=AbortChain)
        mock_chain.propagate_abort.side_effect = RuntimeError("boom")
        orch = MagicMock()
        orch._abort_chain = mock_chain

        with pytest.raises(RuntimeError, match="boom"):
            al._propagate_abort_to_children(orch, "session-abc")  # type: ignore[reportPrivateUsage]

        mock_chain.cleanup.assert_called_once_with("session-abc")
