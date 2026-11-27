"""Issue #5108: one broken skill source must not take every other skill with it.

``SkillLoader._reload`` had no exception handling, and ``__init__`` calls it
directly. A source whose ``iter_skills()`` threw therefore raised out of the
constructor: the caller got no ``SkillLoader`` at all, and every skill from
every OTHER source failed to load because of one bad entry.

A failure that vanishes into a log line is the same as no failure at all to the
operator reading ``status``, so the loader records what it refused to index and
why, and the rest of the set loads.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.core.skills.loader import (
    DuplicateSkillError,
    QuarantinedSkill,
    SkillLoader,
)
from bernstein.core.skills.manifest import SkillManifest
from bernstein.core.skills.source import SkillArtifact, SkillSource

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def persistent_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise the production factory, with only synthetic plugin sources."""
    from bernstein.core.skills.loader import default_loader_from_templates

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    broken = _ThrowingSource("broken-plugin", RuntimeError("entry point blew up"))
    healthy = _InMemorySource("healthy", [_artifact("alpha", "from-healthy")])
    monkeypatch.setattr(
        "bernstein.core.skills.sources.plugin.scan_plugin_sources",
        lambda: SimpleNamespace(sources=[broken, healthy], resolutions=()),
    )

    def load():
        return default_loader_from_templates(tmp_path / "templates" / "roles")

    return load


def _persisted(tmp_path: Path):
    path = tmp_path / ".sdd/runtime/declaration-quarantine.json"
    assert path.exists(), "initialisation failure must leave a persisted disabled marker"
    return json.loads(path.read_text())


def test_quarantine_is_journaled_with_reason_and_timestamp(tmp_path: Path, persistent_failure) -> None:
    from bernstein.core.security.audit_chain import AuditChainStore

    before = time.time()
    loader = persistent_failure()
    after = time.time()
    assert [s.name for s in loader.list_all()] == ["alpha"]
    (record,) = _persisted(tmp_path)
    assert record["kind"] == "skill"
    assert record["disabled_by_failure"] is True
    assert record["reason"] == "entry point blew up"
    assert before <= record["at"] <= after
    chain = AuditChainStore(tmp_path / ".sdd/audit")
    (event,) = chain.query(event_type="declaration.quarantined")
    assert event.details["reason"] == record["reason"]
    assert event.details["at"] == record["at"]
    assert chain.verify() == (True, [])


def test_quarantined_source_is_not_retried(tmp_path: Path, persistent_failure, monkeypatch) -> None:
    persistent_failure()
    _persisted(tmp_path)

    def forbidden(_self):
        pytest.fail("a quarantined source was retried without explicit re-enable")

    monkeypatch.setattr(_ThrowingSource, "iter_skills", forbidden)
    assert [s.name for s in persistent_failure().list_all()] == ["alpha"]
    from bernstein.core.security.audit_chain import AuditChainStore

    assert len(AuditChainStore(tmp_path / ".sdd/audit").query(event_type="declaration.quarantined")) == 1


def test_duplicate_marker_only_blocks_the_conflicting_artifact(tmp_path: Path, persistent_failure, monkeypatch) -> None:
    from bernstein.core.skills.loader import default_loader_from_templates

    monkeypatch.setattr(
        "bernstein.core.skills.sources.plugin.scan_plugin_sources",
        lambda: SimpleNamespace(
            sources=[
                _InMemorySource("first", [_artifact("conflict", "first")]),
                _InMemorySource("second", [_artifact("conflict", "second"), _artifact("beta", "second-beta")]),
            ],
            resolutions=(),
        ),
    )
    roles = tmp_path / "templates/roles"
    default_loader_from_templates(roles)
    (record,) = _persisted(tmp_path)
    assert record["origin"] == "second"
    # The losing artifact stays blocked; the winner and its healthy sibling load.
    original_register = SkillLoader._register

    def register(self, source, artifact):
        assert artifact.origin != "second", "quarantined artifact was registered again"
        original_register(self, source, artifact)

    monkeypatch.setattr(SkillLoader, "_register", register)
    loader = default_loader_from_templates(roles)
    assert [s.name for s in loader.list_all()] == ["beta", "conflict"]


@pytest.mark.parametrize("as_json", [True, False])
def test_status_reports_quarantine_count(persistent_failure, monkeypatch, as_json) -> None:
    from bernstein.cli.commands.status_cmd import status

    persistent_failure()
    monkeypatch.setattr("bernstein.cli.commands.status_cmd.server_get", lambda *a, **kw: {})
    result = CliRunner().invoke(status, ["--json"] if as_json else [])
    assert result.exit_code == 0, result.output
    if as_json:
        assert json.loads(result.output).get("quarantined_declarations") == 1
    else:
        assert "Quarantined declarations: 1" in result.output


