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
