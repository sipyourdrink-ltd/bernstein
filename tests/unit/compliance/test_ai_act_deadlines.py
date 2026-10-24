"""AI Act application dates come from one table, verified against the Official Journal (#6215).

Every expected value below is copied from the authentic Official Journal text,
not from the implementation:

* Regulation (EU) 2024/1689 (OJ L, 2024/1689, 12.7.2024), Article 113:
  "It shall apply from 2 August 2026. However: ... (b) Chapter III Section 4,
  Chapter V, Chapter VII and Chapter XII and Article 78 shall apply from
  2 August 2025, with the exception of Article 101". Article 111(3): "Providers
  of general-purpose AI models that have been placed on the market before
  2 August 2025 shall ... comply ... by 2 August 2027."
* Regulation (EU) 2026/1744, the Digital Omnibus on AI (OJ L, 2026/1744,
  24.7.2026, ELI http://data.europa.eu/eli/reg/2026/1744/oj), Article 1:
  - point (40)(b) replaces Article 113, third paragraph, point (c): "Chapter
    III, Sections 1, 2, and 3, with the exception of Article 6(5), shall apply
    from: (i) 2 December 2027 as regards AI systems classified as high-risk
    pursuant to Article 6(2) and Annex III; and (ii) 2 August 2028 as regards
    AI systems classified as high-risk pursuant to Article 6(1) and Annex I";
  - point (39)(b) adds Article 111(4): providers of AI systems "generating
    synthetic audio, image, video or text content, that have been placed on
    the market before 2 August 2026 shall ... comply with Article 50(2) by
    2 December 2026";
  - point (39)(a) replaces Article 111(2): high-risk systems already on the
    market before the Chapter III date are covered "only if, as from that
    date, those systems are subject to significant changes in their designs";
    those "intended to be used by public authorities" comply "by 2 August 2030";
  - point (2)(a) replaces Article 2(2): for Annex I *Section B* products "only
    Article 6(1), Article 60a and Articles 102 to 112 shall apply".
  Article 50 itself is amended only in paragraph 7 (codes of practice), so it
  keeps the general application date of Article 113.

Texts read on 2026-10-20 from EUR-Lex (CELEX 32024R1689 and 32026R1744).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import compliance_cmd
from bernstein.compliance import eu_ai_act
from bernstein.compliance.eu_ai_act import (
    AI_ACT_DATES_AS_OF,
    AI_ACT_DEADLINES,
    AI_ACT_SOURCE,
    AIActObligation,
    ComplianceEngine,
    RiskCategory,
    SystemDescriptor,
    applicable_obligations,
    requires_article49_registration,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _descriptor(**flags: bool) -> SystemDescriptor:
    descriptor = SystemDescriptor(
        name="System",
        version="1.0.0",
        description="d",
        intended_use="u",
        deployment_context="c",
    )
    for name, value in flags.items():
        assert hasattr(descriptor, name), name
        setattr(descriptor, name, value)
    return descriptor


ANNEX_III = {"used_in_employment": True}
ANNEX_I = {"annex_i_section_a_product": True}
ARTICLE_50 = {"is_chatbot_or_conversational": True}


# ---------------------------------------------------------------------------
# The table, pinned to the Official Journal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obligation", "applies_from", "reference"),
    [
        (AIActObligation.ARTICLE_50_TRANSPARENCY, date(2026, 8, 2), "Article 113, second paragraph"),
        (AIActObligation.HIGH_RISK_ANNEX_III, date(2027, 12, 2), "Article 113, third paragraph, point (c)(i)"),
        (AIActObligation.HIGH_RISK_ANNEX_I, date(2028, 8, 2), "Article 113, third paragraph, point (c)(ii)"),
        (AIActObligation.GENERAL_PURPOSE_AI_MODELS, date(2025, 8, 2), "Article 113, third paragraph, point (b)"),
    ],
)
def test_each_obligation_carries_its_official_journal_date_and_reference(
    obligation: AIActObligation, applies_from: date, reference: str
) -> None:
    row = AI_ACT_DEADLINES[obligation]
    assert row.applies_from == applies_from
    assert row.reference == reference


def test_the_table_covers_exactly_the_four_obligations() -> None:
    assert set(AI_ACT_DEADLINES) == set(AIActObligation)


@pytest.mark.parametrize(
    ("obligation", "transitional"),
    [
        (
            AIActObligation.ARTICLE_50_TRANSPARENCY,
            {("Article 111(4)", date(2026, 12, 2))},
        ),
        (
            AIActObligation.HIGH_RISK_ANNEX_III,
            {("Article 111(2)", None), ("Article 111(2)", date(2030, 8, 2))},
        ),
        (
            AIActObligation.HIGH_RISK_ANNEX_I,
            {("Article 111(2)", None), ("Article 111(2)", date(2030, 8, 2))},
        ),
        (
            AIActObligation.GENERAL_PURPOSE_AI_MODELS,
            {("Article 111(3)", date(2027, 8, 2))},
        ),
    ],
)
def test_transitional_rules_are_kept_apart_from_the_application_date(
    obligation: AIActObligation, transitional: set[tuple[str, date | None]]
) -> None:
    """A legacy-system rule is conditional; it never replaces the application date."""
    rules = AI_ACT_DEADLINES[obligation].transitional
    assert {(r.reference, r.comply_by) for r in rules} == transitional
    assert all(r.applies_to for r in rules)
    assert all(r.rule for r in rules)


def test_the_conditional_legacy_rule_has_no_date() -> None:
    """Article 111(2): a pre-existing high-risk system is covered only on a significant design change."""
    for obligation in (AIActObligation.HIGH_RISK_ANNEX_III, AIActObligation.HIGH_RISK_ANNEX_I):
        conditional = [r for r in AI_ACT_DEADLINES[obligation].transitional if r.comply_by is None]
        assert len(conditional) == 1
        assert "significant change" in conditional[0].rule


def test_the_source_cites_the_amending_regulation_in_the_official_journal() -> None:
    assert "Regulation (EU) 2024/1689" in AI_ACT_SOURCE
    assert "Regulation (EU) 2026/1744" in AI_ACT_SOURCE
    assert "http://data.europa.eu/eli/reg/2026/1744/oj" in AI_ACT_SOURCE
    assert date(2026, 10, 20) == AI_ACT_DATES_AS_OF


def test_article_111_2_is_not_cited_as_an_application_date() -> None:
    """The defect: Article 111(2) is a legacy-system rule, Article 113 sets the dates."""
    for row in AI_ACT_DEADLINES.values():
        assert row.reference.startswith("Article 113")


# ---------------------------------------------------------------------------
# Category -> obligation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (ANNEX_III, [AIActObligation.HIGH_RISK_ANNEX_III]),
        (ANNEX_I, [AIActObligation.HIGH_RISK_ANNEX_I]),
        ({**ANNEX_III, **ANNEX_I}, [AIActObligation.HIGH_RISK_ANNEX_III, AIActObligation.HIGH_RISK_ANNEX_I]),
        (ARTICLE_50, [AIActObligation.ARTICLE_50_TRANSPARENCY]),
        (
            {**ANNEX_III, **ARTICLE_50},
            [AIActObligation.HIGH_RISK_ANNEX_III, AIActObligation.ARTICLE_50_TRANSPARENCY],
        ),
        ({}, []),
        ({"social_scoring_public": True, **ANNEX_III}, []),
    ],
    ids=["annex-iii", "annex-i", "both-annexes", "article-50", "annex-iii-chatbot", "minimal", "prohibited"],
)
def test_applicable_obligations_follow_the_classification(
    flags: dict[str, bool], expected: list[AIActObligation]
) -> None:
    classification = ComplianceEngine().classify(_descriptor(**flags))
    assert applicable_obligations(classification) == expected


def test_an_annex_i_section_a_product_is_high_risk_under_article_6_1() -> None:
    result = ComplianceEngine().classify(_descriptor(**ANNEX_I))
    assert result.risk_category is RiskCategory.HIGH
    assert result.annex_i_section_a_product is True
    assert "Article 6(1)" in result.justification


@pytest.mark.parametrize(
    ("flags", "hash_on_main"),
    [
        (ANNEX_III, "4436802a8d2a3220f24a8100d9a6d19dd421709ca6baa50730249cc421c6a6a8"),
        (ARTICLE_50, "0fb80c2351c93c39ee051b1b509127e4859498ce03de423db4faac2477086785"),
    ],
)
def test_the_new_flag_does_not_change_existing_classification_hashes(flags: dict[str, bool], hash_on_main: str) -> None:
    """Evidence packages already on disk carry hashes computed before the flag existed.

    The expected values were computed on unmodified ``main`` (eeb6f3bf9).
    """
    assert ComplianceEngine().classify(_descriptor(**flags)).classification_hash == hash_on_main


def test_the_new_flag_is_part_of_the_hash_when_set() -> None:
    plain = ComplianceEngine().classify(_descriptor()).classification_hash
    annex_i = ComplianceEngine().classify(_descriptor(**ANNEX_I)).classification_hash
    assert plain != annex_i


# ---------------------------------------------------------------------------
# The report path
# ---------------------------------------------------------------------------


def test_annex_iii_and_annex_i_systems_report_different_dates() -> None:
    annex_iii = ComplianceEngine().run(_descriptor(**ANNEX_III))["compliance_summary"]
    annex_i = ComplianceEngine().run(_descriptor(**ANNEX_I))["compliance_summary"]

    assert "2 December 2027" in annex_iii["deadline"]
    assert "Annex III" in annex_iii["deadline"]
    assert "2028" not in annex_iii["deadline"]
    assert "2 August 2028" in annex_i["deadline"]
    assert "Annex I " in annex_i["deadline"] or annex_i["deadline"].rstrip(")").endswith("Annex I")
    assert "2027" not in annex_i["deadline"]


def test_article_50_output_does_not_inherit_a_high_risk_date() -> None:
    summary = ComplianceEngine().run(_descriptor(**ARTICLE_50))["compliance_summary"]

    assert "2 August 2026" in summary["deadline"]
    assert "Article 50" in summary["deadline"]
    for later in ("2027", "2028"):
        assert later not in summary["deadline"]
    assert [d["obligation"] for d in summary["deadlines"]] == ["article_50_transparency"]


def test_no_report_path_still_prints_the_old_deadline() -> None:
    """The four sites #6215 names: summary, next steps, Annex IV section 8, module docs."""
    report = ComplianceEngine().run(_descriptor(**ANNEX_III))
    rendered = repr(report)
    assert "Article 111(2))" not in rendered
    assert "August 2027 (" not in rendered
    assert "111(2)" not in (eu_ai_act.__doc__ or "")
    assert "August 2027" not in (eu_ai_act.__doc__ or "")


