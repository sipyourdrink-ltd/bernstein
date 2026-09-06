"""Unit tests for #3138: collapsing top-level command names that duplicate an existing group.

The moves are only safe if two things hold at once: the new spelling reaches the
same implementation, and the deprecated spelling keeps working with the flags
scripts already pass it. Asserting ``--help`` exits 0 proves neither, so the
tests below drive the commands against a real project directory and assert on
what comes back on each stream.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.evidence.run_artifacts import ArtifactPayload, post_run_artifact
from bernstein.core.security.audit import load_or_create_audit_key


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with ``.sdd/`` and an audit key that never leaves tmp_path."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _post(project: Path, *, task_id: str = "task-1", key: str = "summary", body: str = "hello") -> None:
    post_run_artifact(
        sdd_dir=project / ".sdd",
        task_id=task_id,
        key=key,
        payload=ArtifactPayload.report(body),
        actor="worker-a",
        hmac_key=load_or_create_audit_key(),
    )


# ---------------------------------------------------------------------------
# New spellings reach the implementation
# ---------------------------------------------------------------------------


def test_cost_estimate_subcommand_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["cost", "estimate", "--help"])
    assert result.exit_code == 0
    assert "Predict the cost of a task" in result.output


def test_cost_estimate_runs_under_the_cost_group() -> None:
    """The group callback must yield to the subcommand instead of running the report."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "cost", "estimate", "ship it", "--metrics-dir", "nonexistent"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["goal"] == "ship it"
    assert "estimated_cost_usd" in payload


def test_cost_envelopes_subcommand_registered_and_no_issue_tag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["cost", "envelopes", "--help"])
    assert result.exit_code == 0
    assert "(issue #1405)" not in result.output


def test_skills_provenance_and_verify_registered() -> None:
    runner = CliRunner()
    res1 = runner.invoke(cli, ["skills", "provenance", "--help"])
    assert res1.exit_code == 0
    assert "usage-provenance graph" in res1.output

    res2 = runner.invoke(cli, ["skills", "verify", "--help"])
    assert res2.exit_code == 0
    assert "install receipt" in res2.output


def test_skills_provenance_is_the_same_command_object_as_skill_provenance() -> None:
    """The move must re-register the implementation, not fork a second copy."""
    from bernstein.cli.commands.skill_cmd import skill_group
    from bernstein.cli.commands.skills_cmd import skills_group

    for name in ("provenance", "verify"):
        assert skills_group.commands[name] is skill_group.commands[name]


# ---------------------------------------------------------------------------
# artifacts -> artifact
# ---------------------------------------------------------------------------


def test_artifact_list_takes_an_optional_task_argument() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "--help"])
    assert result.exit_code == 0
    assert "[TASK]" in result.output


def test_artifact_list_with_task_lists_posted_artifacts(project: Path) -> None:
    """`artifact list <task>` must reach the agent-posted listing, not the spine listing."""
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "summary" in result.output


def test_artifact_list_without_task_returns_the_spine_document(project: Path) -> None:
    """The two paths answer different questions and must not return the same document."""
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "-w", str(project), "--output-json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Lineage-spine shape: production counts keyed by canonical artifact URI.
    assert sorted(payload) == ["artifacts"]
    assert all(sorted(row) == ["productions", "uri"] for row in payload["artifacts"])


def test_artifact_list_with_task_honours_output_json(project: Path) -> None:
    """`--output-json` must survive the delegation; a table here breaks `| jq`."""
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project), "--output-json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["task"] == "task-1"
    assert payload["verified"] is True
    assert payload["reason"] is None
    assert [a["key"] for a in payload["artifacts"]] == ["summary"]
    assert payload["artifacts"][0]["verified"] is True


def test_artifact_list_with_task_json_reports_the_empty_state(project: Path) -> None:
    """Exit code 1 keeps its meaning under --output-json, and stdout stays parseable."""
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "nope", "-w", str(project), "--output-json"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == {"artifacts": [], "reason": None, "task": "nope", "verified": True}


def test_artifact_list_with_task_json_marks_a_flipped_blob_unverified(project: Path) -> None:
    """The JSON path must carry the same tampered verdict the table column shows."""
    from bernstein.core.evidence.bundle import EvidenceStore
    from bernstein.core.evidence.run_artifacts import read_artifact_rows

    _post(project, body="secret-content")
    record = read_artifact_rows(project / ".sdd", "task-1")[0]
    blob_path = EvidenceStore(project / ".sdd" / "evidence").blob_path(record.content_hash)
    data = bytearray(blob_path.read_bytes())
    data[-2] ^= 0x01
    blob_path.write_bytes(bytes(data))

    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project), "--output-json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["verified"] is False
    assert payload["reason"]
    assert payload["artifacts"][0]["verified"] is False
    assert "secret-content" not in result.stdout


