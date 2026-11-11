"""Pin the behaviour the nightly mutation gate mutates in ``audit_pack`` (#6471).

The ``audit_pack`` target was gated against the staged-write tests alone, which
never reach the evidence resolvers, the dispatch by source kind, the staleness
boundary or the rendered counts, so almost every mutant survived. Each test
here pins one of those decisions to an exact value.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bernstein.core.security import audit_pack as ap

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _write(path: Path, body: bytes, *, mtime: datetime = _NOW) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def _sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Constants and defaults
# ---------------------------------------------------------------------------


def test_pending_marker_and_resolved_defaults_are_exact() -> None:
    assert ap.PENDING_EVIDENCE == "evidence: pending - artefact not produced"
    source = ap.EvidenceSource("CC1.1", "policy_file", "x", "d")
    resolved = ap.ResolvedEvidence(source=source, status=ap.STATUS_PENDING, evidence_ref="r")
    assert resolved.last_modified == 0.0
    assert resolved.details == {}


# ---------------------------------------------------------------------------
# Resolvers: missing directory, empty directory, present artefact
# ---------------------------------------------------------------------------


def test_policy_file_digest(tmp_path: Path) -> None:
    assert ap._policy_file_digest(tmp_path / "absent.md") == ("", 0.0, {})
    body = b"line one\nline two\n"
    path = _write(tmp_path / "policy.md", body)
    assert ap._policy_file_digest(path) == (_sha(body), _NOW.timestamp(), {"size_bytes": 18, "lines": 3})


def test_audit_chain_tail_digest(tmp_path: Path) -> None:
    assert ap._hmac_chain_tail_digest(tmp_path / "absent") == ("", 0.0, {})
    audit = tmp_path / "audit"
    audit.mkdir()
    assert ap._hmac_chain_tail_digest(audit) == ("", 0.0, {"reason": "no audit log files present"})

    _write(audit / "2026-09-30.jsonl", b'{"hmac": "old"}\n')
    rows = b'{"hmac": "aaaa"}\n\n{"no": "hmac"}\nnot json\n[1, 2]\n{"hmac": "bbbbccccddddeeeeffff"}\n'
    _write(audit / "2026-10-01.jsonl", rows)
    ref, mtime, details = ap._hmac_chain_tail_digest(audit)
    assert ref == "sha256:bbbbccccddddeeeeffff"
    assert mtime == _NOW.timestamp()
    assert details == {"file": "2026-10-01.jsonl", "lines": 5, "tail_hmac": "bbbbccccddddeeee…"}


def test_audit_chain_without_any_hmac_is_pending_with_a_reason(tmp_path: Path) -> None:
    _write(tmp_path / "a.jsonl", b'{"x": 1}\n')
    assert ap._hmac_chain_tail_digest(tmp_path) == (
        "",
        _NOW.timestamp(),
        {"file": "a.jsonl", "lines": 1, "reason": "no hmac field found"},
    )


def test_capability_matrix_summary(tmp_path: Path) -> None:
    assert ap._capability_matrix_summary(tmp_path / "absent") == ("", 0.0, {})
    runs = tmp_path / "caps"
    runs.mkdir()
    assert ap._capability_matrix_summary(runs) == ("", 0.0, {"reason": "no capability runs recorded"})

    _write(runs / "a.json", b"{}")
    latest = json.dumps({"tools": ["bash"], "violations": ["v1"]}).encode()
    _write(runs / "b.json", latest)
    assert ap._capability_matrix_summary(runs) == (
        _sha(latest),
        _NOW.timestamp(),
        {"file": "b.json", "run_count": 2, "latest_tools": ["bash"], "violations": ["v1"]},
    )


def test_capability_matrix_defaults_to_empty_lists(tmp_path: Path) -> None:
    _write(tmp_path / "a.json", b"not json")
    _ref, _mtime, details = ap._capability_matrix_summary(tmp_path)
    assert details["latest_tools"] == []
    assert details["violations"] == []


def test_tls_cert_log_summary(tmp_path: Path) -> None:
    assert ap._tls_cert_log_summary(tmp_path / "absent") == ("", 0.0, {})
    tls = tmp_path / "tls"
    tls.mkdir()
    assert ap._tls_cert_log_summary(tls) == ("", 0.0, {"reason": "no cluster_tls validation logs"})

    _write(tls / "a.log", b"ok\n")
    body = b'{"valid": true}'
    _write(tls / "b.json", body)
    assert ap._tls_cert_log_summary(tls) == (_sha(body), _NOW.timestamp(), {"file": "b.json", "log_count": 2})


def test_wheelhouse_verify_summary(tmp_path: Path) -> None:
    assert ap._wheelhouse_verify_summary(tmp_path / "absent") == ("", 0.0, {})
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    assert ap._wheelhouse_verify_summary(wheels) == ("", 0.0, {"reason": "no verify*.json file produced"})

    body = json.dumps({"wheels_checked": 7, "all_valid": True}).encode()
    _write(wheels / "verify-1.json", body)
    assert ap._wheelhouse_verify_summary(wheels) == (
        _sha(body),
        _NOW.timestamp(),
        {"file": "verify-1.json", "wheels_checked": 7, "all_valid": True},
    )


def test_run_log_summary(tmp_path: Path) -> None:
    assert ap._run_log_summary(tmp_path / "absent", include_since=None) == ("", 0.0, {})
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    assert ap._run_log_summary(runtime, include_since=None) == ("", 0.0, {"reason": "no run audit slices in window"})

    old = _NOW - timedelta(days=2)
    _write(runtime / "audit" / "r1.audit.jsonl", b"{}", mtime=old)
    _write(runtime / "audit" / "r2.audit.jsonl", b"{}")
    ref, mtime, details = ap._run_log_summary(runtime, include_since=None)
    expected = json.dumps(
        [{"run_id": "r1", "mtime": round(old.timestamp(), 3)}, {"run_id": "r2", "mtime": round(_NOW.timestamp(), 3)}],
        sort_keys=True,
    ).encode()
    assert ref == _sha(expected)
    assert mtime == _NOW.timestamp()
    assert details == {"run_count": 2, "latest_run_id": "r2", "since": None}


def test_without_a_window_every_run_counts(tmp_path: Path) -> None:
    """No ``include_since`` means no cutoff at all, not a cutoff at some small time."""
    epoch_ish = datetime.fromtimestamp(0.5, tz=UTC)
    _write(tmp_path / "audit" / "ancient.audit.jsonl", b"{}", mtime=epoch_ish)
    _ref, _mtime, details = ap._run_log_summary(tmp_path, include_since=None)
    assert details["run_count"] == 1


def test_run_log_window_keeps_a_run_exactly_at_the_cutoff(tmp_path: Path) -> None:
    cutoff = _NOW - timedelta(days=1)
    _write(tmp_path / "audit" / "before.audit.jsonl", b"{}", mtime=cutoff - timedelta(seconds=1))
    _write(tmp_path / "audit" / "at.audit.jsonl", b"{}", mtime=cutoff)
    _ref, _mtime, details = ap._run_log_summary(tmp_path, include_since=cutoff)
    assert details == {"run_count": 1, "latest_run_id": "at", "since": cutoff.isoformat()}


# ---------------------------------------------------------------------------
# Dispatch, staleness and the public resolver
# ---------------------------------------------------------------------------


def _one_of_each(root: Path) -> tuple[ap.EvidenceSource, ...]:
    _write(root / "policy.md", b"p\n")
    _write(root / "audit" / "a.jsonl", b'{"hmac": "h1"}\n')
    _write(root / "caps" / "a.json", b"{}")
    _write(root / "tls" / "a.log", b"ok")
    _write(root / "wheels" / "verify.json", b"{}")
    _write(root / "runtime" / "audit" / "r.audit.jsonl", b"{}")
    return (
        ap.EvidenceSource("C1", "policy_file", "policy.md", "d"),
        ap.EvidenceSource("C2", "audit_chain", "audit", "d"),
        ap.EvidenceSource("C3", "capability_matrix", "caps", "d"),
        ap.EvidenceSource("C4", "tls_cert_log", "tls", "d"),
        ap.EvidenceSource("C5", "wheelhouse_verify", "wheels", "d"),
        ap.EvidenceSource("C6", "run_log", "runtime", "d"),
    )


def test_each_kind_is_routed_to_its_own_resolver(tmp_path: Path) -> None:
    resolved = ap.resolve_evidence_sources(_one_of_each(tmp_path), workdir=tmp_path, now=_NOW)
    assert [r.status for r in resolved] == [ap.STATUS_OK] * 6
    refs = {r.source.control_id: r.evidence_ref for r in resolved}
    assert refs["C1"] == f"evidence: policy.md ({_sha(b'p' + bytes([10]))})"
    assert refs["C2"] == "evidence: audit (sha256:h1)"
    assert refs["C3"] == f"evidence: caps ({_sha(b'{}')})"
    assert refs["C4"] == f"evidence: tls ({_sha(b'ok')})"
    assert refs["C5"] == f"evidence: wheels ({_sha(b'{}')})"
    assert resolved[5].details["latest_run_id"] == "r"
    assert all(r.last_modified == _NOW.timestamp() for r in resolved)


def test_a_missing_artefact_is_pending(tmp_path: Path) -> None:
    (resolved,) = ap.resolve_evidence_sources(
        (ap.EvidenceSource("C1", "policy_file", "absent.md", "d"),), workdir=tmp_path, now=_NOW
    )
    assert resolved.status == ap.STATUS_PENDING
    assert resolved.evidence_ref == ap.PENDING_EVIDENCE
    assert resolved.last_modified == 0.0


@pytest.mark.parametrize(
    ("age", "status"),
    [(timedelta(days=30), ap.STATUS_OK), (timedelta(days=30, hours=6), ap.STATUS_STALE)],
)
def test_staleness_starts_after_the_window(tmp_path: Path, age: timedelta, status: str) -> None:
    _write(tmp_path / "policy.md", b"p", mtime=_NOW - age)
    (resolved,) = ap.resolve_evidence_sources(
        (ap.EvidenceSource("C1", "policy_file", "policy.md", "d"),),
        workdir=tmp_path,
        now=_NOW,
        stale_after_days=30,
    )
    assert resolved.status == status


def test_a_stale_artefact_names_its_age(tmp_path: Path) -> None:
    _write(tmp_path / "policy.md", b"p", mtime=_NOW - timedelta(days=40, hours=3))
    (resolved,) = ap.resolve_evidence_sources(
        (ap.EvidenceSource("C1", "policy_file", "policy.md", "d"),), workdir=tmp_path, now=_NOW
    )
    assert resolved.details["age_days"] == 40.1
    assert resolved.evidence_ref == f"evidence: {_sha(b'p')} (stale, age=40.1d)"
    assert resolved.last_modified == (_NOW - timedelta(days=40, hours=3)).timestamp()


def test_the_given_workdir_and_clock_are_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    _write(project / "policy.md", b"p", mtime=_NOW - timedelta(days=1))
    monkeypatch.chdir(tmp_path)
    (resolved,) = ap.resolve_evidence_sources(
        (ap.EvidenceSource("C1", "policy_file", "policy.md", "d"),), workdir=project, now=_NOW
    )
    # The workdir, not the cwd, is resolved against; a real clock would read
    # the artefact as years old.
    assert resolved.status == ap.STATUS_OK


def test_without_a_workdir_the_cwd_is_resolved_against(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "policy.md", b"p", mtime=_NOW)
    monkeypatch.chdir(tmp_path)
    (resolved,) = ap.resolve_evidence_sources((ap.EvidenceSource("C1", "policy_file", "policy.md", "d"),), now=_NOW)
    assert resolved.status == ap.STATUS_OK


# ---------------------------------------------------------------------------
# Rendering and the manifest
# ---------------------------------------------------------------------------


def _resolved(status: str, details: dict[str, object] | None = None) -> ap.ResolvedEvidence:
    return ap.ResolvedEvidence(
        source=ap.EvidenceSource("CC1.1", "policy_file", "p.md", "desc"),
        status=status,  # type: ignore[arg-type]
        evidence_ref="ref",
        details=details or {},
    )


def test_details_render_as_sorted_pairs_and_empty_as_nothing() -> None:
    assert ap._format_details({}) == ""
    assert ap._format_details({"b": 2, "a": "x"}) == "a='x'; b=2"


def test_markdown_counts_each_status_and_uses_the_given_clock() -> None:
    entries = [
        _resolved(ap.STATUS_OK),
        _resolved(ap.STATUS_OK, {"k": 1}),
        _resolved(ap.STATUS_PENDING),
        _resolved(ap.STATUS_STALE),
        _resolved(ap.STATUS_STALE),
    ]
    body = ap.render_markdown(entries, period_label="Q3", generated_at=_NOW)
    assert body.startswith("# SOC 2 Evidence Checklist\n")
    assert "_Period: Q3 | Generated: 2026-10-01T12:00:00Z_" in body
    assert "- Sources resolved: **5**" in body
    assert "- OK: **2**" in body
    assert "- Pending: **1**" in body
    assert "- Stale: **2**" in body
    assert "| CC1.1 | `OK` | ref <br/>_k=1_ | desc |" in body
    assert body.endswith("\n")


def test_manifest_shape_is_pinned() -> None:
    manifest = ap._build_manifest([_resolved(ap.STATUS_OK)], period_label="Q3", generated_at=_NOW)
    assert manifest["schema_version"] == 1
    assert manifest["report_type"] == "soc2_evidence_pack"
    assert manifest["generated_at"] == "2026-10-01T12:00:00Z"
    assert manifest["evidence"][0]["status"] == "OK"