def test_structured_deadlines_carry_dates_references_and_conditional_rules() -> None:
    summary = ComplianceEngine().run(_descriptor(**ANNEX_III))["compliance_summary"]
    (entry,) = summary["deadlines"]
    assert entry == {
        "obligation": "high_risk_annex_iii",
        "label": AI_ACT_DEADLINES[AIActObligation.HIGH_RISK_ANNEX_III].label,
        "applies_from": "2027-12-02",
        "reference": "Article 113, third paragraph, point (c)(i)",
        "source": AI_ACT_SOURCE,
        "as_of": "2026-10-20",
        "transitional": [
            {
                "applies_to": r.applies_to,
                "rule": r.rule,
                "comply_by": None if r.comply_by is None else r.comply_by.isoformat(),
                "reference": r.reference,
            }
            for r in AI_ACT_DEADLINES[AIActObligation.HIGH_RISK_ANNEX_III].transitional
        ],
    }


def test_next_steps_and_annex_iv_section_8_use_the_category_date() -> None:
    annex_i = ComplianceEngine().run(_descriptor(**ANNEX_I))
    steps = annex_i["compliance_summary"]["next_steps"]
    assert any("2 August 2028" in s for s in steps)
    assert not any("2027" in s for s in steps)
    conformity_section = annex_i["tech_doc"]["sections"]["8_conformity_declaration"]
    assert "2 August 2028" in conformity_section
    assert "2027" not in conformity_section


