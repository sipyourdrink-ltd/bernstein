"""Unit tests for the one-command compliance evidence pack (issue #1316).

Covers:

* Standard-map resolution (only ai-act is exposed at MVP).
* End-to-end build: pack contains the expected zip layout and the
  per-artefact SHA-256 hashes in ``manifest.json`` agree with the
  on-disk content.
* Task scoping: ``--task <id>`` only keeps events whose
  ``resource_id`` (or details.task_id) matches.
* Time filtering: ``--since`` clips audit events strictly before the
  bound.
* Determinism: two builds of the same input produce a byte-identical
  zip with matching ``sha256``.
* Unsupported standards (dora, finos-aigf) raise ``ValueError`` rather
  than emitting a TODO-only bundle.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from bernstein.compliance.evidence_pack import (
    SUPPORTED_STANDARDS,
    EvidencePack,
    build_evidence_pack,
    get_standard_map,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")


@pytest.fixture
def sdd_dir(tmp_path: Path) -> Path:
    """Seed a synthetic .sdd tree the evidence pack will read from."""
    sdd = tmp_path / ".sdd"

    audit_events = [
        {
            "timestamp": "2026-01-05T10:00:00+00:00",
            "event_type": "task.created",
            "actor": "alice",
            "resource_type": "task",
            "resource_id": "T-1",
            "details": {"role": "backend"},
            "hmac": "a" * 64,
            "prev_hmac": "0" * 64,
        },
        {
            "timestamp": "2026-01-05T11:00:00+00:00",
            "event_type": "agent.spawned",
            "actor": "orchestrator",
            "resource_type": "agent",
            "resource_id": "A-1",
            "details": {"task_id": "T-1"},
            "hmac": "b" * 64,
            "prev_hmac": "a" * 64,
        },
        {
            "timestamp": "2026-02-10T09:00:00+00:00",
            "event_type": "task.completed",
            "actor": "alice",
            "resource_type": "task",
            "resource_id": "T-2",
            "details": {"status": "ok"},
            "hmac": "c" * 64,
            "prev_hmac": "b" * 64,
        },
    ]
    _write_jsonl(sdd / "audit" / "2026-01-05.jsonl", audit_events[:2])
    _write_jsonl(sdd / "audit" / "2026-02-10.jsonl", audit_events[2:])

    lineage = [
        {
            "timestamp": "2026-01-05T10:30:00+00:00",
            "artefact_path": "src/foo.py",
            "content_hash": "d" * 64,
            "parent_hashes": [],
            "entry_hash": "e" * 64,
            "meta": {"task_id": "T-1"},
        },
        {
            "timestamp": "2026-02-10T09:30:00+00:00",
            "artefact_path": "src/bar.py",
            "content_hash": "f" * 64,
            "parent_hashes": ["d" * 64],
            "entry_hash": "1" * 64,
            "meta": {"task_id": "T-2"},
        },
    ]
    _write_jsonl(sdd / "lineage" / "log.jsonl", lineage)

    costs = [
        {"date": "2026-01-05", "task_id": "T-1", "usd": 0.42, "model": "claude-3.5"},
        {"date": "2026-02-10", "task_id": "T-2", "usd": 1.10, "model": "claude-3.5"},
    ]
    _write_jsonl(sdd / "metrics" / "cost_history.jsonl", costs)

    return sdd


# ---------------------------------------------------------------------------
# Standard map resolution
# ---------------------------------------------------------------------------


class TestStandardMap:
    def test_supported_standards_constant(self) -> None:
        # ai-act plus the two OWASP agentic-security catalogues ship with
        # reviewed control maps. DORA and FINOS AIGF remain tracked under
        # #1316 and must NOT be selectable until their clause mappings are
        # validated; emitting TODO-only bundles would mislead operators.
        assert set(SUPPORTED_STANDARDS) == {"ai-act", "owasp-asi", "owasp-skills"}
        assert "dora" not in SUPPORTED_STANDARDS
        assert "finos-aigf" not in SUPPORTED_STANDARDS

    def test_ai_act_has_real_controls(self) -> None:
        mapping = get_standard_map("ai-act")
        assert mapping["regulation"].startswith("EU AI Act")
        controls = mapping["controls"]
        assert all(c["status"] == "mapped" for c in controls)
        # Article 12 sub-clauses must be present at minimum.
        clause_ids = {c["control_id"] for c in controls}
        assert {"art-12(1)", "art-12(2)(a)", "art-12(3)"}.issubset(clause_ids)

    @pytest.mark.parametrize("standard", ["dora", "finos-aigf"])
    def test_unsupported_standards_rejected(self, standard: str) -> None:
        # Regression for the L2 bughunt: dora / finos-aigf used to be
        # selectable and emit a bundle whose only controls were
        # ``status: "todo"`` rows. They must now raise instead.
        with pytest.raises(ValueError, match="unknown standard"):
            get_standard_map(standard)

    def test_unknown_standard_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown standard"):
            get_standard_map("not-a-real-standard")


# ---------------------------------------------------------------------------
# Build: layout + hashes
# ---------------------------------------------------------------------------


class TestBuildEvidencePack:
    def test_zip_layout_and_manifest_hashes(self, sdd_dir: Path) -> None:
        pack = build_evidence_pack(
            sdd_dir=sdd_dir,
            standard="ai-act",
            since="",
            task="all",
        )

        assert isinstance(pack, EvidencePack)
        assert pack.archive_path is not None
        assert pack.archive_path.is_file()

        with zipfile.ZipFile(pack.archive_path) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "controls.json" in names
            assert "README.md" in names
            assert "audit-chain/events.jsonl" in names
            assert "audit-chain/data_catalog.json" in names
            assert "lineage/log.jsonl" in names
            assert "costs/cost_history.jsonl" in names
            # Empty operator-supplied dirs still leave placeholders so the
            # layout described in the README is always present.
            assert "policy/.empty" in names
            assert "attestations/.empty" in names

            manifest = json.loads(zf.read("manifest.json"))
            for art, expected in manifest["artefacts"].items():
                actual = hashlib.sha256(zf.read(art)).hexdigest()
                assert actual == expected, f"mismatch on {art}"

        assert pack.event_count == 3
        assert pack.lineage_count == 2
        assert pack.cost_count == 2
        assert pack.controls_mapped >= 5
        assert pack.controls_todo == 0  # ai-act is real

    def test_task_scoping_filters_events(self, sdd_dir: Path) -> None:
        pack = build_evidence_pack(
            sdd_dir=sdd_dir,
            standard="ai-act",
            since="",
            task="T-1",
        )
        with zipfile.ZipFile(pack.archive_path) as zf:  # type: ignore[arg-type]
            events_text = zf.read("audit-chain/events.jsonl").decode("utf-8")
            lines = [json.loads(ln) for ln in events_text.splitlines() if ln.strip()]
        # Two of the three audit events relate to T-1 (task.created on T-1
        # and agent.spawned whose details.task_id is T-1).
        assert {e["event_type"] for e in lines} == {"task.created", "agent.spawned"}
        assert pack.event_count == 2

    def test_since_filter_clips_old_events(self, sdd_dir: Path) -> None:
        pack = build_evidence_pack(
            sdd_dir=sdd_dir,
            standard="ai-act",
            since="2026-02-01T00:00:00+00:00",
            task="all",
        )
        with zipfile.ZipFile(pack.archive_path) as zf:  # type: ignore[arg-type]
            events_text = zf.read("audit-chain/events.jsonl").decode("utf-8")
            lines = [json.loads(ln) for ln in events_text.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert lines[0]["event_type"] == "task.completed"
        assert pack.event_count == 1

    def test_dry_run_does_not_write(self, sdd_dir: Path) -> None:
        pack = build_evidence_pack(
            sdd_dir=sdd_dir,
            standard="ai-act",
            write=False,
        )
        assert pack.archive_path is None
        assert pack.sha256  # still computed in-memory

    def test_invalid_since_rejected(self, sdd_dir: Path) -> None:
        with pytest.raises(ValueError, match="ISO-8601"):
            build_evidence_pack(
                sdd_dir=sdd_dir,
                standard="ai-act",
                since="not-a-date",
            )

    def test_unknown_standard_rejected(self, sdd_dir: Path) -> None:
        with pytest.raises(ValueError, match="unknown standard"):
            build_evidence_pack(
                sdd_dir=sdd_dir,
                standard="iso-9000",
            )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_builds_byte_identical(self, sdd_dir: Path, tmp_path: Path) -> None:
        a = tmp_path / "pack_a.zip"
        b = tmp_path / "pack_b.zip"
        first = build_evidence_pack(
            sdd_dir=sdd_dir,
            standard="ai-act",
            output_path=a,
        )
        second = build_evidence_pack(
            sdd_dir=sdd_dir,
            standard="ai-act",
            output_path=b,
        )
        assert first.sha256 == second.sha256
        assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------
# Regression: deferred standards must error, not emit TODO-only bundles
# ---------------------------------------------------------------------------


class TestDeferredStandardsRejected:
    """Regression for the L2 bughunt on issue #1316.

    Prior to the fix, ``--standard dora`` and ``--standard finos-aigf``
    were advertised by the CLI ``click.Choice`` list and produced a zip
    whose ``controls.json`` carried only ``status: "todo"`` /
    ``selector: "TODO"`` rows. That is not a useful evidence pack and
    misrepresents the project's compliance surface. Both standards now
    raise at build time until their clause maps are reviewed.
    """

    @pytest.mark.parametrize("standard", ["dora", "finos-aigf"])
    def test_deferred_standard_raises_at_build(self, sdd_dir: Path, standard: str) -> None:
        with pytest.raises(ValueError, match="unknown standard"):
            build_evidence_pack(sdd_dir=sdd_dir, standard=standard)

    @pytest.mark.parametrize("standard", ["dora", "finos-aigf"])
    def test_deferred_standard_not_in_supported_list(self, standard: str) -> None:
        assert standard not in SUPPORTED_STANDARDS

    @pytest.mark.parametrize("standard", ["dora", "finos-aigf"])
    def test_cli_choice_rejects_deferred_standards(self, tmp_path: Path, standard: str) -> None:
        """``bernstein audit export --standard {dora,finos-aigf}`` must error.

        Asserts (a) non-zero exit code and (b) the error mentions
        ``--standard`` so an operator understands the gate. This pins
        the ``click.Choice`` declaration in
        ``cli/commands/audit_cmd.py`` so the deferred standards cannot
        be re-introduced without updating both the choice list and the
        underlying control maps.
        """
        from click.testing import CliRunner

        from bernstein.cli.main import cli

        # ``audit export`` requires a ``.sdd`` directory to exist before
        # it parses the flags; seed an empty one so the click parser
        # gets the chance to reject the choice.
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["audit", "export", "--standard", standard, "--dir", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "--standard" in result.output or "'--standard'" in result.output
