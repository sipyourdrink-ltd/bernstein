"""Unit tests for ``scripts/regen_contract_drift.py``.

Focused on the idempotency self-check (feat:META F): a second regen pass
against the just-written tree must not produce further changes. If it does,
the script must exit with the dedicated self-check failure code so the bot
push job aborts rather than landing churn.

The fixture-regen helpers themselves are exercised indirectly via integration
runs; these tests stub them out so the self-check path can be tested in
isolation without touching real source files.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "regen_contract_drift.py"


@pytest.fixture
def regen_module():
    """Load scripts/regen_contract_drift.py as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("regen_contract_drift_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _make_stub_fixture(sequence: list[bool]) -> Callable[[], bool]:
    """Build a stub regen function that returns successive items from ``sequence``.

    Each call pops the head; once exhausted, returns ``False`` (idempotent).
    Mirrors the real fixture contract of ``True == wrote changes``.
    """

    def _stub() -> bool:
        return sequence.pop(0) if sequence else False

    return _stub


def test_self_check_passes_when_regen_is_idempotent(regen_module, monkeypatch):
    """Happy path: first call writes, second call is a no-op. Exit code 0."""
    # First call returns True (wrote changes); subsequent calls return False
    # (idempotent). This matches how a correct fixture-regen behaves.
    stub = _make_stub_fixture([True])
    monkeypatch.setattr(regen_module, "FIXTURES", {"DOCUMENTED_COMMANDS": stub}, raising=True)
    # Force the working-tree probe to report "clean" both before and after so
    # the test does not depend on the host repo state. We patch the helper
    # rather than spawning git.
    monkeypatch.setattr(regen_module, "_git_diff_is_clean", lambda: True)

    exit_code = regen_module.main(["--fixture", "DOCUMENTED_COMMANDS"])

    assert exit_code == 0, "idempotent regen should exit 0 when first pass wrote"


def test_self_check_aborts_when_fixture_keeps_writing(regen_module, monkeypatch, capsys):
    """Negative: fixture reports True twice in a row -> abort with code 2."""
    # Stub keeps returning True forever - i.e. the regen is non-idempotent and
    # would keep editing the file on every pass. The self-check must catch this.
    stub = _make_stub_fixture([True, True, True])
    monkeypatch.setattr(regen_module, "FIXTURES", {"DOCUMENTED_COMMANDS": stub}, raising=True)
    monkeypatch.setattr(regen_module, "_git_diff_is_clean", lambda: True)

    exit_code = regen_module.main(["--fixture", "DOCUMENTED_COMMANDS"])

    assert exit_code == regen_module.SELF_CHECK_FAIL_EXIT_CODE
    err = capsys.readouterr().err
    assert "non-idempotent" in err.lower()


def test_self_check_aborts_when_git_diff_dirties_on_second_pass(regen_module, monkeypatch, capsys):
    """Negative: fixture lies about idempotency but git diff catches it.

    Simulates the case where a fixture returns ``False`` on the second pass
    (claiming nothing to do) yet still mutates files. The git-diff probe
    detects the resulting dirty tree and trips the self-check.
    """
    # Fixture reports True once, then False - but the working-tree check flips
    # from clean to dirty between the two passes, mimicking a stealth write.
    stub = _make_stub_fixture([True, False])
    monkeypatch.setattr(regen_module, "FIXTURES", {"DOCUMENTED_COMMANDS": stub}, raising=True)

    diff_results = iter([True, False])  # initial=clean, post-second-pass=dirty

    def _fake_diff_clean() -> bool:
        return next(diff_results)

    monkeypatch.setattr(regen_module, "_git_diff_is_clean", _fake_diff_clean)

    exit_code = regen_module.main(["--fixture", "DOCUMENTED_COMMANDS"])

    assert exit_code == regen_module.SELF_CHECK_FAIL_EXIT_CODE
    err = capsys.readouterr().err
    assert "non-idempotent" in err.lower()


def test_documented_commands_fixture_never_writes(regen_module, monkeypatch, capsys):
    """A registered-but-undocumented command must not earn itself a bot exemption.

    The autofix workflow pushes whatever this writes straight onto the source
    PR's head ref. If the fixture appended the command to
    ``UNDOCUMENTED_EXEMPTIONS``, an undocumented command would go green with no
    human in the loop -- exactly the hole #3468 closed.
    """
    written: list[Path] = []
    monkeypatch.setattr(
        regen_module,
        "_write_if_changed",
        lambda path, before, after: written.append(path) or True,
    )

    class _FakeCli:
        commands = {"zzz-not-in-the-reference": object()}

    monkeypatch.setitem(sys.modules, "bernstein.cli.main", types.SimpleNamespace(cli=_FakeCli))

    assert regen_module.regen_documented_commands() is False
    assert written == [], "the documented-commands fixture must not patch any file"

    err = capsys.readouterr().err
    assert "zzz-not-in-the-reference" in err, err
    assert "does not patch itself" in err, err
    # The exemption map on disk is untouched: no bot entry was added.
    assert "zzz-not-in-the-reference" not in regen_module.CLI_DOC_GATE_PATH.read_text(encoding="utf-8")


def test_documented_commands_fixture_reports_every_gate_invariant(regen_module, monkeypatch, capsys):
    """Reporting only missing commands would send the reader to the wrong place.

    The gate fails on four states, not one. A run that printed "nothing to add"
    while CI was red on a stale or malformed exemption reads as "the script has
    no opinion", which is worse than saying nothing.
    """
    fake_gate = types.SimpleNamespace(
        _parse_documented_commands=lambda text: {"documented-and-exempt", "registered-and-documented"},
        UNDOCUMENTED_EXEMPTIONS={
            "documented-and-exempt": "stale exemption",
            "not-a-command": "phantom exemption",
            "blank": "",
        },
    )
    monkeypatch.setattr(regen_module, "_load_cli_doc_gate", lambda: fake_gate)

    class _FakeCli:
        commands = {
            "registered-and-documented": object(),
            "documented-and-exempt": object(),
            "blank": object(),
            "undocumented": object(),
        }

    monkeypatch.setitem(sys.modules, "bernstein.cli.main", types.SimpleNamespace(cli=_FakeCli))

    assert regen_module.regen_documented_commands() is False

    err = capsys.readouterr().err
    assert "nor exempt: undocumented" in err, err
    assert "drop the exemption: documented-and-exempt" in err, err
    assert "not a registered command: not-a-command" in err, err
    assert "exempt without a reason: blank" in err, err


def test_documented_commands_fixture_refuses_without_the_reference(regen_module, monkeypatch, tmp_path, capsys):
    """A missing cli-reference.md must refuse, not treat every command as undocumented."""
    monkeypatch.setattr(regen_module, "CLI_REFERENCE_PATH", tmp_path / "no-such-cli-reference.md")

    called: list[str] = []
    monkeypatch.setattr(regen_module, "_load_cli_doc_gate", lambda: called.append("loaded"))

    assert regen_module.regen_documented_commands() is False
    assert called == [], "must bail out before classifying anything as undocumented"
    assert "is missing" in capsys.readouterr().err


def test_regen_and_gate_read_the_reference_the_same_way(regen_module):
    """The script must not carry a second copy of the documentation-parsing rules.

    Two copies drift, and a looser copy here would report a documented command
    as missing (or a missing one as documented) against the same file CI reads.
    """
    gate = regen_module._load_cli_doc_gate()
    reference = regen_module.CLI_REFERENCE_PATH.read_text(encoding="utf-8")
    assert gate._parse_documented_commands(reference) == gate._documented_commands_from_docs()


def test_skip_self_check_flag_disables_second_pass(regen_module, monkeypatch):
    """``--skip-self-check`` must bypass the re-run guard for debugging use."""
    call_count = {"n": 0}

    def _counting_stub() -> bool:
        call_count["n"] += 1
        return True  # would trip the self-check if run

    monkeypatch.setattr(
        regen_module,
        "FIXTURES",
        {"DOCUMENTED_COMMANDS": _counting_stub},
        raising=True,
    )
    monkeypatch.setattr(regen_module, "_git_diff_is_clean", lambda: True)

    exit_code = regen_module.main(["--fixture", "DOCUMENTED_COMMANDS", "--skip-self-check"])

    assert call_count["n"] == 1, "fixture should run exactly once when self-check skipped"
    assert exit_code == 0