def test_artifact_list_with_task_json_reports_a_journal_that_hides_every_row(project: Path) -> None:
    """Tampering that removes every posted row is exit 2, not an empty clean listing."""
    _post(project)
    journals = sorted((project / ".sdd").rglob("*.jsonl"))
    journal = next(j for j in journals if "artifact_posted" in j.read_text(encoding="utf-8"))
    journal.write_text(journal.read_text(encoding="utf-8").replace("artifact_posted", "artifact_hidden"))

    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project), "--output-json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["artifacts"] == []
    assert payload["verified"] is False
    assert "Merkle" in payload["reason"]


def test_artifact_show_renders_a_posted_key(project: Path) -> None:
    _post(project, body="rendered-body")
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "show", "task-1", "summary", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "rendered-body" in result.output


def test_artifact_show_exits_1_for_an_unknown_key(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "show", "task-1", "nope", "-w", str(project)])
    assert result.exit_code == 1, result.output
    assert "No artifact" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["artifact", "list", "task-1"],
        ["artifact", "show", "task-1", "summary"],
        ["skills", "provenance", "task-1"],
        ["skills", "verify", "task-1"],
    ],
)
def test_canonical_spellings_never_print_a_deprecation_warning(project: Path, argv: list[str]) -> None:
    """The moved commands are shared objects; the warning belongs to the old group only.

    Attaching it to the command instead of its deprecated group would make the
    replacement nag about itself, which is the one outcome worse than the
    duplicate name this change removes.
    """
    _post(project)
    result = CliRunner().invoke(cli, [*argv, "-w", str(project)])
    assert "deprecated" not in result.stderr, result.stderr


def test_cost_subcommands_never_print_a_deprecation_warning(tmp_path: Path) -> None:
    """Same property for the two commands that moved under `cost`."""
    runner = CliRunner()
    envelopes = runner.invoke(
        cli,
        ["cost", "envelopes", "show", "--ledger", str(tmp_path / "l.jsonl"), "--config", str(tmp_path / "b.yaml")],
    )
    assert "deprecated" not in envelopes.stderr, envelopes.stderr
    estimate = runner.invoke(cli, ["cost", "estimate", "goal", "--metrics-dir", "nonexistent"])
    assert "deprecated" not in estimate.stderr, estimate.stderr


# ---------------------------------------------------------------------------
# limits pool
# ---------------------------------------------------------------------------


def test_limits_pool_subcommands_all_project_the_admission_ledger() -> None:
    """Every subcommand under `limits pool` must address the store `create` writes.

    `bernstein pool`'s subcommands project the HMAC audit chain into a sandbox-pool
    registry; `limits pool create` writes slot pools to the admission work ledger.
    Registering the first set under the second group yields a group in which
    `limits pool create staging-env --slots 1` is followed by `limits pool list`
    reporting no pools and `limits pool show staging-env` exiting 1.
    """
    from bernstein.cli.commands import limits_cmd
    from bernstein.cli.commands.limits_cmd import pool_group

    for name, command in pool_group.commands.items():
        callback = command.callback
        assert callback is not None, f"'limits pool {name}' has no callback"
        assert callback.__module__ == limits_cmd.__name__, (
            f"'limits pool {name}' is implemented in {callback.__module__}, which projects a "
            "different store than 'limits pool create'"
        )


def test_limits_pool_create_is_readable_back_through_limits_status(project: Path) -> None:
    """The admission-ledger round trip the group is supposed to own."""
    runner = CliRunner()
    created = runner.invoke(cli, ["limits", "pool", "create", "staging-env", "--slots", "1", "--workdir", str(project)])
    assert created.exit_code == 0, created.output
    listed = runner.invoke(cli, ["limits", "status", "--workdir", str(project)])
    assert listed.exit_code == 0, listed.output
    assert "staging-env" in listed.output


# ---------------------------------------------------------------------------
# Deprecated spellings still work, and say so on stderr
# ---------------------------------------------------------------------------