def test_reenable_clears_marker_and_journals_actor(tmp_path: Path, persistent_failure, monkeypatch) -> None:
    from bernstein.cli.commands.advanced_cmd import quarantine_group
    from bernstein.core.security.audit_chain import AuditChainStore

    persistent_failure()
    (record,) = _persisted(tmp_path)
    monkeypatch.setattr("getpass.getuser", lambda: "test-operator")
    result = CliRunner().invoke(quarantine_group, ["re-enable", record["declaration_id"]])
    assert result.exit_code == 0, result.output
    assert _persisted(tmp_path) == []
    (event,) = AuditChainStore(tmp_path / ".sdd/audit").query(event_type="declaration.reenabled")
    assert event.actor == "test-operator"
    assert event.resource_id == record["declaration_id"]
    # Fixed code is only eligible again after the explicit command.
    monkeypatch.setattr(_ThrowingSource, "iter_skills", lambda self: [_artifact("recovered", "fixed")])
    assert [s.name for s in persistent_failure().list_all()] == ["alpha", "recovered"]


def test_doctor_and_govern_audit_report_quarantined_declarations(persistent_failure, tmp_path) -> None:
    from bernstein.cli.commands.govern_audit_cmd import govern_audit_cmd
    from bernstein.cli.commands.status_cmd import doctor

    persistent_failure()
    result = CliRunner().invoke(doctor, ["--json"])
    assert result.exit_code == 1, result.output
    checks = json.loads(result.output)["checks"]
    findings = [check for check in checks if check["name"] == "Declaration quarantine"]
    assert findings, "doctor must report disabled declarations"
    assert findings[0]["ok"] is False
    assert "entry point blew up" in findings[0]["detail"]
    result = CliRunner().invoke(govern_audit_cmd, ["--only", "doctor", "--json", "-w", str(tmp_path)])
    assert result.exit_code == 1, result.output
    findings = [f for f in json.loads(result.output) if f["check_id"] == "doctor:declaration-quarantine"]
    assert findings, "govern audit must report disabled declarations"
    assert findings[0]["verdict"] == "fail"
    assert "entry point blew up" in findings[0]["summary"]


def test_a_failed_audit_append_preserves_isolation_and_marker(
    persistent_failure, tmp_path, monkeypatch, caplog
) -> None:
    def unavailable(*args, **kwargs):
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr("bernstein.core.security.audit_chain.AuditChainStore.log_with_prev_digest", unavailable)
    loader = persistent_failure()
    assert [s.name for s in loader.list_all()] == ["alpha"]
    assert len(_persisted(tmp_path)) == 1
    assert "audit sink unavailable" in caplog.text

    def forbidden(_self):
        pytest.fail("audit failure released a quarantined source")

    monkeypatch.setattr(_ThrowingSource, "iter_skills", forbidden)
    assert [s.name for s in persistent_failure().list_all()] == ["alpha"]


@pytest.mark.parametrize("damaged", ["{", "{}", "[{}]"])
def test_damaged_quarantine_is_a_finding_not_a_silent_release(persistent_failure, tmp_path, damaged) -> None:
    from bernstein.core.checks.adapters import DeclarationQuarantineAdapter
    from bernstein.core.security.declaration_quarantine import quarantine_summary

    persistent_failure()
    (tmp_path / ".sdd/runtime/declaration-quarantine.json").write_text(damaged)
    _name, ok, detail, _fix = quarantine_summary(tmp_path)
    assert ok is False
    assert "Cannot read" in detail
    assert DeclarationQuarantineAdapter().run(tmp_path).passed is False


class _InMemorySource(SkillSource):
    """A source that yields a fixed artifact list. Idiom from ``test_loader.py``."""

    def __init__(self, label: str, artifacts: list[SkillArtifact]) -> None:
        self._label = label
        self._artifacts = artifacts

    @property
    def name(self) -> str:
        return self._label

    def iter_skills(self) -> list[SkillArtifact]:
        return self._artifacts.copy()


class _ThrowingSource(SkillSource):
    """A source that cannot enumerate at all -- an unreadable dir, a bad plugin."""

    def __init__(self, label: str, error: Exception) -> None:
        self._label = label
        self._error = error

    @property
    def name(self) -> str:
        return self._label

    def iter_skills(self) -> list[SkillArtifact]:
        raise self._error


