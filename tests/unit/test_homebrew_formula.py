"""The published Homebrew formula must install a working command (#3573).

`brew install chernistry/tap/bernstein` succeeded and produced a `bernstein`
that died on `import click`, because the formula called
``virtualenv_install_with_resources`` while declaring no ``resource`` blocks --
Homebrew drives that helper with ``pip --no-deps``, so nothing but bernstein
itself was installed.

The reason a broken formula could survive a fix is the second defect pinned
here: the publish workflow used to carry its own inline copy of the formula, so
``packaging/homebrew/bernstein.rb`` could be corrected without any effect on
what reached the tap. These tests keep the template honest and keep it the only
copy.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
FORMULA = _REPO / "packaging" / "homebrew" / "bernstein.rb"
PUBLISH_WORKFLOW = _REPO / ".github" / "workflows" / "publish-homebrew.yml"


def _code(body: str) -> str:
    """Return the formula source with comment lines dropped.

    The formula documents why it does not use ``virtualenv_install_with_resources``,
    so a naive substring search would match the explanation and fail the fix.
    """
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def test_formula_does_not_install_without_dependencies() -> None:
    """``virtualenv_install_with_resources`` with no resources installs nothing."""
    body = _code(FORMULA.read_text(encoding="utf-8"))

    if "virtualenv_install_with_resources" in body:
        resources = re.findall(r"^\s*resource\s+\"", body, re.MULTILINE)
        assert resources, (
            "virtualenv_install_with_resources runs pip with --no-deps, so a formula "
            "using it must declare the runtime closure as `resource` blocks; this one "
            "declares none, which is exactly the install that shipped a bernstein "
            "that could not import click"
        )


def test_formula_installs_the_runtime_closure() -> None:
    """Whatever the strategy, the install step has to resolve dependencies."""
    body = _code(FORMULA.read_text(encoding="utf-8"))

    install = body.partition("def install")[2].partition("end")[0]
    assert install.strip(), "the formula must define an install step"
    assert "--no-deps" not in install, "the install must not skip the runtime closure"

    declares_resources = bool(re.findall(r"^\s*resource\s+\"", body, re.MULTILINE))
    resolves_at_install = "pip" in install and "install" in install
    assert declares_resources or resolves_at_install, (
        "the formula must either pin the closure as resources or let pip resolve it"
    )


def test_formula_binary_is_linked_onto_the_path() -> None:
    body = FORMULA.read_text(encoding="utf-8")
    install = body.partition("def install")[2].partition("end")[0]

    if "virtualenv_install_with_resources" not in install:
        assert "bin.install_symlink" in install or "bin.install" in install, (
            "a hand-rolled virtualenv install has to link the entry point into bin/, "
            "otherwise `brew install` reports success and leaves no `bernstein` command"
        )


def test_publish_workflow_ships_the_checked_in_template() -> None:
    """One copy of the formula, or a fix to the template never reaches users."""
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "packaging/homebrew/bernstein.rb" in workflow, (
        "the publish workflow must read the checked-in formula template"
    )
    assert "class Bernstein < Formula" not in workflow, (
        "the publish workflow must not carry its own copy of the formula body; that "
        "copy is what shipped while the template was being corrected"
    )


def test_publish_workflow_substitutes_the_placeholders_the_template_uses() -> None:
    """The template's placeholders and the workflow's substitutions must agree."""
    body = FORMULA.read_text(encoding="utf-8")
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "bernstein-VERSION.tar.gz" in body, "template must carry the version placeholder"
    assert 'sha256 "PLACEHOLDER"' in body, "template must carry the digest placeholder"

    assert "bernstein-VERSION.tar.gz" in workflow, "workflow must substitute the version placeholder"
    assert "PLACEHOLDER" in workflow, "workflow must substitute the digest placeholder"


def test_publish_workflow_refuses_an_unresolved_digest() -> None:
    """An empty sha256 must fail the publish, not ship a formula nobody can install."""
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    guard = workflow.partition('if [ -z "$META_SHA256" ]')[2]
    assert guard, "the publish step must guard against an unresolved PyPI digest"
    assert "exit 1" in guard.partition("fi")[0], "an unresolved digest must fail the job"


def test_publish_workflow_hashes_the_sdist_and_not_an_error_body() -> None:
    """``curl -sL`` exits 0 on an HTTP error and prints the error page.

    Piping that into ``sha256sum`` yields a digest that is neither empty nor the
    empty-file digest, so it cleared every previous guard -- and the formula
    would ship a ``sha256`` that disagrees with its own ``url``, failing every
    ``brew install`` at checksum verification.
    """
    code = _code(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))

    assert "curl -sL" not in code, "the sdist fetch must not ignore HTTP status"
    assert "--fail" in code, "the sdist fetch must fail on an HTTP error instead of hashing the body"
    assert "gzip --test" in code, "the downloaded bytes must be confirmed to be a gzip stream before hashing"

    digest_lines = [line for line in code.splitlines() if "sha256sum" in line]
    assert digest_lines, "the workflow must compute a digest"
    assert all("/tmp/sdist.tar.gz" in line for line in digest_lines), (
        "the digest must be taken from the downloaded file, not from a pipe off curl"
    )


def test_publish_workflow_validates_the_version_before_building_a_url() -> None:
    """``required: true`` on a dispatch input only means "not omitted".

    The value is interpolated into the sdist URL and into the published formula,
    so ``latest`` or a stray space becomes a formula pinned to a release that
    does not exist.
    """
    code = _code(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))

    version_check = code.partition(_UNRECOGNISED_MESSAGE)[0]
    assert "grep -Eq" in version_check, "the version must be shape-checked before it reaches the URL"

    before_url = code.partition('URL="https://files.pythonhosted.org')[0]
    assert _UNRECOGNISED_MESSAGE in before_url, (
        "the version check must run before the URL that interpolates it is built"
    )


# --------------------------------------------------------------------------
# Which versions the workflow recognises, and which it publishes (#3626)
# --------------------------------------------------------------------------

_UNRECOGNISED_MESSAGE = "is not a version this project's release tooling can produce"
_BUMP_SCRIPT = _REPO / "scripts" / "bump_version.py"
_GREP_PATTERN = re.compile(r"grep -Eq '(\^\[0-9\][^']+)'")
_BUMP_SEMVER = re.compile(r"^_SEMVER_RE = re\.compile\(r\"(.+)\"\)$", re.MULTILINE)

# Versions scripts/bump_version.py accepts, so publish.yml can dispatch them.
_BUMPABLE = ("3.15.0", "3.15.0.dev1", "3.15.0.post1", "3.15.0-rc1", "3.15.0+local.1")
# Canonical PEP 440 pre-releases, which arrive from a hand-pushed tag.
_TAGGABLE = ("3.15.0a1", "3.15.0b2", "3.15.0rc1")
# Values workflow_dispatch's `required: true` still lets through.
_JUNK = ("latest", "v3.15.0", "3.15", "3.15.0 ", " 3.15.0", "", "main")


def _workflow_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The recognise-then-publish pair, in the order the workflow applies them."""
    found = _GREP_PATTERN.findall(_code(PUBLISH_WORKFLOW.read_text(encoding="utf-8")))
    assert len(found) == 2, f"expected a recognise and a publish pattern, found {found}"
    return re.compile(found[0]), re.compile(found[1])


