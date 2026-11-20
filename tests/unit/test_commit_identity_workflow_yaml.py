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
PYTHON = re.compile(r"""(?:^|[\s/(])python[\d.]*['"]?(?=\s|$)""")
HEREDOC = re.compile(r"""(?<!<)<<(?!<)(?P<tabs>-?)[ \t]*(?P<q>['"]?)(?P<end>\w+)(?P=q)""")
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


def _python_reads_program_from_stdin(arguments: str) -> bool:
    """Python's script selector determines whether the heredoc contains code or input data."""
    words = iter(shlex.split(arguments, comments=True))
    for word in words:
        if word == "-":
            return True  # Remaining words are arguments to the program on stdin.
        if word == "--":
            return next(words, "-") == "-"
        if not word.startswith("-") or word.startswith(("-c", "-m")):
            return False
        if word in {"-W", "-X", "--check-hash-based-pycs"}:
            next(words, None)
    return True


def _shell_emails(script: str) -> Iterator[str]:
    if not EMAIL_SETTING.search(script):
        return
    lines = iter(re.sub(r"\\\r?\n[ \t]*", " ", script).splitlines())
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        heredoc = HEREDOC.search(line)
        if heredoc and (interpreters := list(PYTHON.finditer(line[: heredoc.start()]))):
            body = []
            for raw in lines:
                source_line = raw.lstrip("\t") if heredoc["tabs"] else raw
                if source_line == heredoc["end"]:
                    break
                body.append(source_line)
            else:
                raise AssertionError(f"Unterminated Python heredoc: {line}")
            if _python_reads_program_from_stdin(line[interpreters[-1].end() : heredoc.start()]):
                yield from _python_emails("\n".join(body))
        if EMAIL_SETTING.search(line):
            # PowerShell exposes process environment variables through its env: drive.
            line = re.sub(r"\$env:(GIT_(?:AUTHOR|COMMITTER)_EMAIL)\s*=\s*", r"\1=", line, flags=re.I)
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            for separator, words in groupby(lexer, lambda word: word in {"&&", "||", ";", "|", "&"}):
                if not separator:
                    args = list(words)
                    yield from _argument_emails(args)
                    if "-c" in args:
                        i = args.index("-c")
                        if i + 1 < len(args) and any(PYTHON.search(arg) for arg in args[:i]):
                            yield from _python_emails(args[i + 1])


def _python_emails(source: str) -> Iterator[str]:
    # Preserve argv boundaries across multiline subprocess calls. A computed
    # value in an identity setter cannot establish which account gets credit.
    def literal(node: ast.AST) -> str:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else "<computed>"

    if not EMAIL_SETTING.search(source):
        return
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


def _other_script_emails(source: str) -> Iterator[str]:
    """Recognise literal argument vectors and environment assignments in other scripts."""
    # Covers, for example, execFileSync("git", ["config", "user.email", "..."])
    # and process.env.GIT_AUTHOR_EMAIL = "...".
    for marker in EMAIL_SETTING.finditer(source):
        tail = source[marker.end() :]
        if marker[0] == "--author":
            if "commit" not in source[: marker.start()].rsplit(";", 1)[-1]:
                continue
            author = re.match(r"""['"]?(?:\s*,\s*|=|\s+)['"]?[^<>\n]*<([^<>\n]+)>""", tail)
            yield author[1] if author else "<unresolved --author>"
        else:
            separator = (
                r"""['"`]?\]?\s*[:=]\s*""" if marker[0].upper() in GIT_EMAIL_ENV else r"""['"`]?(?:\s*[,=]\s*|[ \t]+)"""
            )
            if value := re.match(separator + r"""['"`]?([^\s,'"`;)>]+)""", tail):
                yield value[1]


def _workflow_emails(node: object, shell: str = "bash") -> Iterator[str]:
    if isinstance(node, list):
        for item in node:
            yield from _workflow_emails(item, shell)
    elif isinstance(node, dict):
        defaults = node.get("defaults")
        if isinstance(defaults, dict) and isinstance(run_defaults := defaults.get("run"), dict):
            shell = run_defaults.get("shell", shell)
        env = node.get("env", {})
        if isinstance(env, dict):
            for key in GIT_EMAIL_ENV.intersection(env):
                yield str(env[key])
        if isinstance(run := node.get("run"), str):
            yield from (_python_emails(run) if PYTHON.search(node.get("shell", shell)) else _shell_emails(run))
        inputs = node.get("with", {})
        if isinstance(inputs, dict):
            for key in ACTION_IDENTITY_INPUTS.intersection(inputs):
                yield _author_email(str(inputs[key]))
            if isinstance(inline := inputs.get("script"), str):
                yield from _other_script_emails(inline)
        for child in node.values():
            yield from _workflow_emails(child, shell)


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


