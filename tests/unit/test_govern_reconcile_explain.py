"""``bernstein govern reconcile --explain <target>``: the effective policy, and where it came from.

Slice 3 of #5117. The composition model and the ordered-baseline hash already
exist in ``core/govern/policy_layers.py``; until this command nothing outside
the package imported them, which is the same "built and never wired" failure
#5105 catalogues for ``security_posture.py``.

What an operator needs from this is not "what applies" -- the flat clause list
answered that -- but WHICH LAYER wrote each clause, and whether the class
overlay could be chosen at all. Both are asserted here through the CLI, because
the composition being right in a library nothing calls is exactly the state this
slice exists to leave.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.govern_cmd import govern_reconcile_cmd

BASELINE_CLAUSE = "MFA required"

LAYERS = {
    "layers": [
        {
            "kind": "classification",
            "name": "facts",
            "clauses": [{"surface": "region", "clause": "runs in eu-west-1", "kind": "permitted"}],
        },
        {
            "kind": "baseline",
            "name": "common",
            "clauses": [
                {"surface": "s3", "clause": "no public buckets", "kind": "forbidden"},
                {"surface": "iam", "clause": BASELINE_CLAUSE, "kind": "required"},
            ],
        },
        {
            "kind": "instrumentation",
            "name": "audit-hooks",
            "clauses": [{"surface": "audit", "clause": "hooks enabled", "kind": "required"}],
        },
        {
            "kind": "class_overlay",
            "name": "prod",
            "applies_to": ["production"],
            "clauses": [{"surface": "iam", "clause": BASELINE_CLAUSE, "kind": "forbidden"}],
        },
        {
            "kind": "class_overlay",
            "name": "regulated",
            "applies_to": ["production", "pci"],
            "clauses": [{"surface": "s3", "clause": "no public buckets", "kind": "forbidden"}],
        },
    ]
}


@pytest.fixture
def policy_set(tmp_path: Path) -> Path:
    path = tmp_path / "layers.json"
    path.write_text(json.dumps(LAYERS), encoding="utf-8")
    return path


def _explain(policy_set: Path, target: str, *classes: str, json_out: bool = False):
    args = ["--explain", target, "--policy-set", str(policy_set)]
    for value in classes:
        args += ["--class", value]
    if json_out:
        args.append("--json")
    return CliRunner().invoke(govern_reconcile_cmd, args)


def test_every_row_names_the_layer_that_wrote_it(policy_set: Path) -> None:
    """The tier alone is not the answer when the baseline is a list of named sub-policies."""
    result = _explain(policy_set, "web-1", "pci")

    assert result.exit_code == 0, result.output
    assert "baseline:common" in result.stdout
    assert "instrumentation:audit-hooks" in result.stdout
    assert "class_overlay:regulated" in result.stdout
    assert "classification:facts" in result.stdout


def test_the_overlay_wins_over_the_baseline_for_the_same_clause(policy_set: Path) -> None:
    """Composition order is the point: a class overlay can tighten or relax the baseline."""
    # `pci` matches exactly one overlay; `production` matches two and is the
    # ambiguous case pinned separately below.
    result = _explain(policy_set, "web-1", "pci")
    payload = json.loads(_explain(policy_set, "web-1", "pci", json_out=True).stdout)
    s3 = next(c for c in payload["clauses"] if c["surface"] == "s3")

    assert result.exit_code == 0
    assert s3["layer"] == "class_overlay"
    assert s3["source"] == "regulated"
    assert s3["overridden"] == ["baseline:common"]


def test_two_matching_overlays_are_a_finding_not_a_default(policy_set: Path) -> None:
    """Whichever overlay declaration order reached last is not an answer."""
    result = _explain(policy_set, "web-2", "production")

    assert result.exit_code == 2
    assert "2 class overlays apply" in result.stderr
    assert "prod" in result.stderr
    assert "regulated" in result.stderr


def test_no_matching_overlay_is_also_a_finding(policy_set: Path) -> None:
    """Silently composing three layers and calling it complete is the same lie."""
    result = _explain(policy_set, "web-3")

    assert result.exit_code == 2
    assert "no class overlay applies" in result.stderr


def test_an_ambiguous_target_still_reports_the_layers_below(policy_set: Path) -> None:
    """Posture that is not in doubt should not be withheld because the class is."""
    result = _explain(policy_set, "web-2", "production")

    assert "baseline:common" in result.stdout
    assert "instrumentation:audit-hooks" in result.stdout
    assert "class_overlay" not in result.stdout


def test_two_runs_over_one_document_print_identically(policy_set: Path) -> None:
    """An explain a human diffs against last week's is worth nothing if it reorders."""
    first = _explain(policy_set, "web-1", "pci").stdout
    second = _explain(policy_set, "web-1", "pci").stdout

    assert first == second


def test_explain_records_nothing(policy_set: Path, tmp_path: Path) -> None:
    """It reads and composes; it must be safe to run against a production root."""
    lineage = tmp_path / ".sdd" / "lineage"

    _explain(policy_set, "web-1", "pci")

    assert not lineage.exists()


def test_explain_needs_its_own_document_not_the_desired_state(policy_set: Path) -> None:
    """The two modes want different inputs; neither should reject the other's call."""
    missing = CliRunner().invoke(govern_reconcile_cmd, ["--explain", "web-1"])

    assert missing.exit_code == 1
    assert "--policy-set" in missing.stderr


def test_a_propose_run_still_requires_its_desired_state() -> None:
    """Relaxing `--desired` at the click level must not make it optional in practice."""
    result = CliRunner().invoke(govern_reconcile_cmd, ["--propose"])

    assert result.exit_code == 1
    assert "--desired" in result.stderr


def test_an_unknown_layer_field_is_rejected_rather_than_dropped(tmp_path: Path) -> None:
    """A dropped key in a posture document is a rule that silently does not apply."""
    path = tmp_path / "layers.json"
    path.write_text(
        json.dumps({"layers": [{"kind": "baseline", "name": "common", "clauses": [], "applies_too": ["x"]}]}),
        encoding="utf-8",
    )

    result = _explain(path, "web-1")

    assert result.exit_code == 1
    assert "unreadable policy set" in result.stderr
    assert "applies_too" in result.stderr


def test_an_unknown_layer_kind_names_the_four(tmp_path: Path) -> None:
    path = tmp_path / "layers.json"
    path.write_text(json.dumps({"layers": [{"kind": "overlayy", "name": "x", "clauses": []}]}), encoding="utf-8")

    result = _explain(path, "web-1")

    assert result.exit_code == 1
    assert "class_overlay" in result.stderr