def test_a_prohibited_system_is_not_given_a_high_risk_deadline() -> None:
    report = ComplianceEngine().run(_descriptor(social_scoring_public=True, **ANNEX_III))
    assert report["compliance_summary"]["deadline"] == "N/A"
    assert "2027" not in report["tech_doc"]["sections"]["8_conformity_declaration"]


def test_the_cli_prints_the_category_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bernstein compliance assess` renders what the engine computed, not a stored string."""
    monkeypatch.setattr(compliance_cmd, "bernstein_descriptor", lambda version="1.0.0": _descriptor(**ANNEX_I))
    result = CliRunner().invoke(compliance_cmd.compliance_group, ["assess", "--no-export"])

    assert result.exit_code == 0, result.output
    assert "2 August 2028" in result.output
    assert "Article 113, third paragraph, point (c)(ii)" in result.output
    assert "2027" not in result.output


def test_annex_i_only_conformity_procedure_and_article_49_scoping() -> None:
    """Annex I Section A products follow sectoral procedures and are exempt from EU AI Act database registration."""
    engine = ComplianceEngine()
    descriptor = _descriptor(**ANNEX_I)
    classification = engine.classify(descriptor)

    assert classification.risk_category is RiskCategory.HIGH
    assert not requires_article49_registration(classification)
    assert "Article 49" not in classification.justification
    assert "EU database registration" not in classification.justification

    report = engine.run(descriptor)
    s8 = report["tech_doc"]["sections"]["8_conformity_declaration"]
    assert "Annex I, Section A (Article 43(3))" in s8
    assert "sectoral conformity assessment procedure" in s8
    assert "Annex VI (Internal Control)" not in s8
    assert (
        "EU Database Registration: NOT APPLICABLE - Annex I systems are exempt from EU AI Act database registration (Article 49(5))"
        in s8
    )

    steps = report["compliance_summary"]["next_steps"]
    assert not any("Article 49" in s for s in steps)


def test_annex_iii_critical_infrastructure_article_49_exemption() -> None:
    """Annex III point 2 critical infrastructure systems are exempt under Article 49(1)."""
    engine = ComplianceEngine()
    descriptor = _descriptor(interacts_with_critical_infrastructure=True)
    classification = engine.classify(descriptor)

    assert classification.risk_category is RiskCategory.HIGH
    assert not requires_article49_registration(classification)
    assert "EU database registration" not in classification.justification

    report = engine.run(descriptor)
    s8 = report["tech_doc"]["sections"]["8_conformity_declaration"]
    assert "EU Database Registration: NOT APPLICABLE - Exempt from EU database registration under Article 49(1)" in s8

    steps = report["compliance_summary"]["next_steps"]
    assert not any("Article 49" in s for s in steps)


def test_annex_iii_non_critical_infrastructure_requires_article_49_registration() -> None:
    """Annex III systems outside point 2 require Article 49 registration."""
    engine = ComplianceEngine()
    descriptor = _descriptor(**ANNEX_III)
    classification = engine.classify(descriptor)

    assert classification.risk_category is RiskCategory.HIGH
    assert requires_article49_registration(classification)
    assert "EU database registration required before deployment" in classification.justification

    report = engine.run(descriptor)
    s8 = report["tech_doc"]["sections"]["8_conformity_declaration"]
    assert "Annex VI (Internal Control)" in s8
    assert "EU Database Registration: PENDING - required before deployment (Article 49)" in s8

    steps = report["compliance_summary"]["next_steps"]
    assert any("Register system in EU AI Act database (Article 49)" in s for s in steps)


def test_dual_annex_i_and_annex_iii_preserves_both_procedures_and_registration() -> None:
    """A system covered under both Annex I and Annex III reflects both bases."""
    engine = ComplianceEngine()
    descriptor = _descriptor(**ANNEX_I, **ANNEX_III)
    classification = engine.classify(descriptor)

    assert classification.risk_category is RiskCategory.HIGH
    assert requires_article49_registration(classification)

    report = engine.run(descriptor)
    s8 = report["tech_doc"]["sections"]["8_conformity_declaration"]
    assert "Annex I, Section A (Article 43(3))" in s8
    assert "Annex III basis: Annex VI (Internal Control)" in s8
    assert "EU Database Registration: PENDING - required before deployment (Article 49)" in s8

    steps = report["compliance_summary"]["next_steps"]
    assert any("Register system in EU AI Act database (Article 49)" in s for s in steps)


# ---------------------------------------------------------------------------
# One source of truth
# ---------------------------------------------------------------------------

#: A day-month-year or month-year in the range the AI Act's dates fall in.
_AI_ACT_DATE = re.compile(r"\b(?:\d{1,2}\s+)?(?:February|August|December)\s+20(?:2[5-9]|30)\b")
#: Words that make such a date an AI Act claim rather than, say, a changelog date.
_AI_ACT_CONTEXT = re.compile(
    r"AI Act|Article\s+(?:5|6|50|111|113)\b|Annex\s+I{1,3}\b|high-risk|transparency obligation|deadline",
    re.IGNORECASE,
)


def test_no_module_outside_the_table_states_an_ai_act_date() -> None:
    """A second copy of a date drifts: #6215 was four copies of one wrong string."""
    canonical = Path(eu_ai_act.__file__).resolve()
    offenders: list[str] = []
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        if path.resolve() == canonical:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _AI_ACT_DATE.search(line) and _AI_ACT_CONTEXT.search(line):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}")
    assert offenders == []


def test_inside_the_canonical_module_dates_live_only_in_the_table() -> None:
    source = Path(eu_ai_act.__file__).read_text(encoding="utf-8")
    # The markers are comment lines; the constants naming them sit elsewhere.
    begin = source.index("\n" + eu_ai_act._TABLE_BEGIN + "\n")  # pyright: ignore[reportPrivateUsage]
    end = source.index("\n" + eu_ai_act._TABLE_END + "\n")  # pyright: ignore[reportPrivateUsage]
    assert begin < end
    outside = source[:begin] + source[end:]
    assert not _AI_ACT_DATE.search(outside)
    assert not re.search(r"\bdate\(20\d\d,", outside)


def test_the_guard_would_catch_the_string_this_issue_removed() -> None:
    line = 'deadline = "August 2027 (Article 111(2))"'
    assert _AI_ACT_DATE.search(line) and _AI_ACT_CONTEXT.search(line)


# ---------------------------------------------------------------------------
# Docs state the verified dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    ["docs/operations/compliance.md", "docs/compliance/eu-ai-act-article-12-bundle.md"],
)
def test_compliance_docs_state_the_dates_with_an_as_of_line_and_the_oj_reference(doc: str) -> None:
    text = (_REPO_ROOT / doc).read_text(encoding="utf-8")
    assert "Regulation (EU) 2026/1744" in text
    assert "http://data.europa.eu/eli/reg/2026/1744/oj" in text
    assert "As of 20 October 2026" in text
    for expected in ("2 December 2027", "2 August 2028"):
        assert expected in text
    assert "August 2027 compliance deadline" not in text
    assert "August 2027 deadline" not in text