@pytest.mark.parametrize("email", ["41898282+github-actions[bot]@users.noreply.github.com", "other@example.org"])
def test_action_script_git_arguments(email: str) -> None:
    script = f"await exec.exec('git', ['config', 'user.email', '{email}']);"
    assert list(_workflow_emails({"with": {"script": script}})) == [email]


@pytest.mark.parametrize("email", ["41898282+github-actions[bot]@users.noreply.github.com", "other@example.org"])
@pytest.mark.parametrize("form", ["python", "job-default", "heredoc", "python-c"])
def test_inline_workflow_python_checks_commit_email(form: str, email: str) -> None:
    source = f'run(["git", "config", "user.email", "{email}"])'
    step = {
        "python": {"shell": "python", "run": source},
        "job-default": {"defaults": {"run": {"shell": "python"}}, "steps": [{"run": source}]},
        "heredoc": {"run": f"uv run python - <<'PY'\n{source}\nPY\n"},
        "python-c": {"run": f"python3 -c '{source}'"},
    }[form]
    assert list(_workflow_emails(step)) == [email]
    assert (email in _allowed_emails()) == (email != "other@example.org")


@pytest.mark.parametrize("email", ["41898282+github-actions[bot]@users.noreply.github.com", "other@example.org"])
@pytest.mark.parametrize(
    "opener",
    [
        """python3 - "$WORK/seg1.cast" "$WORK/seg2.cast" "$WORK/demo.cast" <<'PY'""",
        """value="$(python3 - "$WORK/demo.cast" <<'PY'""",
        '"${VENV_BIN}/python" - "${SMOKE_VERSION}" <<"PY"',
    ],
)
def test_python_stdin_program_with_arguments(opener: str, email: str) -> None:
    body = f'env = {{"GIT_AUTHOR_EMAIL": "{email}", "GIT_COMMITTER_EMAIL": "{email}"}}'
    script = f"{opener}\n{body}\nPY\n"
    if "$(" in opener:
        script += ')"'
    assert list(_shell_emails(script)) == [email, email]
    assert (email in _allowed_emails()) == (email != "other@example.org")


def test_named_python_program_receives_heredoc_as_data() -> None:
    script = """python3 render.py - <<'DATA'\ngit config user.email other@example.org\nDATA"""
    assert list(_shell_emails(script)) == []


@pytest.mark.parametrize(
    ("workflow", "script"),
    [
        ("adapter-conformance-canary.yml", "scripts/canary_propose_branch.py"),
        ("nightly-canary.yml", "scripts/canary_real_run.py"),
    ],
)
def test_canary_workflows_configure_actions_bot(workflow: str, script: str) -> None:
    path = REPO_ROOT / ".github/workflows" / workflow
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["canary"]["steps"]
    (commit_step,) = [step for step in steps if script in step.get("run", "")]
    for role in ("AUTHOR", "COMMITTER"):
        assert commit_step["env"].get(f"GIT_{role}_NAME") == "github-actions[bot]"
        assert commit_step["env"].get(f"GIT_{role}_EMAIL") == "41898282+github-actions[bot]@users.noreply.github.com"


@pytest.mark.whole_tree_guard
def test_generated_commit_emails_belong_to_project_automation() -> None:
    paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            ".github/workflows",
            ".github/actions",
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
        if path.suffix in {".md", ".json", ".toml"}:
            continue
        payload = path.read_bytes()
        if b"\0" in payload:
            continue
        source = payload.decode("utf-8")
        if path.suffix in {".yml", ".yaml"}:
            emails = _workflow_emails(yaml.safe_load(source))
        elif not EMAIL_SETTING.search(source):
            continue
        elif path.suffix == ".py":
            emails = _python_emails(source)
        elif path.suffix in {"", ".sh", ".bash", ".zsh", ".ps1"}:
            emails = _shell_emails(source)
        else:
            emails = _other_script_emails(source)
        errors.extend(f"{relative}: {email}" for email in emails if email not in allowed)
    assert not errors, "Unrecognised generated commit emails:\n" + "\n".join(errors)
