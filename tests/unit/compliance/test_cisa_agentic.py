"""Tests for the CISA / Five Eyes agentic-AI evidence-pack map."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from bernstein.compliance import cisa_agentic
from bernstein.compliance.evidence_pack import (
    SUPPORTED_STANDARDS,
    build_evidence_pack,
    get_standard_map,
)


_CISA_IDS = tuple(f"CISA-{n:02d}" for n in range(1, 24))


def _source_event_types() -> set[str]:
    """Collect event-type values defined by Bernstein's source tree."""
    src_root = Path(__file__).resolve().parents[3] / "src" / "bernstein"
    assert src_root.is_dir(), src_root

    found: set[str] = set()
    call_pat = re.compile(r'event_type\s*=\s*["\']([a-z0-9_.]+)["\']')
    const_pat = re.compile(r'EVENT[A-Z0-9_]*\s*=\s*["\']([a-z0-9_.]+)["\']')

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


def test_cisa_agentic_is_registered() -> None:
    assert "cisa-agentic" in SUPPORTED_STANDARDS

    mapping = get_standard_map("cisa-agentic")

    assert mapping["regulation"]
    assert mapping["controls"]


def test_cisa_agentic_has_23_canonical_controls() -> None:
    mapping = get_standard_map("cisa-agentic")

    ids = [control["control_id"] for control in mapping["controls"]]

    assert ids == list(_CISA_IDS)


def test_cisa_controls_have_required_fields_and_valid_status() -> None:
    mapping = get_standard_map("cisa-agentic")

    for control in mapping["controls"]:
        for key in (
            "control_id",
            "category",
            "risk",
            "mechanism",
            "module",
            "event_type",
            "status",
        ):
            assert key in control, (control.get("control_id"), key)
            if key == "event_type" and control["status"] == "todo":
                continue
            assert control[key] != ""

        assert control["status"] in {"mapped", "partial", "todo"}


def test_cisa_map_is_conservatively_partial() -> None:
    mapping = get_standard_map("cisa-agentic")
    statuses = {control["status"] for control in mapping["controls"]}

    assert "partial" in statuses


def test_cisa_selectors_reference_real_event_types(
    source_event_types: set[str],
) -> None:
    mapping = get_standard_map("cisa-agentic")

    unknown: list[tuple[str, str]] = []

    for control in mapping["controls"]:
        for token in str(control["event_type"]).split(","):
            token = token.strip()
            if not token:
                continue

            if token not in source_event_types:
                unknown.append((control["control_id"], token))

    assert not unknown, (
        "cisa-agentic cites event types not defined in src: "
        f"{unknown}"
    )


def test_cisa_control_map_returns_copy() -> None:
    first = cisa_agentic.control_map()
    first["controls"][0]["status"] = "MUTATED"

    second = cisa_agentic.control_map()

    assert second["controls"][0]["status"] != "MUTATED"


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


def test_build_cisa_evidence_pack(tmp_path: Path) -> None:
    sdd = _seed_sdd(tmp_path)
    out = tmp_path / "cisa-agentic.zip"

    pack = build_evidence_pack(
        sdd_dir=sdd,
        standard="cisa-agentic",
        output_path=out,
        write=True,
    )

    assert pack.standard == "cisa-agentic"
    assert pack.event_count == 1
    assert pack.controls_mapped > 0
    assert pack.controls_partial > 0
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

        assert controls["standard"] == "cisa-agentic"
        assert len(controls["controls"]) == 23
        assert [c["control_id"] for c in controls["controls"]] == list(_CISA_IDS)

        manifest = json.loads(zf.read("manifest.json"))

        assert manifest["controls_mapped"] == pack.controls_mapped
        assert manifest["controls_partial"] == pack.controls_partial
        assert manifest["controls_todo"] == pack.controls_todo

        for name, digest in manifest["artefacts"].items():
            if name == "manifest.json":
                continue

            assert hashlib.sha256(zf.read(name)).hexdigest() == digest, name


def test_build_cisa_evidence_pack_is_deterministic(tmp_path: Path) -> None:
    sdd = _seed_sdd(tmp_path)

    a = build_evidence_pack(
        sdd_dir=sdd,
        standard="cisa-agentic",
        output_path=tmp_path / "a.zip",
    )
    b = build_evidence_pack(
        sdd_dir=sdd,
        standard="cisa-agentic",
        output_path=tmp_path / "b.zip",
    )

    assert a.sha256 == b.sha256
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()