def test_deprecated_estimate_alias_warns_on_stderr_and_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "estimate", "test goal", "--metrics-dir", "nonexistent"])
    assert result.exit_code == 0, result.output
    assert "WARNING: 'bernstein estimate' is deprecated" in result.stderr
    # The warning must not corrupt the machine-readable stream.
    assert json.loads(result.stdout)["goal"] == "test goal"


def test_estimate_alias_shares_the_canonical_parameter_objects() -> None:
    """Matching parameter *names* would still allow the two to parse differently.

    A re-declared `--scope` with a different Choice set, default or requiredness
    reads as identical under a name comparison, so assert object identity: the
    alias holds the same Parameter instances `cost estimate` does.
    """
    from bernstein.cli.commands.cost import estimate_alias_cmd, estimate_cmd

    assert [param.name for param in estimate_alias_cmd.params] == [param.name for param in estimate_cmd.params]
    for alias_param, canonical_param in zip(estimate_alias_cmd.params, estimate_cmd.params, strict=True):
        assert alias_param is canonical_param, f"alias re-declares {canonical_param.name}"


def test_estimate_alias_rejects_exactly_what_cost_estimate_rejects() -> None:
    """The parse-time contract, asserted through the CLI rather than by inspection."""
    runner = CliRunner()
    alias = runner.invoke(cli, ["estimate", "goal", "--scope", "huge"])
    canonical = runner.invoke(cli, ["cost", "estimate", "goal", "--scope", "huge"])
    assert alias.exit_code == 2, alias.output
    assert canonical.exit_code == 2, canonical.output
    assert "'huge' is not one of" in alias.stderr
    assert "'huge' is not one of" in canonical.stderr


def test_deprecated_cost_envelopes_alias_still_dispatches_show(tmp_path: Path) -> None:
    """The alias is only ever used with a subcommand; a leaf alias rejects `show`."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "cost-envelopes",
            "show",
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--config",
            str(tmp_path / "bernstein.yaml"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "WARNING: 'bernstein cost-envelopes' is deprecated" in result.stderr
    assert json.loads(result.stdout)["envelopes"] == {}


def test_bare_cost_envelopes_alias_behaves_like_the_group_it_aliases() -> None:
    """A deliberate change: the bare alias used to exit 0 having done nothing.

    As a leaf command it accepted no arguments, warned, invoked a group callback
    that dispatches nothing, and returned success. It now reports a missing
    subcommand exactly as `bernstein cost envelopes` does.
    """
    runner = CliRunner()
    alias = runner.invoke(cli, ["cost-envelopes"])
    canonical = runner.invoke(cli, ["cost", "envelopes"])
    assert alias.exit_code == canonical.exit_code, (alias.output, canonical.output)
    assert alias.exit_code != 0


def test_cost_envelopes_alias_exposes_the_same_subcommands_as_the_group() -> None:
    from bernstein.cli.commands.cost import cost_envelopes_alias_cmd, cost_envelopes_group

    assert set(cost_envelopes_alias_cmd.commands) == set(cost_envelopes_group.commands)
    for name, command in cost_envelopes_group.commands.items():
        assert cost_envelopes_alias_cmd.commands[name] is command


def test_deprecated_artifacts_alias_warns_and_still_lists(project: Path) -> None:
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifacts", "list", "task-1", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "WARNING: 'bernstein artifacts' is deprecated" in result.stderr
    assert "summary" in result.stdout


def test_deprecated_skill_alias_warns_and_still_runs(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "provenance", "nope", "-w", str(project)])
    assert "WARNING: 'bernstein skill' is deprecated" in result.stderr
    assert result.exit_code in (0, 1), result.output


def test_deprecated_debug_bundle_alias_names_the_flags_that_do_not_carry_over() -> None:
    """`debug bundle` is a different builder, so the warning must not promise a rename."""
    from bernstein.cli.commands.debug_cmd import debug_cmd
    from bernstein.cli.debug_bundle import bundle_cmd

    legacy = {param.name for param in debug_cmd.params}
    replacement = {param.name for param in bundle_cmd.params}
    dropped = legacy - replacement
    assert dropped, "flag surfaces now match; the warning below should be simplified"

    runner = CliRunner()
    result = runner.invoke(cli, ["debug-bundle"], input="n\n")
    assert "WARNING: 'bernstein debug-bundle' is deprecated" in result.stderr
    for flag in sorted(dropped):
        assert flag.replace("_", "-") in result.stderr, f"warning does not mention --{flag}"


# ---------------------------------------------------------------------------
# No two commands share a body (#5102)
# ---------------------------------------------------------------------------
#
# The tests above pin the collapses that were already made. This one catches the
# NEXT accidental copy, which is how the collapsed cases got there: a command was
# reimplemented in a second module, both spellings kept working, and the two
# copies then drifted apart with nobody watching. Comparing AST bodies rather
# than text means a reformat, a renamed local or a different docstring cannot
# hide a duplicate -- and cannot invent one either.


def _command_name(decorator: ast.expr) -> str | None:
    """The name a ``@click.command("x")`` / ``@group.command("x")`` decorator registers."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and func.attr in {"command", "group"}):
        return None
    if not decorator.args:
        return None
    first = decorator.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


