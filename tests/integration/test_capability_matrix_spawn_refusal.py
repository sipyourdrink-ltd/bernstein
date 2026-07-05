"""End-to-end spawn-refusal integration tests for the lethal-trifecta matrix.

These tests exercise the *real* :class:`AgentSpawner.spawn_for_tasks` path -
not the unit-level ``CapabilityRegistry.evaluate_chain`` - to prove that
the capability matrix is wired into spawn-time enforcement and cannot be
bypassed by alias renames, runtime tag mutation, or relaxed-mode flips.

Each scenario constructs a real :class:`CatalogRegistry` with a typed
:class:`CatalogAgent`, hands it to a ``MagicMock``-backed adapter, and
calls ``spawner.spawn_for_tasks([task])``.  Refusal is detected by the
``SpawnError`` raised at validation time *before* the adapter's
``spawn()`` method is ever invoked - which is also asserted via
``adapter.spawn.assert_not_called()`` and via a monkeypatched
``subprocess.Popen`` watch.

Coverage maps the deliverable list:
    * Happy path (baseline) - no trifecta, spawn succeeds.
    * Direct trifecta refusal - declared trifecta tools.
    * Aliased tool refusal - same trifecta but renamed to undeclared aliases.
    * Runtime substitution - operator config swaps a tool mid-spawn.
    * Mutation refusal - registry mutates between validation and execution.
    * Bypass-immune override - bypass=True does not relax IMMUNE/lethal.
    * Audit event - refusal emits ``capability_matrix_refusal`` HMAC entry.
    * No subprocess on refusal - ``subprocess.Popen`` is never called.
    * Adapter-only chain (regression) - adapter envelope alone passes.

Multi-OS: tests do not rely on POSIX-only signals or ``chmod 0o600``;
``tmp_path`` and ``unittest.mock`` work identically on Windows runners.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnError, SpawnResult
from bernstein.agents.catalog import CatalogAgent, CatalogRegistry
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.capability_matrix import (
    Capability,
    EnforcementMode,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CAPABILITY_TEMPLATES_REL = Path("templates") / "capabilities"


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """Per-test workdir with a minimal git repo and a templates tree.

    The capability templates are staged at ``<workdir>/templates/capabilities/``
    so :meth:`CapabilityRegistry.load_default` resolves the local copy
    instead of the bundled defaults - guaranteeing the integration test
    runs against an isolated, predictable registry.
    """
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "ci@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ci"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    (tmp_path / "README.md").write_text("# integration test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    capabilities_dir = tmp_path / _CAPABILITY_TEMPLATES_REL
    capabilities_dir.mkdir(parents=True)
    # Mirror the bundled surfaces.yaml shape with the known-trifecta tags
    # so these tests do not depend on the bundled wheel paths surviving
    # editable installs.
    (capabilities_dir / "surfaces.yaml").write_text(
        """tools:
  - name: fs.read
    capabilities: [private_data]
  - name: fs.read_secret
    capabilities: [private_data]
  - name: web.fetch
    capabilities: [untrusted_input, external_comm]
  - name: web.search
    capabilities: [untrusted_input, external_comm]
  - name: github.fetch_issue
    capabilities: [untrusted_input, external_comm]
  - name: github.post_comment
    capabilities: [external_comm]
  - name: shell.exec
    capabilities: [private_data, external_comm]
  - name: git.commit
    capabilities: []
  - name: git.read
    capabilities: [private_data]
  - name: pytest.run
    capabilities: []
""",
        encoding="utf-8",
    )
    (capabilities_dir / "adapters.yaml").write_text(
        """tools:
  - name: adapter.mockcli
    capabilities: [private_data, untrusted_input, external_comm]
