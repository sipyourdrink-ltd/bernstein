"""Tests for Agent Identity Lifecycle Management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.agents.agent_identity import AgentIdentityStore


@pytest.fixture
def store(tmp_path: Path) -> AgentIdentityStore:
    """Return a fresh AgentIdentityStore backed by a tmp_path."""
    return AgentIdentityStore(tmp_path)


class TestScopeValidationWithParent:
    """Tests for the new scope validation in create_identity when parent_identity_id is provided."""

    def test_parent_scope_validation_refuses_wider_task_ids(self, store: AgentIdentityStore) -> None:
        """A child with task_ids not a subset of parent is refused."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2"])
        child_task_ids = ["t-1", "t-2", "t-9"]  # wider than parent
        with pytest.raises(ValueError, match="child task_ids .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=child_task_ids)

    def test_parent_scope_validation_refuses_wider_allowed_files(self, store: AgentIdentityStore) -> None:
        """A child with allowed_files not a subset of parent is refused."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py"])
        child_files = ["src/a.py", "src/b.py", "src/c.py"]  # wider than parent
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=child_files)

    def test_parent_scope_validation_accepts_narrowing_task_ids(self, store: AgentIdentityStore) -> None:
        """A child with task_ids subset of parent is accepted."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2", "t-3"])
        child_task_ids = ["t-1", "t-2"]  # narrower than parent
        identity, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=child_task_ids)
        assert identity.task_ids == ["t-1", "t-2"]

    def test_parent_scope_validation_accepts_matching_task_ids(self, store: AgentIdentityStore) -> None:
        """A child with task_ids exactly matching parent is accepted."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2"])
        child_task_ids = ["t-1", "t-2"]
        identity, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=child_task_ids)
        assert identity.task_ids == ["t-1", "t-2"]

    def test_parent_scope_validation_accepts_empty_parent(self, store: AgentIdentityStore) -> None:
        """A child with empty parent task_ids is unrestricted."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=[])
        child_task_ids = ["t-1", "t-2"]
        identity, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=child_task_ids)
        assert identity.task_ids == ["t-1", "t-2"]

    def test_parent_scope_validation_refuses_wider_allowed_files_with_patterns(self, store: AgentIdentityStore) -> None:
        """A child with allowed_files not covered by parent patterns is refused."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py"])
        child_files = ["src/a.py", "src/b.py", "src/c.py"]
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=child_files)

    def test_parent_scope_validation_accepts_narrowing_allowed_files(self, store: AgentIdentityStore) -> None:
        """A child with allowed_files subset of parent is accepted."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py", "src/c.py"])
        child_files = ["src/a.py", "src/b.py"]
        identity, _ = store.create_identity(
            "child-1", "backend", parent_identity_id=parent.id, allowed_files=child_files
        )
        assert identity.allowed_files == ["src/a.py", "src/b.py"]

    def test_parent_scope_validation_refuses_unrestricted_from_restricted_parent(
        self, store: AgentIdentityStore
    ) -> None:
        """A child with allowed_files=[] from a parent with restrictions is refused."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py"])
        child_files = []  # unrestricted
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=child_files)

    def test_parent_scope_validation_refuses_wider_task_ids_when_parent_has_empty_task_ids(
        self, store: AgentIdentityStore
    ) -> None:
        """Parent with empty task_ids means unrestricted → child can be anything."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=[])
        child_task_ids = ["t-1", "t-2"]
        identity, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=child_task_ids)
        assert identity.task_ids == ["t-1", "t-2"]

    def test_parent_scope_validation_accepts_narrowing_task_ids_when_parent_has_task_ids(
        self, store: AgentIdentityStore
    ) -> None:
        """Child with fewer task_ids than parent is accepted."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2", "t-3"])
        child_task_ids = ["t-1", "t-2"]
        identity, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=child_task_ids)
        assert identity.task_ids == ["t-1", "t-2"]

    def test_parent_scope_validation_accepts_matching_allowed_files(self, store: AgentIdentityStore) -> None:
        """Child with allowed_files exactly matching parent is accepted."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py"])
        child_files = ["src/a.py", "src/b.py"]
        identity, _ = store.create_identity(
            "child-1", "backend", parent_identity_id=parent.id, allowed_files=child_files
        )
        assert identity.allowed_files == ["src/a.py", "src/b.py"]

    def test_parent_scope_validation_accepts_unrestricted_child_from_unrestricted_parent(
        self, store: AgentIdentityStore
    ) -> None:
        """A child with allowed_files=[] from a parent with no restriction is accepted."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=[])
        identity, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=[])
        assert identity.allowed_files == []

    def test_parent_scope_validation_accepts_narrowing_allowed_files_with_glob_patterns(
        self, store: AgentIdentityStore
    ) -> None:
        """A child with glob patterns that fall inside the parent's patterns is accepted."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/**"])
        identity, _ = store.create_identity(
            "child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/a.py", "src/b/c.py"]
        )
        assert identity.allowed_files == ["src/a.py", "src/b/c.py"]

    def test_parent_scope_validation_refuses_glob_pattern_outside_parent_scope(self, store: AgentIdentityStore) -> None:
        """A child pattern naming a file outside the parent's patterns is refused."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py"])
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=["tests/a.py"])

    def test_parent_scope_validation_refuses_wider_task_ids_naming_the_offending_value(
        self, store: AgentIdentityStore
    ) -> None:
        """The refusal names which axis widened and the offending value."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2"])
        with pytest.raises(
            ValueError,
            match=r"child task_ids \['t-1', 't-2', 't-9'\] are not a subset of parent task_ids \['t-1', 't-2'\]",
        ):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=["t-1", "t-2", "t-9"])

    def test_parent_scope_validation_refuses_wider_allowed_files_naming_the_offending_value(
        self, store: AgentIdentityStore
    ) -> None:
        """The refusal names which axis widened and the offending value."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py"])
        with pytest.raises(
            ValueError,
            match=r"child allowed_files \['src/a.py', 'src/b.py', 'src/c.py'\] are not a subset of parent allowed_files \['src/a.py', 'src/b.py'\]",
        ):
            store.create_identity(
                "child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/a.py", "src/b.py", "src/c.py"]
            )

    def test_parent_scope_validation_refuses_unrestricted_naming_the_offending_value(
        self, store: AgentIdentityStore
    ) -> None:
        """The refusal names the axis and the offending empty list."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py"])
        with pytest.raises(
            ValueError, match=r"child allowed_files \[\] are not a subset of parent allowed_files \['src/a.py'\]"
        ):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=[])

    def test_parent_scope_validation_unaffected_when_no_parent_named(self, store: AgentIdentityStore) -> None:
        """An identity minted with no parent_identity_id is unaffected by the check."""
        identity, _ = store.create_identity("child-1", "backend", task_ids=["t-1", "t-2"])
        assert identity.task_ids == ["t-1", "t-2"]
        assert identity.parent_identity_id is None