def _body_digest(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """A hash of the function body, with the docstring dropped. ``None`` for an empty body.

    The docstring is excluded on purpose: two copies whose help text was reworded
    are still two copies, and a group whose entire body IS its docstring (the
    common Click idiom) has no implementation to duplicate.
    """
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if not body:
        return None
    dumped = ast.dump(ast.Module(body=body, type_ignores=[]))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _commands_by_body() -> dict[tuple[str, str], list[str]]:
    """Every registered CLI command, grouped by ``(command name, body digest)``."""
    root = Path(__file__).resolve().parents[2] / "src" / "bernstein" / "cli"
    grouped: dict[tuple[str, str], list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            names = [n for n in (_command_name(d) for d in node.decorator_list) if n is not None]
            digest = _body_digest(node)
            if not names or digest is None:
                continue
            where = f"{path.relative_to(root)}:{node.lineno} ({node.name})"
            grouped.setdefault((names[0], digest), []).append(where)
    return grouped


# Exact inventory of Click groups named ``receipt`` (#5102). These are four
# different artefacts under one name -- not duplicates -- so the set is the
# documentation-of-known-state, and a fifth module declaring ``.group("receipt")``
# fails this test.
KNOWN_RECEIPT_GROUPS = frozenset(
    {
        "commands/receipt_cmd.py",  # bernstein receipt
        "commands/audit_cmd.py",  # bernstein audit receipt
        "commands/sandbox_cmd.py",  # bernstein sandbox receipt
        "commands/eval_benchmark_cmd.py",  # bernstein benchmark receipt
    }
)


def _receipt_group_modules() -> set[str]:
    """Modules under ``cli/`` that declare ``@….group("receipt")``."""
    root = Path(__file__).resolve().parents[2] / "src" / "bernstein" / "cli"
    found: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if _command_name(decorator) != "receipt":
                    continue
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "group"
                ):
                    found.add(path.relative_to(root).as_posix())
    return found


def test_known_receipt_groups_are_exactly_these_four() -> None:
    """Documents today's four ``receipt`` groups; fails if a fifth appears."""
    assert _receipt_group_modules() == KNOWN_RECEIPT_GROUPS


def test_no_two_commands_named_verify_share_a_body() -> None:
    """``verify`` is the name most copied, and the one an operator most needs to be single."""
    duplicates = {
        name: where for (name, _digest), where in _commands_by_body().items() if name == "verify" and len(where) > 1
    }
    assert duplicates == {}, (
        "two `verify` commands have the same implementation -- collapse them to one "
        f"and make the other a warn-and-delegate shim (#5102): {duplicates}"
    )


def test_no_two_commands_of_any_name_share_a_body() -> None:
    """The general form: a second copy of any command is a second place to fix a bug.

    `bernstein completions` was implemented twice with byte-identical bodies --
    once in ``commands/advanced_cmd.py`` and once in the dedicated
    ``commands/completions_cmd.py`` the operations docs name as the source. Only
    the first was registered, so an edit to the documented module would have had
    no runtime effect at all.
    """
    duplicates = {
        f"{name}:{digest[:8]}": where for (name, digest), where in _commands_by_body().items() if len(where) > 1
    }
    assert duplicates == {}, (
        "a CLI command is implemented more than once with the same body -- register one and "
        f"delete the copy, or make it delegate (#5102): {duplicates}"
    )


def test_completions_is_the_module_the_docs_name() -> None:
    """The registered `completions` must be the one ``docs/operations/shell-completions.md`` points at."""
    from bernstein.cli.commands.completions_cmd import completions_cmd

    registered = cli.commands["completions"]
    assert registered is completions_cmd
    assert completions_cmd.callback is not None
    assert completions_cmd.callback.__module__ == "bernstein.cli.commands.completions_cmd"
