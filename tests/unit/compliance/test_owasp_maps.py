"""Unit tests for the OWASP ASI / AST evidence-pack control maps.

Covers:

* Registration: ``owasp-asi`` and ``owasp-skills`` are in
  ``SUPPORTED_STANDARDS`` and resolve via ``get_standard_map``.
* Shape: each map has the ten canonical control ids, a regulation
  label, and every control carries the required keys with a valid
  status.
* Honesty: partial controls are explicitly marked ``"partial"`` (the
  maps are not all-green).
* Grounding: every ``selector`` event-type token cited by the maps is a
  literal ``event_type`` string that actually appears in the Bernstein
  source tree (no phantom mechanisms).
* End-to-end: ``build_evidence_pack`` produces a well-formed, layout-
  complete zip for both new standards whose ``controls.json`` matches
  the map and whose manifest hashes agree with the on-disk bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from bernstein.compliance import owasp_asi, owasp_skills
from bernstein.compliance.evidence_pack import (
    SUPPORTED_STANDARDS,
    build_evidence_pack,
    get_standard_map,
)

# Audit attribute names that are legitimately used as selectors but are
# not themselves ``event_type`` literals (they are event fields / lineage
# fields). These are excluded from the "must be a real event_type" check.
_NON_EVENT_SELECTOR_TOKENS: frozenset[str] = frozenset(
    {
        "content_hash",
        "parent_hashes",
        "model",
        "task_id",
        "usd",
    }
)

_ASI_IDS = tuple(f"ASI{n:02d}" for n in range(1, 11))
_AST_IDS = tuple(f"AST{n:02d}" for n in range(1, 11))


def _source_event_types() -> set[str]:
    """Collect every literal ``event_type`` string used in the src tree.

    Scans for both ``event_type="..."`` call sites and ``EVENT_* =
    "..."`` / ``EVENT_TYPE = "..."`` constant definitions, so a selector
    that names a constant's value counts as grounded.
    """
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


@pytest.mark.parametrize("standard", ["owasp-asi", "owasp-skills"])
def test_standard_is_registered(standard: str) -> None:
    assert standard in SUPPORTED_STANDARDS
    mapping = get_standard_map(standard)
    assert mapping["regulation"]
    assert mapping["controls"]


def test_asi_map_has_ten_canonical_controls() -> None:
    mapping = get_standard_map("owasp-asi")
    ids = [c["control_id"] for c in mapping["controls"]]
    assert ids == list(_ASI_IDS)


def test_skills_map_has_ten_canonical_controls() -> None:
    mapping = get_standard_map("owasp-skills")
    ids = [c["control_id"] for c in mapping["controls"]]
    assert ids == list(_AST_IDS)


@pytest.mark.parametrize("standard", ["owasp-asi", "owasp-skills"])
def test_every_control_has_required_keys_and_valid_status(standard: str) -> None:
    mapping = get_standard_map(standard)
    for control in mapping["controls"]:
        for key in ("control_id", "requirement", "artefact", "selector", "status"):
            assert key in control, (standard, control.get("control_id"), key)
            assert control[key] != "" or key == "selector"
        assert control["status"] in {"mapped", "partial", "todo"}


@pytest.mark.parametrize("standard", ["owasp-asi", "owasp-skills"])
def test_map_marks_some_controls_partial(standard: str) -> None:
    # An honest partial map is the deliverable: neither catalogue should
    # claim full coverage. This test fails on the pre-change code (the
    # standard is not even registered) and on any future over-claim that
    # marks everything "mapped".
    mapping = get_standard_map(standard)
    statuses = {c["control_id"]: c["status"] for c in mapping["controls"]}
    assert "partial" in statuses.values(), statuses


@pytest.mark.parametrize("standard", ["owasp-asi", "owasp-skills"])
def test_selectors_reference_real_event_types(
    standard: str,
    source_event_types: set[str],
) -> None:
    mapping = get_standard_map(standard)
    unknown: list[tuple[str, str]] = []
    for control in mapping["controls"]:
        for token in str(control["selector"]).split(","):
            token = token.strip()
            if not token or token in _NON_EVENT_SELECTOR_TOKENS:
                continue
            # A grounded selector token is either a literal event_type in
            # the source tree, or a dotted/underscored event name that
            # appears verbatim in a source file (constant value).
            if token not in source_event_types:
                unknown.append((control["control_id"], token))
    assert not unknown, f"{standard} cites event types not found in src: {unknown}"


def test_control_map_returns_copy() -> None:
    # Mutating the returned dict must not corrupt the module catalogue.
    first = owasp_asi.control_map()
    first["controls"][0]["status"] = "MUTATED"
    second = owasp_asi.control_map()
    assert second["controls"][0]["status"] != "MUTATED"

    first_s = owasp_skills.control_map()
    first_s["controls"][0]["status"] = "MUTATED"
    second_s = owasp_skills.control_map()
    assert second_s["controls"][0]["status"] != "MUTATED"


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
                "event_type": "capability_matrix_refusal",
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


@pytest.mark.parametrize("standard", ["owasp-asi", "owasp-skills"])
def test_build_evidence_pack_wellformed(standard: str, tmp_path: Path) -> None:
    sdd = _seed_sdd(tmp_path)
    out = tmp_path / "pack.zip"
    pack = build_evidence_pack(
        sdd_dir=sdd,
        standard=standard,
        output_path=out,
        write=True,
    )
    assert pack.standard == standard
    assert pack.event_count == 1
    assert pack.controls_mapped >= 1
    assert pack.controls_todo == 0
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
        assert controls["standard"] == standard
        assert len(controls["controls"]) == 10

        manifest = json.loads(zf.read("manifest.json"))
        # Every artefact hash in the manifest agrees with the bytes.
        for name, digest in manifest["artefacts"].items():
            if name == "manifest.json":
                continue
            assert hashlib.sha256(zf.read(name)).hexdigest() == digest, name


@pytest.mark.parametrize("standard", ["owasp-asi", "owasp-skills"])
def test_build_evidence_pack_is_deterministic(standard: str, tmp_path: Path) -> None:
    sdd = _seed_sdd(tmp_path)
    a = build_evidence_pack(sdd_dir=sdd, standard=standard, output_path=tmp_path / "a.zip")
    b = build_evidence_pack(sdd_dir=sdd, standard=standard, output_path=tmp_path / "b.zip")
    assert a.sha256 == b.sha256
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()
