"""``docs/security/nist-ai-100-2-crosswalk.md`` must name real controls (#6216).

The crosswalk renders the guardrail stack in the identifiers an
application-security team already uses when it threat-models the rest of
its ML stack with NIST AI 100-2. A rendered identifier that no longer
exists is worse than a missing row: the reader maps a taxonomy class to a
control they then go and search for, and a rename in code turns the page
into a claim about nothing. Nothing compared the two files, so a stale
name could sit there until someone happened to search for it.

This test reads the real registries - the detector pack, the guardrail
pipeline, the chain schema - and fails on any name in the page that is not
in one of them. It also pins the revision the page maps and the set of
taxonomy sections it must cover, so a later NIST revision cannot be
silently answered by the old mapping.
"""

from __future__ import annotations

import re
from pathlib import Path

from bernstein.core.security import audit_chain
from bernstein.core.security.guardrail_pipeline import GuardrailPipeline
from bernstein.core.security.owasp_asi_detectors import DEFAULT_DETECTORS, OwaspAsiGuardrail

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs" / "security" / "nist-ai-100-2-crosswalk.md"

#: The revision the page maps. A new NIST revision means re-reading the
#: taxonomy, not editing this string.
_EXPECTED_REVISION = "NIST AI 100-2e2025"
_EXPECTED_APPROVAL_DATE = "2025-03-20"

#: Taxonomy sections the page must answer, so coverage cannot shrink by
#: deleting rows. Every one of these is either an attack class that reaches
#: an agent runtime or a PredAI class that has to be dismissed with a reason.
_REQUIRED_SECTIONS = ("§2.2", "§2.3", "§2.4", "§3.2", "§3.3", "§3.4", "§3.5")

_DETECTOR_RE = re.compile(r"`(detect_asi\d\d_[a-z_]+)`")
_GUARDRAIL_RE = re.compile(r"`([A-Z][A-Za-z]*Guardrail)`")
_EVENT_RE = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)`")
_VERDICTS = ("Covered", "Partial", "Not covered", "Not applicable")


def _rows() -> list[tuple[str, str, str, str]]:
    """Every four-column table row in the page, as (class, control, event, verdict)."""
    rows: list[tuple[str, str, str, str]] = []
    for line in _DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) != 4:
            continue
        if cells[0] in ("NIST AI 100-2 class",) or set(cells[1]) <= {"-", " "}:
            continue
        rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def _detector_registry() -> set[str]:
    return {detector.__name__ for detector in DEFAULT_DETECTORS}


def _guardrail_registry() -> set[str]:
    names = {type(guardrail).__name__ for guardrail in GuardrailPipeline.default().guardrails}
    # The OWASP pack is on by default but can be switched off by env, and the
    # page is entitled to name it either way.
    names.add(OwaspAsiGuardrail.__name__)
    return names


def _chain_event_values() -> set[str]:
    """Every event type the chain schema declares."""
    return {value for name, value in vars(audit_chain).items() if name.startswith("EVENT_") and isinstance(value, str)}


def test_the_page_still_has_its_tables() -> None:
    rows = _rows()
    assert rows, "the crosswalk table regex matched nothing - did the table layout change?"
    assert len(rows) >= 12, f"expected both tables to survive, found {len(rows)} rows"


def test_every_row_states_a_verdict() -> None:
    offenders = [
        class_name
        for class_name, _control, _event, verdict in _rows()
        if not any(verdict.startswith(verdict_word) for verdict_word in _VERDICTS)
    ]
    assert not offenders, "rows without a Covered/Partial/Not covered/Not applicable verdict: " + ", ".join(offenders)


def test_every_detector_named_in_the_crosswalk_is_in_the_registry() -> None:
    text = _DOC.read_text(encoding="utf-8")
    named = set(_DETECTOR_RE.findall(text))
    assert len(named) >= 3, f"expected the page to name several detectors, found {sorted(named)}"

    registry = _detector_registry()
    unknown = sorted(named - registry)
    assert not unknown, "crosswalk names detectors that are not in DEFAULT_DETECTORS: " + ", ".join(unknown)


def test_every_guardrail_named_in_the_crosswalk_is_in_the_pipeline_registry() -> None:
    text = _DOC.read_text(encoding="utf-8")
    named = set(_GUARDRAIL_RE.findall(text))
    assert len(named) >= 2, f"expected the page to name guardrail classes, found {sorted(named)}"

    registry = _guardrail_registry()
    unknown = sorted(named - registry)
    assert not unknown, "crosswalk names guardrails that the pipeline does not register: " + ", ".join(unknown)


def test_every_chain_event_named_in_the_crosswalk_is_in_the_chain_schema() -> None:
    events = {token for _class, _control, event_cell, _verdict in _rows() for token in _EVENT_RE.findall(event_cell)}
    assert len(events) >= 8, f"expected the page to name several chain events, found {sorted(events)}"

    schema = _chain_event_values()
    assert schema, "the chain schema exposed no EVENT_ constants - did the module move?"
    unknown = sorted(events - schema)
    assert not unknown, "crosswalk names chain events the schema does not declare: " + ", ".join(unknown)


def test_the_page_records_the_revision_it_maps() -> None:
    text = _DOC.read_text(encoding="utf-8")
    assert _EXPECTED_REVISION in text, "the crosswalk must state which NIST revision it maps"
    assert _EXPECTED_APPROVAL_DATE in text, "the crosswalk must date the revision it maps"


def test_the_page_covers_the_required_taxonomy_sections() -> None:
    text = _DOC.read_text(encoding="utf-8")
    missing = [section for section in _REQUIRED_SECTIONS if section not in text]
    assert not missing, "crosswalk no longer covers taxonomy sections: " + ", ".join(missing)
