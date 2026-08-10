"""Structural assertions for the secret-scanning gate.

The gate is only worth having if a red run means something. Three settings
decide that, and each is the kind of thing that gets widened under time
pressure rather than by decision, so they are pinned here:

* it reports verified results only - the unverified stream is dominated by
  test-fixture connection strings, and gating on it turned main red without
  a leak;
* it excludes exactly one detector. ``lob`` is excluded because its verifier
  returns a positive for any ``test_``-prefixed string of exactly 35 further
  characters, so it fires on ordinary Python test names and on test
  filenames quoted in prose. A detector that cannot separate a credential
  from a docstring contributes nothing, but the argument stops there - it
  does not extend to detectors for services this project actually uses;
* it names the scanner version it runs. The ``uses:`` SHA pins the action,
  which is a shell wrapper; the wrapper then runs
  ``ghcr.io/trufflesecurity/trufflehog:${version}``, and that input defaults
  to ``latest``. Left at the default, a detector added upstream reaches this
  repository on its own schedule and lands on whichever pull request is open
  at the time.

The second point is why this file exists. ``--exclude-detectors`` takes a
comma-separated list, so silencing a genuine finding is a one-word edit that
reads exactly like the justified exclusion already present. Adding a name
here has to be a deliberate act with a reason attached, not a way to get a
build green.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "trufflehog.yml"

#: The only detector this project may silence, and the reason it may.
#:
#: ``lob`` verifies any ``test_`` + 35 characters as a live key, a shape
#: ordinary test names hit. There is no Lob integration here, so nothing is
#: lost. Extending this set means a real detector is being turned off.
ALLOWED_EXCLUDED_DETECTORS = frozenset({"lob"})

#: An exact scanner release, e.g. ``3.96.0``. The image is tagged without a
#: leading ``v``; ``latest`` and floating prefixes are what this rejects.
EXACT_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")

#: The only action this gate may run.
#:
#: The step is *discovered* by looking for ``trufflehog`` anywhere in a
#: ``uses:`` value, which is deliberately generous so that a swapped action
#: is still found rather than quietly skipped. Identity is then checked
#: exactly: every other assertion in this file reads the selected step's
#: inputs, so a fork or a similarly named action would satisfy all of them
#: while executing somebody else's code.
SCANNER_ACTION = "trufflesecurity/trufflehog"

#: That action pinned to a full lowercase commit SHA, as Renovate writes it.
ACTION_PIN = re.compile(rf"^{re.escape(SCANNER_ACTION)}@[0-9a-f]{{40}}\Z")


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _scan_step() -> dict[str, Any]:
    jobs = _doc().get("jobs")
    assert isinstance(jobs, dict), "workflow must declare jobs"
    steps = [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict) and "trufflehog" in str(step.get("uses", ""))
    ]
    assert len(steps) == 1, f"expected exactly one trufflehog step, found {len(steps)}"
    return steps[0]


def _extra_args() -> str:
    return str(_scan_step().get("with", {}).get("extra_args", ""))


def _scanner_version() -> str:
    """The ``version`` input, i.e. the image tag the wrapper actually runs."""
    return str(_scan_step().get("with", {}).get("version", "")).strip()


def _release_comment(text: str, uses: str) -> str:
    """The release Renovate records beside this exact ``uses:`` pin.

    A SHA is opaque, so the trailing comment is the only readable statement
    of which release runs - which makes it worth reading carefully. The
    pattern is anchored to the pin passed in and to the start of its line,
    so a commented-out predecessor or a second copy of the action cannot
    answer on behalf of the step that actually runs. Two matches are an
    error rather than a coin toss: it means the file disagrees with itself.
    """
    pattern = re.compile(
        rf"^[ \t]*uses:[ \t]*{re.escape(uses)}[ \t]*#[ \t]*v?(?P<version>\d+\.\d+\.\d+)[ \t]*$",
        re.MULTILINE,
    )
    found = pattern.findall(text)
    assert len(found) == 1, (
        f"expected exactly one `uses: {uses}` line carrying a release comment, "
        f"found {len(found)}. Renovate writes the release beside the SHA it "
        "pins; without it there is no readable record of which scanner runs"
    )
    return str(found[0])


def _action_uses() -> str:
    """The selected step's ``uses:``, checked to be the upstream scanner."""
    uses = str(_scan_step().get("uses", ""))
    assert ACTION_PIN.match(uses), (
        f"`uses: {uses}` must be `{SCANNER_ACTION}@` followed by a full "
        "40-character lowercase commit SHA. The step is found by matching "
        "`trufflehog` anywhere in the value, so a fork or a similarly named "
        "action would pass every other assertion in this file while running "
        "different code; a tag or branch is rewritable and pins nothing"
    )
    return uses


def _action_version() -> str:
    """The wrapper release, read from the step the workflow actually runs."""
    return _release_comment(WORKFLOW.read_text(encoding="utf-8"), _action_uses())


def _excluded_detectors() -> frozenset[str]:
    for arg in _extra_args().split():
        if arg.startswith("--exclude-detectors="):
            _, _, value = arg.partition("=")
            return frozenset(part.strip().lower() for part in value.split(",") if part.strip())
    return frozenset()


def test_gate_reports_verified_results_only() -> None:
    """Unverified results are noise here; gating on them is a red main."""
    assert "--results=verified" in _extra_args(), "the gate must report verified results only, or it fails on fixtures"