def _artifact(name: str, origin: str) -> SkillArtifact:
    return SkillArtifact(
        manifest=SkillManifest(
            name=name,
            description="A stub description exceeding twenty characters easily.",
        ),
        body=f"# body for {name}",
        origin=origin,
    )


def test_one_throwing_source_quarantines_and_others_load() -> None:
    """The load-bearing case. On main the constructor raises and there is no loader."""
    broken = _ThrowingSource("broken-plugin", RuntimeError("entry point blew up"))
    healthy = _InMemorySource("healthy", [_artifact("alpha", "from-healthy")])

    loader = SkillLoader([broken, healthy])

    assert [s.name for s in loader.list_all()] == ["alpha"]

    (quarantined,) = loader.quarantined
    assert quarantined.source_name == "broken-plugin"
    assert quarantined.error_type == "RuntimeError"
    assert quarantined.reason == "entry point blew up"
    # A source that threw while enumerating never said WHICH skill it was
    # building, and claiming one would be an invention.
    assert quarantined.skill_name is None


def test_a_source_after_a_broken_one_still_loads() -> None:
    """Ordering: the isolation is per source, not "stop at the first failure"."""
    loader = SkillLoader(
        [
            _InMemorySource("first", [_artifact("alpha", "a")]),
            _ThrowingSource("broken", OSError("permission denied")),
            _InMemorySource("last", [_artifact("beta", "b")]),
        ]
    )

    assert [s.name for s in loader.list_all()] == ["alpha", "beta"]
    assert len(loader.quarantined) == 1


def test_duplicate_skill_name_quarantines_the_second_one() -> None:
    """A conflict is still never silent -- it is recorded rather than fatal."""
    first = _InMemorySource("first", [_artifact("conflict", "from-first")])
    second = _InMemorySource("second", [_artifact("conflict", "from-second")])

    loader = SkillLoader([first, second])

    assert loader.get("conflict").origin == "from-first"
    (quarantined,) = loader.quarantined
    assert quarantined.error_type == DuplicateSkillError.__name__
    assert quarantined.skill_name == "conflict"


def test_the_record_carries_a_reason_and_a_timestamp() -> None:
    """What the operator needs to act: which, why, and when."""
    before = time.time()
    loader = SkillLoader([_ThrowingSource("broken", ValueError("bad manifest"))])
    after = time.time()

    (quarantined,) = loader.quarantined
    assert isinstance(quarantined, QuarantinedSkill)
    assert quarantined.reason == "bad manifest"
    assert quarantined.error_type == "ValueError"
    assert before <= quarantined.at <= after


def test_the_hook_is_called_once_per_quarantine_as_it_happens() -> None:
    """The seam a caller journals a governance event through.

    The loader is deliberately stateless and holds no audit key, so it reports
    rather than writes -- and it reports at the moment of failure rather than
    handing back a list to diff afterwards.
    """
    seen: list[QuarantinedSkill] = []
    SkillLoader(
        [
            _ThrowingSource("broken-a", RuntimeError("a")),
            _ThrowingSource("broken-b", RuntimeError("b")),
            _InMemorySource("healthy", [_artifact("alpha", "x")]),
        ],
        on_quarantine=seen.append,
    )

    assert [record.reason for record in seen] == ["a", "b"]


def test_a_throwing_hook_does_not_break_the_load() -> None:
    """The reporter is not the subject.

    An audit sink that is down must not do what the broken skill could not.
    """

    def _explode(_record: QuarantinedSkill) -> None:
        raise RuntimeError("audit sink unavailable")

    loader = SkillLoader(
        [
            _ThrowingSource("broken", RuntimeError("boom")),
            _InMemorySource("healthy", [_artifact("alpha", "x")]),
        ],
        on_quarantine=_explode,
    )

    assert [s.name for s in loader.list_all()] == ["alpha"]
    assert len(loader.quarantined) == 1


def test_a_healthy_load_quarantines_nothing() -> None:
    """The control: the record is empty when nothing failed."""
    loader = SkillLoader([_InMemorySource("healthy", [_artifact("alpha", "x")])])
    assert loader.quarantined == ()


def test_a_reload_clears_the_previous_quarantine(sample_skills_root: Path) -> None:
    """Quarantine describes THIS load, not every load the object has ever done."""
    from bernstein.core.skills.sources import LocalDirSkillSource

    loader = SkillLoader([_ThrowingSource("broken", RuntimeError("boom"))])
    assert len(loader.quarantined) == 1

    loader._sources = [LocalDirSkillSource(sample_skills_root)]
    loader._reload()

    assert loader.quarantined == ()
    assert [s.name for s in loader.list_all()] == ["alpha", "beta", "gamma"]
