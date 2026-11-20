"""Generated commits must identify a GitHub account recognised by the quorum check."""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import tomllib
from collections.abc import Iterator
from itertools import groupby
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT_EMAIL_KEYS = {"user.email", "author.email", "committer.email"}
GIT_EMAIL_ENV = {"GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"}
EMAIL_SETTING = re.compile(r"(?:user|author|committer)\.email|GIT_(?:AUTHOR|COMMITTER)_EMAIL|--author\b", re.I)
CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--unset", "--unset-all", "get", "unset"}
ACTION_IDENTITY_INPUTS = {
    "author",
    "committer",
    "commit_author",
    "commit_user_email",
    "author_email",
    "committer_email",
}
# The numeric prefix identifies the GitHub account even after a login change.
BOT_IDS = {
    "github-actions[bot]": 41898282,
    "bernstein-the-conductor[bot]": 273363914,
    "dependabot[bot]": 49699333,
    "renovate[bot]": 29139614,
}


def _allowed_emails() -> set[str]:
    with (REPO_ROOT / ".github/quorum-roster.toml").open("rb") as roster:
        automation = set(tomllib.load(roster)["automation"])
    # GitHub App bot accounts use the numeric form. Older machine users can
    # have a login-only noreply address.
    addresses = {f"{bot}@users.noreply.github.com" for bot in automation if not bot.endswith("[bot]")}
    for bot in (automation | {"github-actions[bot]"}) & BOT_IDS.keys():
        addresses.add(f"{BOT_IDS[bot]}+{bot}@users.noreply.github.com")
    return addresses


def _author_email(identity: str) -> str:
    match = re.fullmatch(r"[^<>]*<([^<>]+)>", identity.strip())
    return match[1] if match else identity


def _argument_emails(args: list[str]) -> Iterator[str]:
    for i, arg in enumerate(args):
        key, equal, value = arg.partition("=")
        if equal and (key.lower() in GIT_EMAIL_KEYS or key in GIT_EMAIL_ENV):
            yield value
        elif key.lower() in GIT_EMAIL_KEYS and "config" in args[:i] and i + 1 < len(args):
            if CONFIG_READS.isdisjoint(args[:i]):
                yield args[i + 1]
        elif key == "--author" and "commit" in args[:i]:
            yield _author_email(value if equal else args[i + 1])


def _shell_emails(script: str) -> Iterator[str]:
    for line in re.sub(r"\\\r?\n\s*", " ", script).splitlines():
        if EMAIL_SETTING.search(line):
            # PowerShell exposes process environment variables through its env: drive.
            line = re.sub(r"\$env:(GIT_(?:AUTHOR|COMMITTER)_EMAIL)\s*=\s*", r"\1=", line, flags=re.I)
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            for separator, words in groupby(lexer, lambda word: word in {"&&", "||", ";", "|", "&"}):
                if not separator:
                    yield from _argument_emails(list(words))


def _python_emails(source: str) -> Iterator[str]:
    # Preserve argv boundaries across multiline subprocess calls. A computed
    # value in an identity setter cannot establish which account gets credit.
    def literal(node: ast.AST) -> str:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else "<computed>"

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)):
            yield from _argument_emails([literal(arg) for arg in node.elts])
        elif isinstance(node, ast.Call):
            yield from _argument_emails([literal(arg) for arg in node.args])
            for kw in node.keywords:
                if kw.arg in GIT_EMAIL_ENV:
                    yield literal(kw.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key and literal(key) in GIT_EMAIL_ENV:
                    yield literal(value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and literal(target.slice) in GIT_EMAIL_ENV:
                    yield literal(node.value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "git " in node.value:
            yield from _shell_emails(node.value)


def _workflow_emails(node: object) -> Iterator[str]:
    if isinstance(node, list):
        for item in node:
            yield from _workflow_emails(item)
    elif isinstance(node, dict):
        env = node.get("env", {})
        if isinstance(env, dict):
            for key in GIT_EMAIL_ENV.intersection(env):
                yield str(env[key])
        if isinstance(run := node.get("run"), str):
            yield from _shell_emails(run)
        inputs = node.get("with", {})
        if isinstance(inputs, dict):
            for key in ACTION_IDENTITY_INPUTS.intersection(inputs):
                yield _author_email(str(inputs[key]))
        for child in node.values():
            yield from _workflow_emails(child)


@pytest.mark.parametrize(
    "command",
    [
        'git config --global "user.email" "other@example.org"',
        'git config --get user.email && git -c user.email="other@example.org" commit -m update',
        'git commit --author="Another User <other@example.org>" -m update',
        'export GIT_AUTHOR_EMAIL="other@example.org"',
        '$env:GIT_COMMITTER_EMAIL = "other@example.org"',
    ],
)
def test_shell_commit_email_setters(command: str) -> None:
    assert list(_shell_emails(command)) == ["other@example.org"]


def test_multiline_python_git_config() -> None:
    source = 'run(["git", "config",\n    "user.email",\n    "other@example.org"])'
    assert list(_python_emails(source)) == ["other@example.org"]


@pytest.mark.parametrize("workflow", ["adapter-conformance-canary.yml", "nightly-canary.yml"])
def test_canary_workflows_configure_actions_bot(workflow: str) -> None:
    path = REPO_ROOT / ".github/workflows" / workflow
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["canary"]["steps"]
    settings = [
        shlex.split(line)[-2:]
        for step in steps
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("git config ")
    ]
    assert ["user.name", "github-actions[bot]"] in settings
    assert ["user.email", "41898282+github-actions[bot]@users.noreply.github.com"] in settings


@pytest.mark.whole_tree_guard
def test_generated_commit_emails_belong_to_project_automation() -> None:
    paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            ".github/workflows",
            "action.y*ml",
            "*/action.y*ml",
            "scripts",
            "*/scripts/*",
            "tools",
            "hooks",
            "*.sh",
            "*.bash",
            "*.zsh",
            "*.ps1",
            "*.py",
            ":(exclude)src",
            ":(exclude)tests",
            ":(exclude,glob)**/tests/**",
        ],
        cwd=REPO_ROOT,
        text=True,
    )
    allowed = _allowed_emails()
    errors = []
    for relative in sorted(filter(None, paths.split("\0"))):
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        if path.suffix in {".yml", ".yaml"}:
            emails = _workflow_emails(yaml.safe_load(source))
        elif not EMAIL_SETTING.search(source):
            continue
        elif path.suffix == ".py":
            emails = _python_emails(source)
        elif path.suffix in {"", ".sh", ".bash", ".zsh", ".ps1"}:
            emails = _shell_emails(source)
        else:
            continue
        errors.extend(f"{relative}: {email}" for email in emails if email not in allowed)
    assert not errors, "Unrecognised generated commit emails:\n" + "\n".join(errors)