class TestIdentityAndCredentialScopeMustAgree:
    """Two copies of the task scope must not disagree about what is allowed.

    ``task_ids`` and ``allowed_files`` are persisted on the identity and on
    its credential.  Different consumers read different copies - the request
    middleware reads the identity's, the JWT claim check reads the
    credential's - so a record holding two answers is authorized under
    whichever copy the reader happens to reach.  An identity holding an empty
    list beside a scoped credential is the dangerous direction: empty means
    unrestricted.
    """

    def test_a_legacy_shaped_record_is_refused_before_its_claims_are_read(self, tmp_path: Path) -> None:
        """A stored non-string scope dies at the read, not at the claim comparison.

        Both copies of the scope are written together, so a token can only
        carry a non-string entry if the record beside it carries one too - and
        that record is refused when it loads, a step before the claim check
        runs.  Tightening the claim comparison therefore takes no token out of
        service that was still in service without it.
        """
        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-legacy", "backend", task_ids=["7"])
        path = tmp_path / "agent_identities" / f"{identity.id}.json"
        payload = json.loads(path.read_text())
        payload["task_ids"] = [7]
        payload["credential"]["task_ids"] = [7]
        path.write_text(json.dumps(payload))

        reloaded = AgentIdentityStore(tmp_path)

        assert reloaded._load(identity.id) is None
        assert reloaded.authenticate(token) is None
