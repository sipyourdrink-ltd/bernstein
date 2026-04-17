"""Tests for bernstein.core.orchestration.dependency_scan_tasks.

Regression guard for audit-002: the original ``orchestrator_tick`` module
defined a never-called duplicate of ``Orchestrator.tick`` plus these three
dependency-scan helpers that *were* called from ``orchestrator_run``.

If the helpers silently regress (e.g. they rename, lose deduplication, or
drop server-post errors), these tests fail — they previously would have
passed because the dead duplicate in ``orchestrator_tick`` absorbed them.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

import pytest
from bernstein.core.dependency_scan import (
    DependencyScanResult,
    DependencyScanStatus,
    DependencyVulnerabilityFinding,
)

import bernstein.core.orchestration.dependency_scan_tasks as dependency_scan_tasks


@dataclass
class _FakeResponse:
    """Tiny stand-in for an ``httpx.Response``."""

    payload: object = None
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


@dataclass
class _FakeClient:
    """Fake HTTP client that records GET/POST calls."""

    get_response: _FakeResponse = field(default_factory=_FakeResponse)
    post_response: _FakeResponse = field(default_factory=_FakeResponse)
    get_calls: list[str] = field(default_factory=list)
    post_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    get_raises: Exception | None = None
    post_raises: Exception | None = None

    def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
        self.get_calls.append(url)
        if self.get_raises is not None:
            raise self.get_raises
        return self.get_response

    def post(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> _FakeResponse:
        self.post_calls.append((url, json))
        if self.post_raises is not None:
            raise self.post_raises
        return self.post_response


@dataclass
class _FakeConfig:
    server_url: str = "http://srv"


@dataclass
class _FakeScanner:
    """Records the ``create_fix_task`` callable and returns a canned result."""

    result: DependencyScanResult | None = None
    last_findings: list[DependencyVulnerabilityFinding] = field(default_factory=list)
    raise_exc: Exception | None = None

    def run_if_due(
        self,
        *,
        create_fix_task: Any,
        audit_log: Any,
    ) -> DependencyScanResult | None:
        if self.raise_exc is not None:
            raise self.raise_exc
        # Simulate the scanner invoking the callback for each finding.
        for finding in self.last_findings:
            create_fix_task(finding)
        return self.result


@dataclass
class _FakeOrch:
    _client: _FakeClient = field(default_factory=_FakeClient)
    _config: _FakeConfig = field(default_factory=_FakeConfig)
    _dependency_scanner: _FakeScanner = field(default_factory=_FakeScanner)
    _audit_log: object = None
    bulletins: list[tuple[str, str]] = field(default_factory=list)

    def _post_bulletin(self, kind: str, message: str) -> None:
        self.bulletins.append((kind, message))


def _finding(
    package: str = "jinja2",
    installed: str = "2.11.0",
    advisory: str = "PYSEC-1",
    summary: str = "Template injection",
    source: str = "pip-audit",
    fixes: tuple[str, ...] = ("3.1.0",),
) -> DependencyVulnerabilityFinding:
    return DependencyVulnerabilityFinding(
        package=package,
        installed_version=installed,
        advisory_id=advisory,
        summary=summary,
        source=source,
        fix_versions=fixes,
    )


# --- module structure ----------------------------------------------------


def test_module_exposes_expected_public_helpers() -> None:
    """Three helpers imported by ``orchestrator_run`` must exist."""
    assert hasattr(dependency_scan_tasks, "run_scheduled_dependency_scan")
    assert hasattr(dependency_scan_tasks, "load_existing_dependency_scan_task_titles")
    assert hasattr(dependency_scan_tasks, "create_dependency_fix_task")


def test_orchestrator_tick_module_is_deleted() -> None:
    """Regression guard for audit-002: the zombie module must not exist."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.orchestration.orchestrator_tick")


def test_core_redirect_map_no_longer_lists_orchestrator_tick() -> None:
    """The legacy redirect entry must be gone so importers fail loudly."""
    from bernstein.core import _REDIRECT_MAP

    assert "orchestrator_tick" not in _REDIRECT_MAP


# --- load_existing_dependency_scan_task_titles ---------------------------


def test_load_titles_filters_by_open_status() -> None:
    client = _FakeClient(
        get_response=_FakeResponse(
            payload=[
                {"title": "T-open", "status": "open"},
                {"title": "T-claimed", "status": "claimed"},
                {"title": "T-in-progress", "status": "in_progress"},
                {"title": "T-pending", "status": "pending_approval"},
                {"title": "T-done", "status": "done"},
                {"title": "T-failed", "status": "failed"},
            ]
        )
    )
    orch = _FakeOrch(_client=client)

    titles = dependency_scan_tasks.load_existing_dependency_scan_task_titles(orch)

    assert titles == {"T-open", "T-claimed", "T-in-progress", "T-pending"}
    assert client.get_calls == ["http://srv/tasks"]


def test_load_titles_returns_empty_on_http_error() -> None:
    orch = _FakeOrch(_client=_FakeClient(get_raises=RuntimeError("boom")))
    assert dependency_scan_tasks.load_existing_dependency_scan_task_titles(orch) == set()


def test_load_titles_returns_empty_for_non_list_payload() -> None:
    orch = _FakeOrch(_client=_FakeClient(get_response=_FakeResponse(payload={"err": "nope"})))
    assert dependency_scan_tasks.load_existing_dependency_scan_task_titles(orch) == set()