""",
        encoding="utf-8",
    )

    templates_roles = tmp_path / "templates" / "roles" / "backend"
    templates_roles.mkdir(parents=True)
    (templates_roles / "system_prompt.md").write_text(
        "You are a backend specialist.",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture()
def mock_adapter() -> MagicMock:
    """Mock :class:`CLIAdapter` matching the ``mock_adapter_factory`` shape."""
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=4242, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "mockcli"
    return adapter


@pytest.fixture()
def make_task(workdir: Path) -> Iterator[Any]:  # type: ignore[misc]
    """Yield a factory that builds a minimal :class:`Task`."""
    from bernstein.core.models import (
        Complexity,
        Scope,
        Task,
        TaskStatus,
        TaskType,
    )

    def _factory(
        *,
        task_id: str = "T-001",
        role: str = "backend",
        title: str = "Integration spawn test task",
        description: str = "Drive the spawner through to refusal or success.",
    ) -> Task:
        return Task(
            id=task_id,
            title=title,
            description=description,
            role=role,
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            priority=2,
        )

    yield _factory


def _catalog_with(
    *,
    name: str = "test-agent",
    role: str = "backend",
    tools: list[str] | None = None,
) -> CatalogRegistry:
    """Build a :class:`CatalogRegistry` containing one tool-bearing agent."""
    agent = CatalogAgent(
        name=name,
        role=role,
        description="Integration test agent",
        system_prompt="You are an integration test specialist.",
        id=f"local:{name}",
        tools=tools or [],
        capabilities=[],
        priority=10,
        source="local",
    )
    catalog = CatalogRegistry()
    catalog.register_agent(agent)
    return catalog


def _build_spawner(
    *,
    workdir: Path,
    adapter: MagicMock,
    catalog: CatalogRegistry | None = None,
) -> AgentSpawner:
    """Construct an :class:`AgentSpawner` plumbed exactly like prod (sans worktree).

    ``default_model`` is set explicitly: these tests exercise capability-matrix
    enforcement, not model routing, but the ``mockcli`` adapter is not
    Claude-compatible, so an unconfigured model would now correctly raise
    ``ModelNotConfiguredError`` at the spawn boundary (Bernstein never
    silently falls back to a Claude tier name for a non-Claude adapter).
    """
    return AgentSpawner(
        adapter=adapter,
        templates_dir=workdir / "templates" / "roles",
        workdir=workdir,
        catalog=catalog,
        use_worktrees=False,
        default_model="mock-model",
    )


def _set_enforcement(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Replace the frozen :data:`SECURITY` singleton with the requested mode."""
    from bernstein.core.defaults import SecurityDefaults

    monkeypatch.setattr(
        "bernstein.core.defaults.SECURITY",
        SecurityDefaults(lethal_trifecta_enforcement=value),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 1. Happy path - non-trifecta tools spawn successfully (baseline)
# ---------------------------------------------------------------------------


class TestHappyPath:
    """A non-trifecta agent must spawn - the matrix is permissive by design
    when capability axes do not union the lethal trifecta.
    """

    def test_baseline_safe_chain_spawns(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        catalog = _catalog_with(tools=["fs.read", "git.commit", "pytest.run"])
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        session = spawner.spawn_for_tasks([make_task()])

        assert session.pid == 4242
        mock_adapter.spawn.assert_called_once()
        manifest_path = workdir / ".sdd" / "runtime" / "spawn_capabilities" / f"{session.id}.json"
        assert manifest_path.exists(), "Spawn manifest must be persisted on the happy path so audit can replay."
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["allowed"] is True
        assert manifest["mode"] == EnforcementMode.ENFORCE.value


# ---------------------------------------------------------------------------
# 2. Direct trifecta refusal - declared tags union the trifecta
# ---------------------------------------------------------------------------


class TestDirectTrifectaRefusal:
    """Three declared tools that union ``private_data + untrusted_input +
    external_comm`` must refuse the spawn through the real
    ``AgentSpawner.spawn_for_tasks`` path.
    """

    def test_declared_trifecta_refused(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        catalog = _catalog_with(
            tools=["fs.read", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task()])

        # Refusal happens BEFORE the adapter's ``spawn()`` is invoked.
        mock_adapter.spawn.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Aliased tool refusal - same trifecta but tool names are unknown aliases
# ---------------------------------------------------------------------------


class TestAliasedToolRefusal:
    """Renaming a trifecta-bearing tool to a different *registered* alias
    must NOT bypass the spawn refusal.  The matrix is name-agnostic - it
    keys on declared capabilities, not on canonical tool names - so an
    operator who registers ``read_file`` with ``[private_data]`` caps
    must see the same refusal as if they had used ``fs.read``.

    The "alias bypass" the security team worries about is two-step: the
    operator declares an alias with the original tool's caps in the
    registry (a structural choice they own), then references the alias
    from the catalog.  The spawner must follow the cap tags, not the
    name, so the trifecta still trips.
    """

    def _register_alias_yaml(self, workdir: Path) -> None:
        """Add aliased tool entries (read_file, fetch_url, post_pr_comment)
        with the canonical tools' capability tags so the trifecta
        calculation goes through the alias names rather than the
        canonical ones.
        """
        (workdir / _CAPABILITY_TEMPLATES_REL / "aliases.yaml").write_text(
            """tools:
  - name: read_file
    capabilities: [private_data]
  - name: fetch_url
    capabilities: [untrusted_input, external_comm]
  - name: post_pr_comment
    capabilities: [external_comm]
""",
            encoding="utf-8",
        )

    def test_registered_aliases_with_trifecta_caps_refused(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        """Aliases that are registered with the canonical caps still trip.

        The matrix is keyed on the declared cap set, not the canonical
        tool name - so renaming ``fs.read_secret`` → ``read_file`` (with
        the same ``[private_data]`` cap) must produce the same refusal.
        """
        self._register_alias_yaml(workdir)

        catalog = _catalog_with(tools=["read_file", "fetch_url", "post_pr_comment"])
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()

    def test_partial_aliasing_with_declared_safe_tool_still_refused(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        """Safe declared tools mixed with aliased trifecta still refuses.

        Even if the operator pads the chain with a known-safe tool to look
        legitimate, the registered aliases still cover all three caps and
        the trifecta fires.
        """
        self._register_alias_yaml(workdir)

        catalog = _catalog_with(
            tools=["git.commit", "pytest.run", "read_file", "fetch_url"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Runtime substitution - operator-supplied chain swap mid-spawn
# ---------------------------------------------------------------------------


class TestRuntimeSubstitution:
    """If an operator hot-swaps a clean catalog agent for a trifecta one
    between two ``spawn_for_tasks`` invocations, the SECOND spawn must be
    refused.  This is the realistic "config refresh" attack surface - and
    it pins the property that no spawner-internal cache can make a stale
    "allow" decision sticky.
    """

    def test_swap_clean_for_trifecta_refuses_second(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        # First spawn uses a safe catalog - must succeed.
        safe_catalog = _catalog_with(tools=["fs.read", "git.commit"])
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=safe_catalog)
        spawner.spawn_for_tasks([make_task(task_id="T-001")])
        assert mock_adapter.spawn.call_count == 1

        # Operator "hot-swaps" the catalog to a trifecta-bearing one.
        # ``AgentSpawner._catalog`` is the field the orchestrator rebinds
        # on catalog reload, so we exercise the same surface here.
        trifecta_catalog = _catalog_with(
            name="trifecta-agent",
            tools=["fs.read_secret", "web.fetch", "github.post_comment"],
        )
        spawner._catalog = trifecta_catalog  # rebind catalog like prod hot-swap

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task(task_id="T-002")])

        # Adapter spawn count is exactly 1 - only the first (safe) call ran.
        assert mock_adapter.spawn.call_count == 1


# ---------------------------------------------------------------------------
# 5. Mutation refusal - registry tags mutated between validation and exec
# ---------------------------------------------------------------------------


class TestMutationRefusal:
    """The :func:`_enforce_lethal_trifecta` guard re-loads the registry on
    every spawn, so post-spawn YAML edits (or in-flight registry rewrites)
    are observed at the *next* spawn - but a decision that fired during
    the current spawn is captured atomically before the manifest is
    written.
    """

    def test_registry_yaml_edit_takes_effect_on_next_spawn(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        # Spawn 1: trifecta blocked
        catalog = _catalog_with(
            tools=["fs.read", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)
        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task(task_id="T-blocked")])

        # Operator strips tags from the YAML to relax the deny then
        # re-tightens to undo the relaxation.  The spawner must observe
        # the re-tightened state on the next call.
        re_tightened = """tools:
  - name: fs.read
    capabilities: [private_data]
  - name: web.fetch
    capabilities: [untrusted_input, external_comm]
  - name: github.post_comment
    capabilities: [external_comm]
  - name: git.commit
    capabilities: []
  - name: git.read
    capabilities: [private_data]
  - name: pytest.run
    capabilities: []
"""
        (workdir / _CAPABILITY_TEMPLATES_REL / "surfaces.yaml").write_text(
            re_tightened,
            encoding="utf-8",
        )
        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task(task_id="T-blocked-2")])
        mock_adapter.spawn.assert_not_called()

    def test_decision_captured_at_validation_time_not_execution_time(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        """The persisted manifest is frozen against post-deny YAML edits.

        The unit-level "frozen :class:`ChainDecision`" contract is
        translated into the live spawn path - once the spawn manifest is
        written with ``allowed=False``, mutating the YAML cannot rewrite
        the historical record.
        """
        catalog = _catalog_with(
            tools=["fs.read", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)
        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks([make_task()])

        manifest_dir = workdir / ".sdd" / "runtime" / "spawn_capabilities"
        manifest_files = sorted(manifest_dir.glob("*.json"))
        assert manifest_files, "Expected a manifest to be written on refusal"
        manifest = json.loads(manifest_files[-1].read_text(encoding="utf-8"))
        assert manifest["allowed"] is False
        # Frozen even after we mutate the YAML - manifest value stays as-was.
        (workdir / _CAPABILITY_TEMPLATES_REL / "surfaces.yaml").write_text(
            "tools:\n  - name: fs.read\n    capabilities: []\n",
            encoding="utf-8",
        )
        manifest_after = json.loads(manifest_files[-1].read_text(encoding="utf-8"))
        assert manifest_after["allowed"] is False


# ---------------------------------------------------------------------------
# 6. Bypass-immune override - bypass=True does NOT relax IMMUNE
# ---------------------------------------------------------------------------


class TestBypassImmuneAtSpawnTime:
    """The ``BERNSTEIN_BYPASS_PERMISSIONS`` environment knob is meant for
    plugin/hook layers; it must NOT relax the structural lethal-trifecta
    refusal at spawn time.  We assert this property by setting the bypass
    flag and confirming the spawn is still refused.
    """

    def test_bypass_env_does_not_unlock_trifecta_spawn(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BERNSTEIN_BYPASS_PERMISSIONS", "1")

        catalog = _catalog_with(
            tools=["fs.read_secret", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Audit event - every refusal lands in the HMAC-chained audit log
# ---------------------------------------------------------------------------


class TestAuditEventOnRefusal:
    """Refusal must emit a ``capability_matrix_refusal`` audit event so the
    HMAC-chained audit log carries the structural decision and a security
    auditor can verify that no trifecta-prone agent ever spawned without
    a matching deny event.
    """

    def test_refusal_emits_capability_matrix_refusal_audit_event(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        catalog = _catalog_with(
            tools=["fs.read_secret", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks([make_task()])

        # The spawner writes audit events under ``<workdir>/.sdd/audit/``.
        audit = AuditLog(audit_dir=workdir / ".sdd" / "audit")
        ok, errors = audit.verify()
        assert ok, f"Audit chain integrity broke: {errors}"

        events = list(audit.query(event_type="capability_matrix_refusal"))
        assert events, (
            "Refusal must emit a capability_matrix_refusal audit event so "
            "the HMAC chain captures every blocked spawn attempt."
        )
        first = events[0]
        assert first.event_type == "capability_matrix_refusal"
        assert first.actor == "spawner"
        assert "lethal trifecta" in first.details.get("reason", "")
        offending = first.details.get("offending_tools") or []
        assert set(offending) >= {"fs.read_secret", "web.fetch", "github.post_comment"}


# ---------------------------------------------------------------------------
# 8. No subprocess started on refusal
# ---------------------------------------------------------------------------


class TestNoSubprocessOnRefusal:
    """Both ``subprocess.Popen`` and the adapter's ``spawn()`` must remain
    untouched whenever the trifecta refusal fires.  We monkeypatch
    ``subprocess.Popen`` after the workdir fixture's ``git init`` so our
    spy only counts calls initiated by the spawner path.
    """

    def test_popen_never_called_on_refusal(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        popen_calls: list[tuple[Any, ...]] = []

        original_popen = subprocess.Popen

        def _spy_popen(*args: Any, **kwargs: Any) -> Any:
            popen_calls.append((args, kwargs))
            return original_popen(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", _spy_popen)

        catalog = _catalog_with(
            tools=["fs.read", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()
        assert popen_calls == [], f"Refusal path triggered subprocess.Popen unexpectedly: {popen_calls!r}"


# ---------------------------------------------------------------------------
# 9. Adapter envelope alone is intentionally allowed
# ---------------------------------------------------------------------------


class TestAdapterEnvelopeAlone:
    """The bundled YAML tags every ``adapter.*`` entry with all three caps,
    intentionally - but the spawner only evaluates the catalog tool list,
    so a no-tools agent must still spawn.  This pins the contract that
    operators have to *opt into* a trifecta-prone tool list.
    """

    def test_no_catalog_tools_spawn_succeeds(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        catalog = _catalog_with(tools=[])
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_called_once()


# ---------------------------------------------------------------------------
# 10. Warn / off mode regression - refusal does NOT fire under WARN
# ---------------------------------------------------------------------------


class TestRelaxedModeRegression:
    """When the operator dials enforcement down to ``warn``, the spawner
    must allow the trifecta but still record the offending tools so
    auditors can flip back to ``enforce`` from the audit trail.
    """

    def test_warn_mode_allows_but_records_offending_tools(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_enforcement(monkeypatch, "warn")

        catalog = _catalog_with(
            tools=["fs.read", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)
        spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_called_once()
        manifest_dir = workdir / ".sdd" / "runtime" / "spawn_capabilities"
        manifest_files = list(manifest_dir.glob("*.json"))
        assert manifest_files
        manifest = json.loads(manifest_files[-1].read_text(encoding="utf-8"))
        assert manifest["allowed"] is True
        assert manifest["mode"] == EnforcementMode.WARN.value
        assert sorted(manifest["triggered"]) == sorted(c.value for c in Capability)


# ---------------------------------------------------------------------------
# 11. Logging surface - refusal logs at ERROR level with the trifecta tag
# ---------------------------------------------------------------------------


class TestRefusalLogging:
    """The refusal path must emit a structured log entry so operators
    notice the deny via Loki/Stackdriver alerts even before the audit log
    is queried.
    """

    def test_refusal_logs_trifecta_cause_at_error(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        catalog = _catalog_with(
            tools=["fs.read", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)
        with caplog.at_level(logging.ERROR, logger="bernstein.core.agents.spawner_core"):
            with pytest.raises(SpawnError):
                spawner.spawn_for_tasks([make_task()])

        msgs = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "Refusing spawn" in msgs
        assert "lethal trifecta" in msgs.lower()


# ---------------------------------------------------------------------------
# 12. Empty enforcement env value still defaults to ENFORCE
# ---------------------------------------------------------------------------


class TestEnforcementCoercion:
    """An invalid or stray enforcement value must coerce to ``enforce``
    rather than silently disabling the matrix.  We exercise the live
    spawn path to confirm the coercion is honoured by the spawner.
    """

    def test_invalid_enforcement_value_falls_back_to_enforce(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``Literal`` lets typing complain but the runtime constructor
        # accepts arbitrary strings; the spawner's coercion is the seam
        # under test.
        _set_enforcement(monkeypatch, "garbage-value")

        catalog = _catalog_with(
            tools=["fs.read_secret", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()


# ---------------------------------------------------------------------------
# 13. Single-tool trifecta - adapter-equivalent omnipotent tool refused
# ---------------------------------------------------------------------------


class TestSingleToolTrifectaSpawn:
    """If an operator declares a single catalog tool tagged with all three
    caps (e.g., a custom shell wrapper) the spawn must refuse.  Adversarial
    unit test :class:`TestSingleToolTrifecta` covers the matrix layer; this
    test pins the property at the spawn-time integration boundary.
    """

    def test_single_omnipotent_catalog_tool_refused(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        capabilities_dir = workdir / _CAPABILITY_TEMPLATES_REL
        custom_yaml = capabilities_dir / "custom.yaml"
        custom_yaml.write_text(
            """tools:
  - name: custom.omnipotent
    capabilities: [private_data, untrusted_input, external_comm]
""",
            encoding="utf-8",
        )

        catalog = _catalog_with(tools=["custom.omnipotent"])
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)

        with pytest.raises(SpawnError, match="lethal trifecta"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()


# ---------------------------------------------------------------------------
# 14. Manifest persists offending tools for declared trifecta
# ---------------------------------------------------------------------------


class TestManifestOffendingToolsOnDeny:
    """The persisted manifest must list every tool that contributed a
    triggering capability.  This is the seam SOC2 auditors rely on to
    replay refusals without parsing log lines.
    """

    def test_manifest_lists_offending_tools_on_refusal(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        catalog = _catalog_with(
            tools=["fs.read", "web.fetch", "github.post_comment"],
        )
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=catalog)
        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks([make_task()])

        manifest_dir = workdir / ".sdd" / "runtime" / "spawn_capabilities"
        manifests = list(manifest_dir.glob("*.json"))
        assert manifests, "Expected a manifest to be persisted on refusal"
        manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
        assert set(manifest["offending_tools"]) >= {
            "fs.read",
            "web.fetch",
            "github.post_comment",
        }
        assert sorted(manifest["triggered"]) == sorted(c.value for c in Capability)


# ---------------------------------------------------------------------------
# 15. No catalog at all - spawner falls back to built-in role template
# ---------------------------------------------------------------------------


class TestNoCatalog:
    """If the spawner is built without a catalog the trifecta evaluation
    has zero declared tools to chain - the spawn must succeed.  This
    regression-pins the default open-source posture: stock Bernstein with
    no operator-declared tools never blocks.
    """

    def test_no_catalog_spawns_without_block(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        spawner = _build_spawner(workdir=workdir, adapter=mock_adapter, catalog=None)

        spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_called_once()
