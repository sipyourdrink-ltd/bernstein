"""Structural assertions on ``.github/workflows/auto-heal.yml`` (v2).

These tests parse the YAML and assert that the safety / observability
rails the operator depends on are wired exactly as documented in
``docs/operations/auto-heal.md``. They are intentionally cheap so they
can run in the unit suite, not the integration suite.

Reference capability list (the v2 wave). Each capability is asserted
by the presence of a YAML keyword, env var, or step name. Capabilities
that ship as deferred (no workflow surface yet) are marked
``CAPABILITY_DEFERRED`` and are NOT asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


WORKFLOW = Path(".github/workflows/auto-heal.yml")


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text: str) -> dict[str, object]:
    return yaml.safe_load(workflow_text)


def _heal_steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    heal = jobs.get("heal", {})
    assert isinstance(heal, dict)
    steps = heal.get("steps", [])
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _heal_step(workflow: dict[str, object], name: str) -> dict[str, object]:
    for step in _heal_steps(workflow):
        if step.get("name") == name:
            return step
    raise AssertionError(f"heal step missing: {name}")


def _step_run(workflow: dict[str, object], name: str) -> str:
    run = _heal_step(workflow, name).get("run", "")
    assert isinstance(run, str)
    return run


def test_workflow_file_exists() -> None:
    assert WORKFLOW.exists(), "v2 auto-heal workflow must live at .github/workflows/auto-heal.yml"


def test_workflow_name_is_v2(workflow: dict[str, object]) -> None:
    name = workflow.get("name", "")
    assert isinstance(name, str)
    assert "Auto-heal" in name or "auto-heal" in name


def test_workflow_call_trigger_with_inputs(workflow: dict[str, object]) -> None:
    """v2 is now invoked by post-ci-dispatcher.yml via workflow_call.

    The workflow_run fanout was consolidated into a single dispatcher so
    we no longer pay a per-child cold start. The dispatcher reads the
    upstream CI run metadata once and forwards it via workflow_call inputs.
    """
    # PyYAML parses ``on:`` as boolean True under YAML 1.1 -- so the key
    # arrives as ``True``. Tolerate both forms.
    on = workflow.get(True, workflow.get("on"))
    assert isinstance(on, dict)
    wfc = on.get("workflow_call")
    assert isinstance(wfc, dict), "auto-heal must declare workflow_call"
    assert "workflow_run" not in on, (
        "auto-heal must not listen to workflow_run directly; post-ci-dispatcher.yml owns that surface now"
    )
    inputs = wfc.get("inputs", {})
    assert isinstance(inputs, dict)
    for key in ("head_sha", "run_id", "display_title"):
        assert key in inputs, f"workflow_call input {key} missing"


def test_workflow_call_exposes_heal_outcome(workflow: dict[str, object]) -> None:
    """Dispatcher gates bernstein-ci-fix on this output.

    Acceptance criterion: auto-heal and bernstein-ci-fix call each other
    via dispatcher (instead of both firing in parallel). The serialisation
    relies on the dispatcher reading the heal outcome via this output.
    """
    on = workflow.get(True, workflow.get("on"))
    assert isinstance(on, dict)
    wfc = on.get("workflow_call")
    assert isinstance(wfc, dict)
    outputs = wfc.get("outputs", {})
    assert isinstance(outputs, dict)
    assert "heal_outcome" in outputs, "heal_outcome workflow_call output missing"


def test_workflow_level_permissions_are_empty(workflow: dict[str, object]) -> None:
    perms = workflow.get("permissions")
    assert perms == {} or perms == "{}"


def test_no_top_level_concurrency_under_workflow_call(workflow: dict[str, object]) -> None:
    """Top-level concurrency is owned by the dispatcher now.

    The dispatcher applies a per-SHA concurrency group covering all
    fanout children; per-child concurrency would cancel cousin runs and
    is left out so the dispatcher stays the single arbitrator.
    """
    assert "concurrency" not in workflow, (
        "auto-heal must not set its own concurrency; post-ci-dispatcher.yml owns the per-SHA concurrency group"
    )


def test_all_action_uses_pinned_to_sha(workflow_text: str) -> None:
    """No ``@vX`` or ``@branch`` form is allowed; everything is 40-char SHA."""
    uses_lines = [m.group(0) for m in re.finditer(r"uses:\s*[^\s#]+", workflow_text)]
    pattern = re.compile(r"uses:\s*[\w./-]+@[0-9a-f]{40}\b")
    for line in uses_lines:
        assert pattern.match(line), f"action not pinned to 40-char SHA: {line}"


def test_no_persist_credentials_true(workflow_text: str) -> None:
    """All ``actions/checkout`` invocations must set persist-credentials: false."""
    # Find every checkout block and ensure the next 10 lines mention false.
    matches = list(re.finditer(r"actions/checkout@", workflow_text))
    assert matches, "expected at least one actions/checkout"
    for m in matches:
        tail = workflow_text[m.start() : m.start() + 600]
        assert "persist-credentials: false" in tail, "every actions/checkout must set persist-credentials: false"


def test_no_em_dashes_in_workflow(workflow_text: str) -> None:
    """Constraint: no em-dashes anywhere."""
    assert "\u2014" not in workflow_text


def test_no_attribution_strings_in_workflow(workflow_text: str) -> None:
    """No AI-attribution leakage."""
    banned = ["Co-Authored-By: Claude", "Generated with Claude"]
    for token in banned:
        assert token not in workflow_text, f"banned attribution string: {token}"


def test_attestation_does_not_label_git_sha_as_sha256(workflow_text: str) -> None:
    """A git commit SHA is not a SHA-256 subject digest."""
    unsafe = re.compile(r"subject-digest:\s*[\"']?sha256:\$\{\{\s*needs\.triage\.outputs\.head_sha\s*}}")
    assert unsafe.search(workflow_text) is None


def test_self_tests_are_blocking_and_export_validation_result(workflow: dict[str, object]) -> None:
    """Self-test failures must stop the heal PR path."""
    self_test = _heal_step(workflow, "Diff-aware self-test (Capability 11)")
    assert self_test.get("id") == "self_test"

    run = _step_run(workflow, "Diff-aware self-test (Capability 11)")
    assert "|| true" not in run
    assert "validation_failed=true" in run
    assert "validation_failed=false" in run


def test_open_pr_requires_successful_validation(workflow: dict[str, object]) -> None:
    """The PR-opening step must be gated by every blocking validation result."""
    open_pr = _heal_step(workflow, "Open heal PR")
    if_clause = open_pr.get("if", "")
    assert isinstance(if_clause, str)

    assert "steps.self_test.outputs.validation_failed != 'true'" in if_clause
    assert "steps.ci_dispatch.outputs.validation_failed != 'true'" in if_clause


def test_ci_dispatch_failure_is_blocking(workflow: dict[str, object]) -> None:
    """CI dispatch failure must mark validation failed, not only warn."""
    dispatch = _heal_step(workflow, "Trigger CI on heal PR branch")
    assert dispatch.get("id") == "ci_dispatch"

    run = _step_run(workflow, "Trigger CI on heal PR branch")
    assert '|| echo "::warning::ci.yml dispatch failed' not in run
    assert "validation_failed=true" in run
    assert "validation_failed=false" in run


def test_push_does_not_embed_token_in_remote_url(workflow_text: str) -> None:
    """Do not construct a remote URL containing x-access-token credentials."""
    assert "x-access-token:${GH_TOKEN}@github.com" not in workflow_text
    assert "https://${AUTHKEY}" not in workflow_text


# ---------- Capability matrix coverage ----------------------------------------

CAPABILITY_ASSERTIONS: list[tuple[str, str]] = [
    # Detection layer
    ("c01_bayesian_confidence", "autoheal-bayes.json"),
    ("c02_flake_detector", "flake_detector"),
    ("c04_failure_clustering", "bucketize"),
    # Classification layer
    ("c05_llm_categorization_envvar", "BERNSTEIN_AUTOHEAL_BUDGET_USD"),
    # Repair layer
    ("c08_bandit_state", "autoheal-bandit.json"),
    # Safety layer
    ("c11_diff_aware_self_test", "ruff"),
    ("c12_permission_profile", "cordon"),
    ("c13_cost_breaker", "cost_guard"),
    ("c15_blast_radius_gate", "blast_radius"),
    # Provenance layer
    ("c16_lineage_v2", "autoheal-history.jsonl"),
    ("c17_decision_log", "decision_log"),
    # Operator-experience layer
    ("c21_audit_ledger", "autoheal-history.jsonl"),
    ("c23_kill_switch", "autoheal-disabled"),
    # Reliability layer
    ("c24_idempotency", "ci-heal/"),
]


@pytest.mark.parametrize(("name", "token"), CAPABILITY_ASSERTIONS)
def test_capability_wired_in_workflow(workflow_text: str, name: str, token: str) -> None:
    assert token in workflow_text, f"capability {name} missing token {token!r} in workflow"


# ---------- Regression: install path + tool scope --------------------------


def test_does_not_pip_install_typos_cli(workflow_text: str) -> None:
    """typos-cli does NOT exist on PyPI -- v2 originally died here.

    Regression for the f67487627 / 5e0be90 / 48f38a21 / 42bd1846 main-red
    incident series: the heal job's `Install runtime deps` step ran
    `python -m pip install ruff typos-cli`, which fails because
    typos-cli is not a published PyPI distribution (typos ships as a
    Rust binary via cargo or the crate-ci/typos GH action). The failure
    masked the entire heal pipeline -- no heal PR was ever created in
    the v2 era. The replacement uses `uv sync --group dev` for ruff and
    the crate-ci/typos action for the typos binary, gated to the
    heuristic strategy.
    """
    assert "pip install ruff typos-cli" not in workflow_text
    assert "pip install typos-cli" not in workflow_text


def test_uses_uv_for_heal_toolchain(workflow_text: str) -> None:
    """Heal job must use the same toolchain CI uses, or lint drift mismatches.

    The Lint job in ci.yml runs `uv run ruff format --check src/`. If
    the heal job used a different ruff version (e.g. a pip-installed
    floating release), the heal could either fail to reproduce the
    failing diff or introduce a new diff CI then rejects. Pinning to
    `uv sync --group dev` keeps both paths on the same ruff.
    """
    assert "uv sync --group dev" in workflow_text


def test_ruff_format_scope_matches_ci(workflow_text: str) -> None:
    """`ruff format` in the heal must mirror ci.yml's Lint job scope.

    ci.yml only checks `src/` for format drift, but the heal additionally
    formats `tests/` and `scripts/` because the cordon's
    WHITESPACE_OK_GLOBS allows those three roots. Running
    `ruff format .` (whole repo) would touch vendored paths the cordon
    rejects and the heal would always be cordon-blocked.
    """
    assert "uv run ruff format src/ tests/ scripts/" in workflow_text
    # And NOT the legacy whole-repo form
    assert "ruff format .\n" not in workflow_text


def test_agents_md_sync_strategy_chained_with_ruff(workflow_text: str) -> None:
    """agents-md-sync strategy must run ruff format after regen.

    When a feature merge adds Python modules, `bernstein agents-md sync`
    regenerates a module map that may itself trigger a ruff-format
    failure. The strategy must compose: sync FIRST, then ruff format on
    the result, so the heal PR lands in canonical formatted form.
    """
    # The agents-md case in the apply step contains both invocations.
    apply_block = re.search(
        r"agents-md-sync\).*?;;",
        workflow_text,
        flags=re.DOTALL,
    )
    assert apply_block is not None, "agents-md-sync case missing from apply step"
    body = apply_block.group(0)
    assert "bernstein agents-md sync" in body
    assert "ruff format" in body
