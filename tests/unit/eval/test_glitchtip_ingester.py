"""Tests for the GlitchTip event ingester.

Covers two surfaces:

* The :class:`GlitchTipIncident` dataclass and its routing through
  ``IncidentSynthesizer._synthesize_eval_case`` (shape, dedup,
  idempotency, redaction).
* The ``scripts/scrape_glitchtip_events`` helpers: issue listing,
  pagination, the wiring-probe allow-list filter, stacktrace
  extraction, dedup against existing YAML cases, and the missing-token
  graceful exit.

The GlitchTip API is never contacted: every test injects a fake
``http_get`` that reads fixtures under ``tests/fixtures/glitchtip/``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from bernstein.eval.incident_synthesizer import (
    GlitchTipIncident,
    IncidentSynthesizer,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "glitchtip"


def _load_scraper() -> Any:
    """Load ``scripts/scrape_glitchtip_events`` once per session."""
    if "scrape_glitchtip_events" in sys.modules:
        return sys.modules["scrape_glitchtip_events"]
    repo_root = Path(__file__).resolve().parents[3]
    script_path = repo_root / "scripts" / "scrape_glitchtip_events.py"
    spec = importlib.util.spec_from_file_location("scrape_glitchtip_events", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrape_glitchtip_events"] = module
    spec.loader.exec_module(module)
    return module


SCRAPER = _load_scraper()


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fake HTTP transport
# ---------------------------------------------------------------------------


class _FakeHTTP:
    """A scriptable ``http_get`` returning ``(status, payload, headers)``.

    ``routes`` maps a URL substring to a ``(status, payload, headers)``
    tuple. The first substring that matches the requested URL wins, so
    register the more specific routes first.
    """

    def __init__(self, routes: list[tuple[str, tuple[int, Any, dict[str, str]]]]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, token: str, timeout: float) -> tuple[int, Any, dict[str, str]]:
        self.calls.append(url)
        for needle, response in self._routes:
            if needle in url:
                return response
        raise AssertionError(f"no fake route registered for {url}")


# ---------------------------------------------------------------------------
# GlitchTipIncident dataclass + synthesizer dispatch
# ---------------------------------------------------------------------------


class TestGlitchTipIncidentDataclass:
    def test_required_field_and_defaults(self) -> None:
        inc = GlitchTipIncident(issue_id="42")
        assert inc.issue_id == "42"
        assert inc.project_slug == ""
        assert inc.exception_type == ""
        assert inc.top_frame_line == 0
        assert inc.event_count == 0

    def test_is_hashable_and_frozen(self) -> None:
        import dataclasses

        inc = GlitchTipIncident(issue_id="7")
        assert hash(inc) == hash(inc)
        with pytest.raises(dataclasses.FrozenInstanceError):
            inc.issue_id = "8"  # type: ignore[misc]


class TestSynthesizeFromGlitchTipIncident:
    def test_one_incident_produces_one_p1_case(self, tmp_path: Path) -> None:
        inc = GlitchTipIncident(
            issue_id="12345",
            project_slug="bernstein-orchestrator",
            exception_type="RuntimeError",
            exception_value="ci-verify shim raised on purpose",
            top_frame_path="src/bernstein/core/orchestration/conductor.py",
            top_frame_line=487,
            first_seen="2026-05-20T00:00:00Z",
            last_seen="2026-05-21T03:14:15Z",
            event_count=42,
            environment="production",
            release="bernstein@2.4.1",
            title="RuntimeError: ci-verify shim raised",
        )
        synth = IncidentSynthesizer(tmp_path)
        case = synth.synthesize_from_glitchtip_incident(inc)
        assert case is not None
        assert case.severity == "P1"
        assert case.source_incident == "glitchtip-issue:12345"
        assert case.owner == "orchestrator"
        assert "glitchtip" in case.tags
        assert "regression" in case.tags
        assert "runtime_error" in case.tags
        assert "runtimeerror" in case.tags
        assert "env_production" in case.tags
        # Breadcrumbs in the prompt so a candidate agent can reproduce.
        assert "12345" in case.prompt
        assert "RuntimeError" in case.prompt
        assert "conductor.py:487" in case.prompt
        assert "bernstein-orchestrator" in case.prompt

    def test_missing_issue_id_skipped(self, tmp_path: Path) -> None:
        inc = GlitchTipIncident(issue_id="")
        synth = IncidentSynthesizer(tmp_path)
        assert synth.synthesize_from_glitchtip_incident(inc) is None

    def test_dispatch_via_internal_seam(self, tmp_path: Path) -> None:
        inc = GlitchTipIncident(issue_id="99", exception_type="KeyError")
        synth = IncidentSynthesizer(tmp_path)
        case = synth._synthesize_eval_case(inc)
        assert case is not None
        assert case.severity == "P1"

    def test_no_hostname_leaks_into_prompt(self, tmp_path: Path) -> None:
        """The synthesised prompt must never carry the GlitchTip host."""
        inc = GlitchTipIncident(
            issue_id="555",
            exception_type="RuntimeError",
            exception_value="boom",
            top_frame_path="src/bernstein/x.py",
            top_frame_line=10,
        )
        synth = IncidentSynthesizer(tmp_path)
        case = synth.synthesize_from_glitchtip_incident(inc)
        assert case is not None
        assert "errors.bernstein.run" not in case.prompt
        assert "http" not in case.prompt.lower()


# ---------------------------------------------------------------------------
# Redaction: secrets in the exception value must not survive into YAML
# ---------------------------------------------------------------------------


class TestRedaction:
    # A 40-char GitHub PAT body so the PII gate's ``github_token`` rule
    # fires (its threshold is 37+ chars after the ``ghp_`` prefix).
    _GHP = "ghp_" + ("A" * 40)

    def test_secret_in_exception_value_is_redacted(self, tmp_path: Path) -> None:
        inc = GlitchTipIncident(
            issue_id="666",
            exception_type="ValueError",
            exception_value=f"auth failed for token {self._GHP} leaked",
            top_frame_path="src/bernstein/core/observability/error_capture.py",
            top_frame_line=64,
        )
        synth = IncidentSynthesizer(tmp_path)
        case = synth.synthesize_from_glitchtip_incident(inc)
        assert case is not None
        assert self._GHP not in case.prompt
        assert "***" in case.prompt

    def test_emitted_yaml_has_no_secret(self, tmp_path: Path) -> None:
        """Drive a record through the scraper -> sync() and scan the YAML."""
        gt_dir = tmp_path / ".sdd" / "reports" / "glitchtip_events"
        gt_dir.mkdir(parents=True, exist_ok=True)
        (gt_dir / "issue-666.json").write_text(
            json.dumps(
                {
                    "glitchtip_issue_id": "666",
                    "project_slug": "bernstein-orchestrator",
                    "exception_type": "ValueError",
                    "exception_value": f"auth failed for token {self._GHP} leaked",
                    "top_frame_path": "src/bernstein/x.py",
                    "top_frame_line": 1,
                    "first_seen": "",
                    "last_seen": "",
                    "event_count": 1,
                    "environment": "staging",
                    "release": "",
                    "title": "ValueError: token leak",
                },
            ),
            encoding="utf-8",
        )
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 1
        cases_dir = tmp_path / "src" / "bernstein" / "eval" / "cases" / "incidents"
        body = next(cases_dir.glob("inc-*.yaml")).read_text(encoding="utf-8")
        assert self._GHP not in body


# ---------------------------------------------------------------------------
# Filesystem-path scrubbing: absolute home/root frame paths must not leak
# the OS username into committed public YAML.
# ---------------------------------------------------------------------------


class TestFramePathScrub:
    @pytest.mark.parametrize(
        "abs_path",
        [
            "/Users/alice/dev/bernstein/src/bernstein/x.py",
            "/home/bob/bernstein/src/bernstein/x.py",
            "/root/carol/bernstein/src/bernstein/x.py",
            "C:\\Users\\dave\\bernstein\\src\\x.py",
        ],
    )
    def test_absolute_home_path_scrubbed_from_prompt(self, tmp_path: Path, abs_path: str) -> None:
        inc = GlitchTipIncident(
            issue_id="777",
            exception_type="RuntimeError",
            exception_value="boom",
            top_frame_path=abs_path,
            top_frame_line=42,
        )
        synth = IncidentSynthesizer(tmp_path)
        case = synth.synthesize_from_glitchtip_incident(inc)
        assert case is not None
        for leak in ("/Users/", "/home/", "/root/", "C:\\Users\\"):
            assert leak not in case.prompt
        # The username segment is gone; a relative-looking tail remains.
        assert "alice" not in case.prompt
        assert "<path>/" in case.prompt

    def test_absolute_home_path_scrubbed_from_yaml(self, tmp_path: Path) -> None:
        gt_dir = tmp_path / ".sdd" / "reports" / "glitchtip_events"
        gt_dir.mkdir(parents=True, exist_ok=True)
        (gt_dir / "issue-778.json").write_text(
            json.dumps(
                {
                    "glitchtip_issue_id": "778",
                    "project_slug": "bernstein-orchestrator",
                    "exception_type": "RuntimeError",
                    "exception_value": "boom",
                    "top_frame_path": "/Users/erin/dev/bernstein/src/bernstein/x.py",
                    "top_frame_line": 7,
                    "first_seen": "",
                    "last_seen": "",
                    "event_count": 1,
                    "environment": "production",
                    "release": "",
                    "title": "RuntimeError: boom",
                },
            ),
            encoding="utf-8",
        )
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 1
        cases_dir = tmp_path / "src" / "bernstein" / "eval" / "cases" / "incidents"
        body = next(cases_dir.glob("inc-*.yaml")).read_text(encoding="utf-8")
        for leak in ("/Users/", "/home/", "/root/", "erin"):
            assert leak not in body


# ---------------------------------------------------------------------------
# Pagination host pinning: a cross-host ``next`` link is never followed.
# ---------------------------------------------------------------------------


class TestPaginationHostPin:
    def test_same_host_next_is_followed(self) -> None:
        page1 = _fixture("issues_page1.json")
        page2 = _fixture("issues_page2.json")
        next_url = "https://x.example.com/api/0/organizations/bernstein/issues/?cursor=p2"
        link_header = f'<{next_url}>; rel="next"; results="true"'
        fake = _FakeHTTP(
            [
                ("cursor=p2", (200, page2, {})),
                ("issues/", (200, page1, {"link": link_header})),
            ],
        )
        issues = SCRAPER.list_unresolved_issues(
            "https://x.example.com",
            "tok",
            "bernstein",
            http_get=fake,
        )
        assert len(issues) == 2
        assert any("cursor=p2" in c for c in fake.calls)

    def test_cross_host_next_is_dropped(self) -> None:
        page1 = _fixture("issues_page1.json")
        # A hostile server tries to redirect the token-bearing cursor at
        # an attacker-controlled host; it must not be followed.
        evil_url = "https://evil.example.net/api/0/organizations/bernstein/issues/?cursor=p2"
        link_header = f'<{evil_url}>; rel="next"; results="true"'
        fake = _FakeHTTP(
            [
                ("issues/", (200, page1, {"link": link_header})),
            ],
        )
        issues = SCRAPER.list_unresolved_issues(
            "https://x.example.com",
            "tok",
            "bernstein",
            http_get=fake,
        )
        # Only page 1 was fetched; the cross-host cursor was never called.
        assert len(fake.calls) == 1
        assert not any("evil.example.net" in c for c in fake.calls)
        assert len(issues) == 1

    def test_same_host_helper(self) -> None:
        assert SCRAPER._same_host("https://x.example.com/api/0/x/?c=1", "https://x.example.com")
        assert not SCRAPER._same_host("https://evil.example.net/api/0/x/", "https://x.example.com")
        assert not SCRAPER._same_host("ftp://x.example.com/api/0/x/", "https://x.example.com")
        assert not SCRAPER._same_host("/relative/only", "https://x.example.com")


# ---------------------------------------------------------------------------
# JSON record ingestion + dedup / idempotency via sync()
# ---------------------------------------------------------------------------


def _write_record(workdir: Path, record: dict[str, Any]) -> Path:
    gt_dir = workdir / ".sdd" / "reports" / "glitchtip_events"
    gt_dir.mkdir(parents=True, exist_ok=True)
    path = gt_dir / f"issue-{record['glitchtip_issue_id']}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class TestSyncIngestsGlitchTipRecords:
    def test_sync_emits_yaml_case(self, tmp_path: Path) -> None:
        _write_record(
            tmp_path,
            {
                "glitchtip_issue_id": "12345",
                "project_slug": "bernstein-orchestrator",
                "exception_type": "RuntimeError",
                "exception_value": "ci-verify shim raised",
                "top_frame_path": "src/bernstein/cli/run_cmd.py",
                "top_frame_line": 212,
                "first_seen": "2026-05-20T00:00:00Z",
                "last_seen": "2026-05-21T00:00:00Z",
                "event_count": 42,
                "environment": "production",
                "release": "bernstein@2.4.1",
                "title": "RuntimeError: ci-verify shim raised",
            },
        )
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 1
        case = result.created[0]
        assert case.source_incident == "glitchtip-issue:12345"
        assert case.severity == "P1"

        cases_dir = tmp_path / "src" / "bernstein" / "eval" / "cases" / "incidents"
        files = list(cases_dir.glob("inc-*.yaml"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        assert "source_incident:" in body
        assert "glitchtip-issue:12345" in body
        assert "severity: P1" in body

    def test_sync_is_idempotent(self, tmp_path: Path) -> None:
        _write_record(
            tmp_path,
            {
                "glitchtip_issue_id": "777",
                "exception_type": "KeyError",
                "exception_value": "missing",
                "title": "KeyError",
            },
        )
        synth = IncidentSynthesizer(tmp_path)
        first = synth.sync()
        second = synth.sync()
        assert len(first.created) == 1
        assert len(second.created) == 0
        assert second.skipped_duplicates >= 1

    def test_malformed_records_skipped(self, tmp_path: Path) -> None:
        gt_dir = tmp_path / ".sdd" / "reports" / "glitchtip_events"
        gt_dir.mkdir(parents=True, exist_ok=True)
        (gt_dir / "bad.json").write_text("{not json", encoding="utf-8")
        (gt_dir / "no-id.json").write_text(json.dumps({"exception_type": "X"}), encoding="utf-8")
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 0


# ---------------------------------------------------------------------------
# scrape_glitchtip_events helpers
# ---------------------------------------------------------------------------


class TestReadEnv:
    def test_alias_token_resolves(self) -> None:
        token, _, org = SCRAPER._read_env(
            {"GLITCHTIP_API_TOKEN": "abc", "GLITCHTIP_BASE_URL": "https://x.example.com"},
        )
        assert token == "abc"
        assert org == "bernstein"

    def test_bernstein_token_wins_over_alias(self) -> None:
        token, _, _ = SCRAPER._read_env(
            {"BERNSTEIN_GLITCHTIP_TOKEN": "primary", "GLITCHTIP_API_TOKEN": "alias"},
        )
        assert token == "primary"

    def test_base_url_alias_and_strip(self) -> None:
        _, base_url, _ = SCRAPER._read_env(
            {"GLITCHTIP_API_TOKEN": "t", "GLITCHTIP_BASE_URL": "https://x.example.com/"},
        )
        assert base_url == "https://x.example.com"

    def test_org_slug_alias(self) -> None:
        _, _, org = SCRAPER._read_env(
            {"GLITCHTIP_API_TOKEN": "t", "GLITCHTIP_ORG_SLUG": "acme"},
        )
        assert org == "acme"

    def test_base_url_derived_from_dsn(self) -> None:
        _, base_url, _ = SCRAPER._read_env(
            {
                "GLITCHTIP_API_TOKEN": "t",
                "GLITCHTIP_DSN": "https://pub@host.example.com/7",
            },
        )
        assert base_url == "https://host.example.com"


class TestLinkHeaderPagination:
    def test_parse_next_with_results(self) -> None:
        header = (
            '<https://h.example.com/api/0/x/?cursor=a:0:1>; rel="previous"; results="false", '
            '<https://h.example.com/api/0/x/?cursor=b:1:0>; rel="next"; results="true"'
        )
        assert SCRAPER._parse_link_header(header) == "https://h.example.com/api/0/x/?cursor=b:1:0"

    def test_parse_next_results_false_returns_none(self) -> None:
        header = '<https://h.example.com/api/0/x/?cursor=z>; rel="next"; results="false"'
        assert SCRAPER._parse_link_header(header) is None

    def test_empty_header(self) -> None:
        assert SCRAPER._parse_link_header("") is None

    def test_list_follows_pagination(self) -> None:
        page1 = _fixture("issues_page1.json")
        page2 = _fixture("issues_page2.json")
        next_url = "https://x.example.com/api/0/organizations/bernstein/issues/?cursor=p2"
        link_header = f'<{next_url}>; rel="next"; results="true"'
        fake = _FakeHTTP(
            [
                ("cursor=p2", (200, page2, {})),
                ("issues/", (200, page1, {"link": link_header})),
            ],
        )
        issues = SCRAPER.list_unresolved_issues(
            "https://x.example.com",
            "tok",
            "bernstein",
            http_get=fake,
        )
        ids = sorted(i["id"] for i in issues)
        assert ids == ["1001", "1002"]
        # The query string filters to unresolved issues.
        assert any("is:unresolved" in c for c in fake.calls)


class TestStacktraceExtraction:
    def test_picks_deepest_in_app_frame(self) -> None:
        event = _fixture("event_with_stacktrace.json")
        exc_type, exc_value, path, line = SCRAPER._extract_exception_from_event(event)
        assert exc_type == "RuntimeError"
        assert exc_value == "ci-verify shim raised on purpose"
        # The deepest in_app frame (conductor.py:487), not the library frame.
        assert path == "src/bernstein/core/orchestration/conductor.py"
        assert line == 487

    def test_tags_extracted(self) -> None:
        event = _fixture("event_with_stacktrace.json")
        assert SCRAPER._extract_tag(event, "environment") == "production"
        assert SCRAPER._extract_tag(event, "release") == "bernstein@2.4.1"
        assert SCRAPER._extract_tag(event, "server_name") == "runner-7"

    def test_event_without_exception(self) -> None:
        exc_type, _exc_value, path, line = SCRAPER._extract_exception_from_event({"tags": []})
        assert exc_type == ""
        assert path == ""
        assert line == 0


class TestWiringProbeFilter:
    def test_default_probes_match(self) -> None:
        assert SCRAPER._is_wiring_probe(
            "glitchtip insights wiring probe",
            SCRAPER.DEFAULT_WIRING_PROBE_ALLOW_LIST,
        )
        assert SCRAPER._is_wiring_probe(
            "GlitchTip Smoke From Operator Finalisation",
            SCRAPER.DEFAULT_WIRING_PROBE_ALLOW_LIST,
        )

    def test_real_issue_not_filtered(self) -> None:
        assert not SCRAPER._is_wiring_probe(
            "RuntimeError: real regression",
            SCRAPER.DEFAULT_WIRING_PROBE_ALLOW_LIST,
        )

    def test_substring_variant_filtered(self) -> None:
        assert SCRAPER._is_wiring_probe(
            "glitchtip smoke from operator finalisation v2",
            SCRAPER.DEFAULT_WIRING_PROBE_ALLOW_LIST,
        )


# ---------------------------------------------------------------------------
# scrape_glitchtip_events.run end-to-end with fake HTTP
# ---------------------------------------------------------------------------


def _routes_for(issues_fixture: str, event_fixture: str | None) -> list[tuple[str, tuple[int, Any, dict[str, str]]]]:
    routes: list[tuple[str, tuple[int, Any, dict[str, str]]]] = []
    if event_fixture is not None:
        routes.append(("events/latest", (200, _fixture(event_fixture), {})))
    else:
        routes.append(("events/latest", (404, None, {})))
    routes.append(("issues/", (200, _fixture(issues_fixture), {})))
    return routes


class TestScraperRun:
    def _env(self) -> dict[str, str]:
        return {
            "GLITCHTIP_API_TOKEN": "tok",
            "GLITCHTIP_BASE_URL": "https://x.example.com",
            "GLITCHTIP_ORG_SLUG": "bernstein",
        }

    def test_single_issue_becomes_one_record(self, tmp_path: Path) -> None:
        fake = _FakeHTTP(_routes_for("issues_single.json", "event_with_stacktrace.json"))
        out_dir = tmp_path / "glitchtip_events"
        emitted = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            env=self._env(),
            http_get=fake,
        )
        assert emitted == 1
        files = list(out_dir.glob("*.json"))
        assert len(files) == 1
        record = json.loads(files[0].read_text(encoding="utf-8"))
        assert record["glitchtip_issue_id"] == "12345"
        assert record["exception_type"] == "RuntimeError"
        assert record["top_frame_path"] == "src/bernstein/core/orchestration/conductor.py"
        assert record["top_frame_line"] == 487
        assert record["environment"] == "production"
        assert record["release"] == "bernstein@2.4.1"
        assert record["project_slug"] == "bernstein-orchestrator"
        assert record["event_count"] == 42
        # The dataclass-only ``title`` is carried for the probe filter.
        assert record["title"] == "RuntimeError: ci-verify shim raised"

    def test_empty_issue_list_emits_nothing(self, tmp_path: Path) -> None:
        fake = _FakeHTTP(_routes_for("issues_empty.json", None))
        out_dir = tmp_path / "glitchtip_events"
        emitted = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            env=self._env(),
            http_get=fake,
        )
        assert emitted == 0
        assert not out_dir.exists() or not list(out_dir.glob("*.json"))

    def test_wiring_probes_filtered_out(self, tmp_path: Path) -> None:
        fake = _FakeHTTP(_routes_for("issues_with_probes.json", "event_with_stacktrace.json"))
        out_dir = tmp_path / "glitchtip_events"
        emitted = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            env=self._env(),
            http_get=fake,
        )
        # Only the real RuntimeError issue (902) survives the filter.
        assert emitted == 1
        record = json.loads(next(out_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert record["glitchtip_issue_id"] == "902"

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        fake = _FakeHTTP(_routes_for("issues_single.json", "event_with_stacktrace.json"))
        out_dir = tmp_path / "glitchtip_events"
        first = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            env=self._env(),
            http_get=fake,
        )
        second = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            env=self._env(),
            http_get=fake,
        )
        assert first == 1
        assert second == 0
        assert len(list(out_dir.glob("*.json"))) == 1

    def test_dedup_against_existing_yaml_case(self, tmp_path: Path) -> None:
        cases_dir = tmp_path / "cases" / "incidents"
        cases_dir.mkdir(parents=True, exist_ok=True)
        (cases_dir / "inc-deadbeefcafe.yaml").write_text(
            "id: inc-deadbeefcafe\n"
            "severity: P1\n"
            'source_incident: "glitchtip-issue:12345"\n'
            "owner: orchestrator\n"
            "tags: []\n"
            "prompt: |\n"
            "  Reproduce and resolve the runtime exception reported by GlitchTip issue 12345.\n",
            encoding="utf-8",
        )
        fake = _FakeHTTP(_routes_for("issues_single.json", "event_with_stacktrace.json"))
        out_dir = tmp_path / "glitchtip_events"
        emitted = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=cases_dir,
            dry_run=False,
            env=self._env(),
            http_get=fake,
        )
        assert emitted == 0

    def test_dry_run_writes_nothing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        fake = _FakeHTTP(_routes_for("issues_single.json", "event_with_stacktrace.json"))
        out_dir = tmp_path / "glitchtip_events"
        emitted = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=True,
            env=self._env(),
            http_get=fake,
        )
        assert emitted == 1
        assert not out_dir.exists()
        printed = capsys.readouterr().out
        assert "12345" in printed

    def test_event_lookup_failure_tolerated(self, tmp_path: Path) -> None:
        """A failed event lookup still emits a record (no stacktrace fields)."""
        fake = _FakeHTTP(_routes_for("issues_single.json", None))
        out_dir = tmp_path / "glitchtip_events"
        emitted = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            env=self._env(),
            http_get=fake,
        )
        assert emitted == 1
        record = json.loads(next(out_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert record["glitchtip_issue_id"] == "12345"
        assert record["exception_type"] == ""
        assert record["top_frame_path"] == ""

    def test_missing_token_exits_zero(self, tmp_path: Path) -> None:
        emitted = SCRAPER.run(
            out_dir=tmp_path / "glitchtip_events",
            cases_dir=None,
            dry_run=False,
            env={},  # no token
        )
        assert emitted == 0
        assert not (tmp_path / "glitchtip_events").exists()

    def test_missing_base_url_exits_zero(self, tmp_path: Path) -> None:
        emitted = SCRAPER.run(
            out_dir=tmp_path / "glitchtip_events",
            cases_dir=None,
            dry_run=False,
            env={"GLITCHTIP_API_TOKEN": "tok"},  # token but no base url / DSN
        )
        assert emitted == 0

    def test_api_unreachable_exits_zero(self, tmp_path: Path) -> None:
        def boom(url: str, token: str, timeout: float) -> tuple[int, Any, dict[str, str]]:
            raise SCRAPER.GlitchTipHTTPError("connection refused")

        emitted = SCRAPER.run(
            out_dir=tmp_path / "glitchtip_events",
            cases_dir=None,
            dry_run=False,
            env=self._env(),
            http_get=boom,
        )
        assert emitted == 0


# ---------------------------------------------------------------------------
# Cross-module: scraper -> synthesizer end-to-end
# ---------------------------------------------------------------------------


class TestEndToEndFlow:
    def test_scraper_output_drives_synthesizer(self, tmp_path: Path) -> None:
        """A record emitted by the scraper must be ingested by ``sync()``."""
        env = {
            "GLITCHTIP_API_TOKEN": "tok",
            "GLITCHTIP_BASE_URL": "https://x.example.com",
            "GLITCHTIP_ORG_SLUG": "bernstein",
        }
        fake = _FakeHTTP(_routes_for("issues_single.json", "event_with_stacktrace.json"))
        out_dir = tmp_path / ".sdd" / "reports" / "glitchtip_events"
        emitted = SCRAPER.run(
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            env=env,
            http_get=fake,
        )
        assert emitted == 1
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 1
        case = result.created[0]
        assert case.source_incident == "glitchtip-issue:12345"
        assert case.severity == "P1"
        # Re-run is a pure no-op end-to-end.
        again = synth.sync()
        assert len(again.created) == 0
        assert again.skipped_duplicates >= 1
