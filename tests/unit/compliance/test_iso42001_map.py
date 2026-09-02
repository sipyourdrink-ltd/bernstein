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
from pathlib import Path

import pytest

from bernstein.compliance import iso42001
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
