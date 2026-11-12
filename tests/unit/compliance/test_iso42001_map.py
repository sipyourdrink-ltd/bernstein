"""Unit tests for the ISO/IEC 42001 evidence-pack control map (issue #3238).

Covers:

* Registration: ``iso-42001`` is in ``SUPPORTED_STANDARDS`` and resolves via
  ``get_standard_map``.
* Shape: every control carries the required keys and a valid status.
* Completeness: every mapped control resolves to exactly one of ``mapped``
  / ``partial`` / ``organisational`` and the status counters over the map
  sum to ``len(CONTROLS)`` - no control is silently absent from the pack
  summary.
* Honesty: the map declares controls it cannot evidence as
  ``"organisational"`` rather than overclaiming, and marks some controls
  ``"partial"`` (it is not an all-green map).
* Grounding: every ``selector`` event-type token cited by the map is a
  literal ``event_type`` string that actually appears in the Bernstein
  source tree (no phantom mechanisms).
* End-to-end: ``build_evidence_pack`` produces a well-formed pack for
  ``iso-42001`` whose ``controls.json`` matches the map and whose manifest
  counts the organisational controls rather than dropping them.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from bernstein.compliance import evidence_pack, iso42001
from bernstein.compliance.evidence_pack import (
    SUPPORTED_STANDARDS,
    build_evidence_pack,
    get_standard_map,
)

# Selector tokens that are legitimate evidence but are not themselves
# ``event_type`` literals (lineage / cost-ledger / data-catalog fields, or
# the "n/a" placeholder used on organisational controls).
_NON_EVENT_SELECTOR_TOKENS: frozenset[str] = frozenset(
    {
        "content_hash",
        "parent_hashes",
        "model",
        "task_id",
        "usd",
        "resource_type",
        "resource_id",
        "n/a",
    }
)

_VALID_STATUSES: frozenset[str] = frozenset({"mapped", "partial", "organisational"})


def _source_event_types() -> set[str]:
    """Collect every literal ``event_type`` string used in the src tree."""
    src_root = Path(__file__).resolve().parents[3] / "src" / "bernstein"
    assert src_root.is_dir(), src_root
    found: set[str] = set()
    call_pat = re.compile(r'event_type\s*=\s*["\']([a-z0-9_.]+)["\']')
    const_pat = re.compile(r'EVENT[A-Z_]*\s*=\s*["\']([a-z0-9_.]+)["\']')
    for path in src_root.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        found.update(call_pat.findall(text))
        found.update(const_pat.findall(text))
    return found


@pytest.fixture(scope="module")
def source_event_types() -> set[str]:
    return _source_event_types()


def test_standard_is_registered() -> None:
    assert "iso-42001" in SUPPORTED_STANDARDS
    mapping = get_standard_map("iso-42001")
    assert mapping["regulation"]
    assert mapping["controls"]


def test_iso42005_documentation_contract() -> None:
    docs = Path(__file__).resolve().parents[3] / "docs" / "compliance"
    page = docs / "iso42005-mapping.md"
    assert page.is_file(), "ISO/IEC 42005 mapping page is missing"
    text = page.read_text(encoding="utf-8")
    first_paragraph = " ".join(text.split("\n\n")[1].split())
    for phrase in (
        "ISO/IEC 42005:2025 provides guidance for performing an impact assessment",
        "evidence for the operator's own assessment",
        "no compliance or certification claim",
    ):
        assert phrase in first_paragraph

    coverage = text.split("## Coverage\n", 1)[1].split("\n## ", 1)[0]
    rows = [
        [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        for line in coverage.splitlines()
        if line.startswith("|")
    ][2:]
    expected = {
        "impact.intended-use": ("Intended use", "compliance preset", "purpose"),
        "impact.affected-parties": ("Affected parties", "lineage receipts", "stakeholder"),
        "impact.foreseeable-misuse": ("Foreseeable misuse", "incident pack", "scenarios"),
        "impact.mitigations": ("Mitigations", "guardrail inventory", "effectiveness"),
        "impact.review-cadence": ("Review cadence", "incident pack", "schedule"),
    }
    assert len(rows) == len(expected)
    assert {row[1] for row in rows} == set(expected)  # Exact IDs, each once.
    for element, control_id, mechanism, verdict, gaps in rows:
        label, mechanism_hint, gap_hint = expected[control_id]
        assert element == label
        assert mechanism_hint in mechanism
        assert verdict in {"Covered", "Partial", "Not-covered"}
        assert gap_hint in gaps

    contract = " ".join(text.split())
    for phrase in (
        "Bernstein identifiers, not ISO clause numbers",
        "Covered → `mapped`",
        "Partial → `partial`",
        "Not-covered → `organisational` for explicitly operator-owned content/processes, otherwise `todo`",
        'both `artefact` and `selector` are `"n/a"`',
        "State gaps in `requirement`",
        "unclassified known element defaults to Not-covered/`todo`",
        "no element defaults to Covered",
        "Missing, duplicate or unknown IDs",
        "invalid verdicts/statuses raise `ValueError`",
        "do not preapprove any individual Covered verdict",
        "contract for the subsequent exporter implementation",
        "not behaviour delivered by this documentation change",
        "For `iso-42005` only",
        "`evidence_refs: list[str]`, default `[]`",
        "sorted, deduplicated `<member>:<positive-1-based-line>` strings",
        "final serialized `audit-chain/events.jsonl` or `lineage/log.jsonl` members",
        "Catalogue verdicts describe capability ceilings",
        "copied controls",
        "private, element-specific predicates over the existing filtered records",
        "`mapped` or `partial` status only when its predicate matches and references resolve",
        "otherwise emit `todo`",
        "Retain `organisational`",
        "recompute summary counters from exported statuses",
        "Unknown matching rules",
        "invalid, dangling or nonmatching references raise `ValueError` before writing",
        "Unrelated records, generic policy files, inventories and provenance hashes alone",
        "cannot establish assessment content, including intended use or affected parties",
        "empty window remains valid",
        "empty references and zero mapped/partial controls",
        "Resolving references establishes evidence presence",
        "chain authentication remains a separate verification procedure",
        "Existing standards, API signatures, filters, source collection, shared evidence-member bytes",
        "schema version `1.0.0` remain unchanged",
        "No migration or additional authority",
    ):
        assert phrase in contract, phrase

    iso42001 = " ".join((docs / "iso42001-mapping.md").read_text(encoding="utf-8").split())
    assert "## Role of ISO/IEC 42006:2025" in iso42001
    for phrase in (
        "requirements for bodies auditing and certifying ISO/IEC 42001 management systems",
        "affects the evidence they require",
        "does not certify Bernstein",
        "ISO/IEC 42005:2025",
    ):
        assert phrase in iso42001


def test_every_control_has_required_keys_and_valid_status() -> None:
    mapping = get_standard_map("iso-42001")
    for control in mapping["controls"]:
        for key in ("control_id", "requirement", "artefact", "selector", "status"):
            assert key in control, (control.get("control_id"), key)
            assert control[key] != "", (control.get("control_id"), key)
        assert control["status"] in _VALID_STATUSES, control


def test_every_iso42001_control_is_counted_exactly_once() -> None:
    # The property the "Start here" brief names directly: the status
    # counters over the ISO map must sum to len(CONTROLS) - no control is
    # silently absent from the pack summary. This fails on the
    # pre-change tree because the standard is not registered at all, and
    # would also fail if a fourth status value slipped in uncounted.
    mapping = get_standard_map("iso-42001")
    controls = mapping["controls"]
    counted = sum(1 for c in controls if c["status"] in _VALID_STATUSES)
    assert counted == len(controls)

    statuses = [c["status"] for c in controls]
    mapped = statuses.count("mapped")
    partial = statuses.count("partial")
    organisational = statuses.count("organisational")
    assert mapped + partial + organisational == len(controls)


def test_map_declares_organisational_controls() -> None:
    # The honesty rule this module exists to enforce: controls the tool
    # cannot evidence are named "organisational", not silently marked
    # "mapped" or dropped.
    mapping = get_standard_map("iso-42001")
    statuses = {c["control_id"]: c["status"] for c in mapping["controls"]}
    assert "organisational" in statuses.values(), statuses


def test_map_marks_some_controls_partial() -> None:
    mapping = get_standard_map("iso-42001")
    statuses = {c["control_id"]: c["status"] for c in mapping["controls"]}
    assert "partial" in statuses.values(), statuses


def test_organisational_controls_carry_no_chain_selector() -> None:
    # An "organisational" control that cited a real event_type would be
    # claiming chain evidence it does not have.
    mapping = get_standard_map("iso-42001")
    for control in mapping["controls"]:
        if control["status"] == "organisational":
            assert control["selector"] == "n/a", control
            assert control["artefact"] == "n/a", control


def test_selectors_reference_real_event_types(source_event_types: set[str]) -> None:
    mapping = get_standard_map("iso-42001")
    unknown: list[tuple[str, str]] = []
    for control in mapping["controls"]:
        for token in str(control["selector"]).split(","):
            token = token.strip()
            if not token or token in _NON_EVENT_SELECTOR_TOKENS:
                continue
            if token not in source_event_types:
                unknown.append((control["control_id"], token))
    assert not unknown, f"iso-42001 cites event types not found in src: {unknown}"


def test_control_map_returns_copy() -> None:
    first = iso42001.control_map()
    first["controls"][0]["status"] = "MUTATED"
    second = iso42001.control_map()
    assert second["controls"][0]["status"] != "MUTATED"


# ---------------------------------------------------------------------------
# End-to-end pack build
# ---------------------------------------------------------------------------


def _seed_sdd(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    audit = sdd / "audit"
    audit.mkdir(parents=True)
    (audit / "log.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-01-05T10:00:00+00:00",
                "event_type": "task.transition",
                "actor": "agent",
                "resource_type": "task",
                "resource_id": "T-1",
                "hmac": "a" * 64,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return sdd


def test_build_evidence_pack_wellformed(tmp_path: Path) -> None:
    sdd = _seed_sdd(tmp_path)
    out = tmp_path / "pack.zip"
    pack = build_evidence_pack(
        sdd_dir=sdd,
        standard="iso-42001",
        output_path=out,
        write=True,
    )
    assert pack.standard == "iso-42001"
    assert pack.event_count == 1

    mapping = get_standard_map("iso-42001")
    n_controls = len(mapping["controls"])
    # No control silently dropped from the summary: mapped + partial +
    # organisational + todo must reach every control in the map.
    assert (
        pack.controls_mapped + pack.controls_partial + pack.controls_organisational + pack.controls_todo == n_controls
    )
    assert pack.controls_organisational > 0
    assert out.is_file()

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        for required in (
            "manifest.json",
            "controls.json",
            "audit-chain/events.jsonl",
            "lineage/log.jsonl",
            "costs/cost_history.jsonl",
            "README.md",
        ):
            assert required in names, required

        controls = json.loads(zf.read("controls.json"))
        assert controls["standard"] == "iso-42001"
        assert len(controls["controls"]) == n_controls

        manifest = json.loads(zf.read("manifest.json"))
        # The organisational count reaches the manifest and the to_dict
        # summary, so an operator reading either sees which controls are
        # theirs to answer rather than a pack that looks complete.
        assert manifest["controls_organisational"] == pack.controls_organisational
        assert pack.to_dict()["controls_organisational"] == pack.controls_organisational
        for name, digest in manifest["artefacts"].items():
            if name == "manifest.json":
                continue
            assert hashlib.sha256(zf.read(name)).hexdigest() == digest, name


def test_build_evidence_pack_is_deterministic(tmp_path: Path) -> None:
    sdd = _seed_sdd(tmp_path)
    a = build_evidence_pack(sdd_dir=sdd, standard="iso-42001", output_path=tmp_path / "a.zip")
    b = build_evidence_pack(sdd_dir=sdd, standard="iso-42001", output_path=tmp_path / "b.zip")
    assert a.sha256 == b.sha256
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()


def test_iso42005_export_references_only_matching_records(tmp_path: Path) -> None:
    sdd = _seed_sdd(tmp_path)
    audit_log = sdd / "audit" / "log.jsonl"
    unrelated = json.loads(audit_log.read_text(encoding="utf-8"))
    refusal = {
        **unrelated,
        "timestamp": "2026-01-06T10:00:00+00:00",
        "event_type": "capability_matrix_refusal",
        "details": {"mode": "enforce", "reason": "fixture", "offending_tools": ["example"], "chain": ["parent"]},
    }
    later = {**refusal, "timestamp": "2026-01-07T10:00:00+00:00", "details": {**refusal["details"], "mode": "audit"}}
    audit_log.write_text("".join(json.dumps(event) + "\n" for event in (later, refusal, unrelated)), encoding="utf-8")
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    a = build_evidence_pack(sdd_dir=sdd, standard="iso-42005", output_path=first)
    b = build_evidence_pack(sdd_dir=sdd, standard="iso-42005", output_path=second)
    assert a.sha256 == b.sha256
    assert first.read_bytes() == second.read_bytes()
    assert a.controls_mapped == 0
    assert a.controls_partial == 2

    with zipfile.ZipFile(first) as archive:
        rows = json.loads(archive.read("controls.json"))["controls"]
        refs = {row["control_id"]: row["evidence_refs"] for row in rows if row["evidence_refs"]}
        assert refs == {
            "impact.foreseeable-misuse": ["audit-chain/events.jsonl:2"],
            "impact.mitigations": ["audit-chain/events.jsonl:2"],
        }
        for member, line in (ref.rsplit(":", 1) for refs_for_row in refs.values() for ref in refs_for_row):
            assert json.loads(archive.read(member).splitlines()[int(line) - 1]) == refusal

    for name, since in (("audit-only", later["timestamp"]), ("empty", "2026-02-01T00:00:00+00:00")):
        output = tmp_path / f"{name}.zip"
        pack = build_evidence_pack(sdd_dir=sdd, standard="iso-42005", since=since, output_path=output)
        assert pack.event_count == (1 if name == "audit-only" else 0)
        assert (pack.controls_mapped, pack.controls_partial, pack.controls_organisational, pack.controls_todo) == (
            0,
            0,
            3,
            2,
        )
        with zipfile.ZipFile(output) as archive:
            assert all(not row["evidence_refs"] for row in json.loads(archive.read("controls.json"))["controls"])


@pytest.mark.parametrize("field", ["verdict", "status"])
def test_iso42005_export_rejects_non_string_catalogue_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    original = get_standard_map("iso-42005")
    catalogue = deepcopy(original)
    catalogue["controls"][0][field] = []
    output = tmp_path / "invalid.zip"
    sdd = _seed_sdd(tmp_path)
    with monkeypatch.context() as patch:
        patch.setitem(evidence_pack._STANDARD_MAPS, "iso-42005", catalogue)
        with pytest.raises(ValueError, match="invalid ISO 42005 verdict/status"):
            build_evidence_pack(sdd_dir=sdd, standard="iso-42005", output_path=output)
    assert not output.exists()
    assert get_standard_map("iso-42005") is original
    assert build_evidence_pack(sdd_dir=sdd, standard="iso-42005", output_path=output).event_count == 1