# --- create_dependency_fix_task ------------------------------------------


def test_create_fix_task_posts_expected_payload() -> None:
    orch = _FakeOrch()
    existing: set[str] = set()
    finding = _finding()

    title = dependency_scan_tasks.create_dependency_fix_task(orch, finding, existing)

    assert title == "Upgrade vulnerable dependency: jinja2"
    assert title in existing
    assert len(orch._client.post_calls) == 1
    url, payload = orch._client.post_calls[0]
    assert url == "http://srv/tasks"
    assert payload["title"] == title
    assert payload["role"] == "security"
    assert payload["priority"] == 2
    assert payload["task_type"] == "fix"
    assert "Advisory: PYSEC-1" in payload["description"]
    assert "Recommended fix versions: 3.1.0" in payload["description"]


def test_create_fix_task_skips_existing_titles() -> None:
    orch = _FakeOrch()
    existing = {"Upgrade vulnerable dependency: jinja2"}
    finding = _finding()

    title = dependency_scan_tasks.create_dependency_fix_task(orch, finding, existing)

    assert title is None
    assert orch._client.post_calls == []


def test_create_fix_task_returns_none_on_http_error() -> None:
    orch = _FakeOrch(_client=_FakeClient(post_raises=RuntimeError("502")))
    existing: set[str] = set()

    title = dependency_scan_tasks.create_dependency_fix_task(orch, _finding(), existing)

    assert title is None
    # Dedup set must NOT be populated when the post fails — otherwise the
    # next scan would silently skip re-trying.
    assert existing == set()


def test_create_fix_task_omits_fix_versions_line_when_empty() -> None:
    orch = _FakeOrch()
    finding = _finding(fixes=())
    dependency_scan_tasks.create_dependency_fix_task(orch, finding, set())

    _, payload = orch._client.post_calls[0]
    assert "Recommended fix versions" not in payload["description"]


# --- run_scheduled_dependency_scan ---------------------------------------


def test_run_scheduled_scan_posts_bulletin_on_result() -> None:
    result = DependencyScanResult(
        scan_id="scan-1",
        scanned_at=1.0,
        status=DependencyScanStatus.CLEAN,
        summary="All clean",
    )
    orch = _FakeOrch(_dependency_scanner=_FakeScanner(result=result))

    dependency_scan_tasks.run_scheduled_dependency_scan(orch)

    assert orch.bulletins == [("status", "dependency_scan: All clean")]


def test_run_scheduled_scan_swallows_scanner_exceptions() -> None:
    orch = _FakeOrch(_dependency_scanner=_FakeScanner(raise_exc=RuntimeError("scanner died")))
    dependency_scan_tasks.run_scheduled_dependency_scan(orch)
    # No bulletin posted, no crash bubbled up.
    assert orch.bulletins == []


def test_run_scheduled_scan_is_noop_when_scanner_returns_none() -> None:
    orch = _FakeOrch(_dependency_scanner=_FakeScanner(result=None))
    dependency_scan_tasks.run_scheduled_dependency_scan(orch)
    assert orch.bulletins == []


def test_run_scheduled_scan_dedups_findings_via_existing_titles() -> None:
    """Two findings for the same package should produce one POST (second is dedup'd)."""
    finding_a = _finding(package="jinja2")
    finding_b = _finding(package="jinja2", advisory="PYSEC-2")  # same package
    finding_c = _finding(package="urllib3", advisory="PYSEC-3", fixes=("1.26.0",))

    scanner = _FakeScanner(
        result=DependencyScanResult(
            scan_id="scan-2",
            scanned_at=1.0,
            status=DependencyScanStatus.VULNERABLE,
            summary="2 vulnerable packages",
        ),
        last_findings=[finding_a, finding_b, finding_c],
    )
    # Pre-populate the server with an already-open task for "jinja2"
    client = _FakeClient(
        get_response=_FakeResponse(
            payload=[{"title": "Upgrade vulnerable dependency: jinja2", "status": "open"}]
        )
    )
    orch = _FakeOrch(_client=client, _dependency_scanner=scanner)

    dependency_scan_tasks.run_scheduled_dependency_scan(orch)

    # Only urllib3 should have been POSTed; both jinja2 findings are skipped.
    assert len(orch._client.post_calls) == 1
    _, payload = orch._client.post_calls[0]
    assert payload["title"] == "Upgrade vulnerable dependency: urllib3"


# --- orchestrator_run shim -----------------------------------------------


def test_orchestrator_run_shim_delegates_to_new_module() -> None:
    """The private shims in orchestrator_run.py must still call the helpers.

    This is what guarantees the production call site keeps working after
    the orchestrator_tick.py deletion.
    """
    from bernstein.core.orchestration import orchestrator_run

    orch = _FakeOrch(
        _dependency_scanner=_FakeScanner(
            result=DependencyScanResult(
                scan_id="s",
                scanned_at=1.0,
                status=DependencyScanStatus.CLEAN,
                summary="nothing to do",
            )
        )
    )
    orchestrator_run._run_scheduled_dependency_scan(orch)
    assert orch.bulletins == [("status", "dependency_scan: nothing to do")]

    titles = orchestrator_run._load_existing_dependency_scan_task_titles(orch)
    assert titles == set()

    title = orchestrator_run._create_dependency_fix_task(orch, _finding(), set())
    assert title == "Upgrade vulnerable dependency: jinja2"