def test_no_detector_is_excluded_without_a_recorded_reason() -> None:
    """Silencing a detector is a one-word edit - it should not be a quiet one."""
    excluded = _excluded_detectors()
    unexpected = excluded - ALLOWED_EXCLUDED_DETECTORS
    assert not unexpected, (
        f"{sorted(unexpected)} excluded from secret scanning without a reason "
        "recorded here. If the detector genuinely cannot distinguish a "
        "credential from this repository's own source, say so in "
        "ALLOWED_EXCLUDED_DETECTORS and in the workflow comment. If it is "
        "excluded to make a build green, fix the finding instead."
    )


def test_the_known_false_positive_detector_stays_excluded() -> None:
    """Removing it turns any 35-character test name into a red main."""
    assert "lob" in _excluded_detectors(), (
        "`lob` verifies any `test_` + 35 characters as a live key; without "
        "this exclusion a test name of that length fails the scan"
    )


def test_the_scanner_binary_is_pinned_to_an_exact_release() -> None:
    """The ``uses:`` SHA pins the wrapper, not the scanner it downloads."""
    version = _scanner_version()
    assert version, (
        "the trufflehog step must set the `version` input. The `uses:` SHA "
        "pins the action, but the action is a wrapper that runs "
        "`ghcr.io/trufflesecurity/trufflehog:${version}`, and that input "
        "defaults to `latest` - so a detector added upstream reaches this "
        "repository with no change on this side, on whichever pull request "
        "happens to be open"
    )
    assert EXACT_RELEASE.match(version), (
        f"`version: {version}` does not name one release. The image tag must "
        "be an exact `MAJOR.MINOR.PATCH` (no `v` prefix, and not `latest`), "
        "or the scan is not reproducible from the workflow alone"
    )


def test_the_scanner_binary_and_the_action_wrapper_are_the_same_release() -> None:
    """Renovate groups the two bumps; a mismatch means one landed alone."""
    scanner, action = _scanner_version(), _action_version()
    assert scanner == action, (
        f"the workflow runs scanner {scanner} through wrapper {action}. "
        "These ship from one repository and `renovate.json` groups them into "
        "a single pull request, so a mismatch means half a bump landed - "
        "reconcile them rather than pinning the test to the drift"
    )


def _workflow_text(*pin_lines: str) -> str:
    """A workflow fragment carrying the given ``uses:`` lines verbatim."""
    body = "\n".join(f"      - name: Run trufflehog\n        {line}" for line in pin_lines)
    return f"jobs:\n  trufflehog:\n    steps:\n{body}\n"


@pytest.mark.parametrize(
    "uses",
    [
        pytest.param(f"attacker/trufflehog-wrapper@{'a' * 40}", id="unrelated-owner"),
        pytest.param(f"trufflesecurity/trufflehog-fork@{'a' * 40}", id="suffixed-repo"),
        pytest.param(f"nottrufflesecurity/trufflehog@{'a' * 40}", id="prefixed-owner"),
        pytest.param(f"trufflesecurity/trufflehog@{'A' * 40}", id="uppercase-sha"),
        pytest.param("trufflesecurity/trufflehog@v3", id="tag-not-sha"),
        pytest.param(f"trufflesecurity/trufflehog@{'a' * 39}", id="truncated-sha"),
    ],
)
def test_an_action_that_merely_resembles_the_scanner_is_refused(uses: str) -> None:
    """Discovery matches a substring; identity must not be so generous."""
    assert not ACTION_PIN.match(uses), (
        f"`{uses}` would be accepted as the scanner. Every other assertion "
        "in this file reads the selected step's inputs, so a look-alike "
        "passes them all while running different code"
    )


def test_the_workflow_runs_the_upstream_scanner_action() -> None:
    """The positive half: the step really is the action it is taken to be."""
    assert _action_uses().startswith(f"{SCANNER_ACTION}@")


def test_a_commented_out_pin_cannot_answer_for_the_live_one() -> None:
    """The release must come from the pin that runs, not the first in the file."""
    text = _workflow_text(
        f"# uses: trufflesecurity/trufflehog@{'a' * 40}  # v3.90.0",
        f"uses: trufflesecurity/trufflehog@{'b' * 40}  # v3.96.0",
    )
    assert _release_comment(text, f"trufflesecurity/trufflehog@{'b' * 40}") == "3.96.0"


def test_a_pin_with_no_release_comment_beside_it_is_rejected() -> None:
    """Silence is not agreement - an absent comment must not read as a match."""
    uses = f"trufflesecurity/trufflehog@{'b' * 40}"
    with pytest.raises(AssertionError):
        _release_comment(_workflow_text(f"uses: {uses}"), uses)


def test_two_pins_of_one_action_are_rejected_rather_than_silently_halved() -> None:
    """Picking either of two answers hides that the file disagrees with itself."""
    uses = f"trufflesecurity/trufflehog@{'b' * 40}"
    text = _workflow_text(f"uses: {uses}  # v3.96.0", f"uses: {uses}  # v3.90.0")
    with pytest.raises(AssertionError):
        _release_comment(text, uses)


def test_the_scan_still_runs_on_main_and_pull_requests() -> None:
    """An exclusion is only defensible while the gate itself still runs."""
    doc = _doc()
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    assert "pull_request" in triggers, "the gate must run before a merge"
    assert "push" in triggers, "the gate must run on what actually landed"