def test_every_version_the_bump_script_produces_is_recognised() -> None:
    """A legitimate release must not fail this workflow, only skip it.

    publish.yml dispatches `${TAG#v}` for whatever tag was released, so a
    recognise-rule narrower than what scripts/bump_version.py accepts turns a
    deliberate pre-release into a red run.
    """
    recognise, _ = _workflow_patterns()
    bump = _BUMP_SEMVER.search(_BUMP_SCRIPT.read_text(encoding="utf-8"))
    assert bump is not None, "scripts/bump_version.py no longer declares _SEMVER_RE"
    bumpable = re.compile(bump.group(1))

    for version in _BUMPABLE:
        assert bumpable.match(version), f"corpus is stale: bump_version.py rejects {version!r}"
        assert recognise.match(version), f"{version!r} is bumpable but the workflow errors on it"
    for version in _TAGGABLE:
        assert recognise.match(version), f"{version!r} is a valid tag but the workflow errors on it"


def test_a_hand_typed_dispatch_value_is_still_rejected() -> None:
    recognise, _ = _workflow_patterns()

    for value in _JUNK:
        assert not recognise.match(value), f"{value!r} must not be treated as a version"


def test_only_final_releases_reach_the_tap() -> None:
    """A stable formula that tracks a dev or pre-release version is a broken tap."""
    recognise, publish = _workflow_patterns()

    assert publish.match("3.15.0")
    for version in (*_BUMPABLE[1:], *_TAGGABLE):
        assert recognise.match(version), f"{version!r} must be recognised"
        assert not publish.match(version), f"{version!r} must not be published to the tap"


def test_a_skipped_version_stops_the_publishing_steps() -> None:
    """`exit 0` ends the step, not the job -- the later steps need the guard.

    Without it they run with an empty ``steps.meta.outputs.version`` and push a
    formula pinned to a release that does not exist.
    """
    body = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    code = _code(body)

    assert 'echo "skip=true" >> "$GITHUB_OUTPUT"' in code, "the skip path must record itself as an output"
    assert code.count("steps.meta.outputs.skip != 'true'") == 2, (
        "both the formula generation and the tap push must be guarded on the skip output"
    )
