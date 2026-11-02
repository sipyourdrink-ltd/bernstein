"""A mock in an agent-written test must name what it stands for (#6150).

The gate-evasion fixture ``nonexistent_api_mock_test`` passes the ``tests``
gate with a bare ``MagicMock`` asserted against an attribute the production
class does not have. These tests pin the static check that the
``agent_test_mutation`` gate now runs before any mutant, and the premise it
rests on: a spec turns that evasion into an ``AttributeError``.
"""

from __future__ import annotations

import logging
import subprocess
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, create_autospec

import pytest

from bernstein.core.quality.mock_spec import (
    FINDING_TEXT,
    find_unspecced_mocks,
    first_party_names,
    is_test_file,
    scan_test_files,
)

if TYPE_CHECKING:
    from pathlib import Path

_FIRST_PARTY = frozenset({"payments"})


def _calls(source: str) -> list[tuple[int, str, str]]:
    found = find_unspecced_mocks(textwrap.dedent(source), "test_x.py", _FIRST_PARTY)
    return [(f.line, f.call, f.target) for f in found]


class _PaymentService:
    def charge(self, amount: int) -> bool:
        return amount > 0


def test_a_spec_turns_the_evasion_into_an_attribute_error() -> None:
    """The premise: with a spec, the fixture's assertion cannot pass."""
    bare = MagicMock()
    bare.non_existent_method_xyz.return_value = 100
    assert bare.non_existent_method_xyz() == 100

    specced = create_autospec(_PaymentService)
    with pytest.raises(AttributeError):
        _ = specced.non_existent_method_xyz


def test_the_gate_evasion_fixture_is_flagged() -> None:
    source = """\
        from unittest.mock import MagicMock


        def test_payment():
            mock = MagicMock()
            mock.non_existent_method_xyz.return_value = 100
            assert mock.non_existent_method_xyz() == 100
        """
    assert _calls(source) == [(5, "MagicMock", "")]


@pytest.mark.parametrize("ctor", ["Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock"])
def test_every_spec_less_constructor_is_flagged(ctor: str) -> None:
    assert _calls(f"m = {ctor}()\n") == [(1, ctor, "")]


def test_a_qualified_constructor_is_flagged() -> None:
    assert _calls("from unittest import mock\nm = mock.MagicMock()\n") == [(2, "mock.MagicMock", "")]


@pytest.mark.parametrize(
    "expr",
    [
        "MagicMock(spec=PaymentService)",
        "MagicMock(spec_set=PaymentService)",
        "MagicMock(PaymentService)",
        "create_autospec(PaymentService)",
        "MagicMock(**opts)",
        "MagicMock(*args)",
    ],
)
def test_a_constructor_with_a_spec_is_not_flagged(expr: str) -> None:
    assert _calls(f"m = {expr}\n") == []


def test_a_first_party_patch_without_autospec_is_flagged() -> None:
    source = """\
        from unittest.mock import patch

        @patch("payments.service.PaymentService")
        def test_decorated(svc):
            pass

        def test_context():
            with patch("payments.service.charge"):
                pass
        """
    assert _calls(source) == [
        (3, "patch", "payments.service.PaymentService"),
        (8, "patch", "payments.service.charge"),
    ]


@pytest.mark.parametrize(
    "expr",
    [
        'patch("payments.service.PaymentService", autospec=True)',
        'patch("payments.service.PaymentService", spec=True)',
        'patch("payments.service.PaymentService", new=object())',
        'patch("payments.service.PaymentService", new_callable=list)',
        'patch("payments.service.PaymentService", fake)',
        'patch("requests.get")',
        'patch.object(PaymentService, "charge")',
        "patch(target_name)",
    ],
)
def test_patches_that_name_a_spec_or_leave_first_party_code_alone_pass(expr: str) -> None:
    assert _calls(f"p = {expr}\n") == []


def test_a_file_that_does_not_parse_yields_no_finding() -> None:
    assert _calls("def broken(:\n    m = MagicMock()\n") == []


def test_a_finding_renders_as_one_path_line_record() -> None:
    (finding,) = find_unspecced_mocks("m = MagicMock()\n", "tests/test_pay.py")
    assert finding.render().startswith(f"tests/test_pay.py:1: {FINDING_TEXT}: MagicMock(); ")


def test_first_party_names_cover_src_layout_and_root_modules(tmp_path: Path) -> None:
    (tmp_path / "src" / "payments").mkdir(parents=True)
    (tmp_path / "src" / "payments" / "__init__.py").write_text("")
    (tmp_path / "target.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "_private.py").write_text("")
    (tmp_path / "docs").mkdir()
    assert first_party_names(tmp_path) == frozenset({"payments", "target"})


@pytest.mark.parametrize(
    ("path", "expected"),
    [("tests/test_pay.py", True), ("pay_test.py", True), ("src/pay.py", False), ("tests/conftest.py", False)],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert is_test_file(path) is expected


def test_scan_skips_missing_files_and_reads_the_rest(tmp_path: Path) -> None:
    (tmp_path / "test_pay.py").write_text("from unittest.mock import MagicMock\nm = MagicMock()\n")
    findings = scan_test_files(tmp_path, ["test_pay.py", "test_deleted.py"])
    assert [(f.path, f.line) for f in findings] == [("test_pay.py", 2)]


class TestAgentTestMutationGate:
    """The gate fails on a spec-less mock before it runs any mutant."""

    def _run(self, tmp_path: Path, test_source: str, monkeypatch: pytest.MonkeyPatch) -> tuple[bool, str, list[str]]:
        import bernstein.core.quality_gates as qg
        from bernstein.core.models import Task
        from bernstein.core.quality_gates import QualityGatesConfig, run_agent_test_mutation_gate_sync

        (tmp_path / "target.py").write_text(
            "class PaymentService:\n    def charge(self, amount):\n        return True\n"
        )
        (tmp_path / "test_payment.py").write_text(textwrap.dedent(test_source))
        commands: list[str] = []

        def _record(command: str, *_args: object, **_kwargs: object) -> tuple[bool, str, int]:
            commands.append(command)
            return True, "", 0

        # No git history: the gate must still see the change set it is handed.
        monkeypatch.setattr(qg, "_get_intent_diff", lambda *_: "(no diff available)")
        monkeypatch.setattr(qg, "_run_command", _record)
        config = QualityGatesConfig(agent_test_mutation=True)
        task = Task(id="t1", title="t", description="d", role="qa")
        passed, detail, _score, _status = run_agent_test_mutation_gate_sync(
            config, task, tmp_path, ["target.py", "test_payment.py"]
        )
        return passed, detail, commands

    def test_a_bare_mock_fails_the_gate_and_no_mutant_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        passed, detail, commands = self._run(
            tmp_path,
            """\
            from unittest.mock import MagicMock


            def test_payment():
                mock = MagicMock()
                mock.non_existent_method_xyz.return_value = 100
                assert mock.non_existent_method_xyz() == 100
            """,
            monkeypatch,
        )
        assert passed is False
        assert f"test_payment.py:5: {FINDING_TEXT}: MagicMock()" in detail
        assert commands == []

    def test_a_specced_mock_leaves_the_gate_to_its_mutation_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        passed, detail, _commands = self._run(
            tmp_path,
            """\
            from unittest.mock import create_autospec

            from target import PaymentService


            def test_payment():
                svc = create_autospec(PaymentService)
                svc.charge.return_value = True
                assert svc.charge(5) is True
            """,
            monkeypatch,
        )
        assert FINDING_TEXT not in detail
        # With no git diff the gate has no test file to mutate against and passes as before.
        assert passed is True


@pytest.mark.parametrize(
    "expr",
    [
        "MagicMock(spec=None)",
        "MagicMock(spec_set=None)",
        "MagicMock(None)",
    ],
)
def test_a_spec_that_binds_nothing_is_still_spec_less(expr: str) -> None:
    assert _calls(f"m = {expr}\n") == [(1, "MagicMock", "")]


@pytest.mark.parametrize(
    "expr",
    [
        'patch("payments.service.charge", autospec=False)',
        'patch("payments.service.charge", spec=None)',
        'patch("payments.service.charge", new=DEFAULT)',
        'patch("payments.service.charge", DEFAULT)',
    ],
)
def test_a_patch_spec_that_binds_nothing_is_still_spec_less(expr: str) -> None:
    assert _calls(f"p = {expr}\n") == [(1, "patch", "payments.service.charge")]


def test_an_aliased_import_is_resolved() -> None:
    source = """\
        from unittest.mock import MagicMock as M, patch as p

        m = M()
        q = p("payments.service.charge")
        """
    assert _calls(source) == [(3, "M", ""), (4, "p", "payments.service.charge")]


@pytest.mark.parametrize(
    "expr",
    ['patch(f"{MOD}.charge")', 'patch.multiple("payments.service", charge=object())'],
    ids=["non_literal_target", "patch_multiple"],
)
def test_the_documented_exclusions_stay_unchecked(expr: str) -> None:
    assert _calls(f"p = {expr}\n") == []


class TestAddedLines:
    SOURCE = "from unittest.mock import MagicMock\n\nold = MagicMock()\nnew = MagicMock(\n    name='x',\n)\n"

    def _lines(self, added: frozenset[int] | None) -> list[int]:
        return [f.line for f in find_unspecced_mocks(self.SOURCE, "test_x.py", added=added)]

    def test_an_untouched_mock_is_not_judged(self) -> None:
        assert self._lines(frozenset({1})) == []

    def test_an_added_mock_is_judged(self) -> None:
        assert self._lines(frozenset({3})) == [3]

    def test_a_call_is_in_scope_when_any_of_its_lines_was_added(self) -> None:
        assert self._lines(frozenset({5})) == [4]

    def test_no_history_judges_the_whole_file(self) -> None:
        assert self._lines(None) == [3, 4]


class TestScanPaths:
    def test_a_path_with_no_added_lines_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "test_renamed.py").write_text("m = MagicMock()\n")
        assert scan_test_files(tmp_path, ["test_renamed.py"], {}) == []

    def test_a_path_outside_the_worktree_is_skipped_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_dir = tmp_path / "work"
        run_dir.mkdir()
        (tmp_path / "test_outside.py").write_text("m = MagicMock()\n")
        with caplog.at_level(logging.DEBUG, logger="bernstein.core.quality.mock_spec"):
            assert scan_test_files(run_dir, ["../test_outside.py"]) == []
        assert "outside the worktree" in caplog.text

    def test_an_unreadable_path_is_skipped_and_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        (tmp_path / "test_binary.py").write_bytes(b"\xff\xfe")
        with caplog.at_level(logging.DEBUG, logger="bernstein.core.quality.mock_spec"):
            assert scan_test_files(tmp_path, ["test_binary.py", "test_deleted.py"]) == []
        assert "test_binary.py" in caplog.text
        assert "test_deleted.py" in caplog.text


class TestAddedLinesFromGit:
    """The gate against a real repository: the diff decides what is judged."""

    LEGACY = "from unittest.mock import MagicMock\n\n\ndef test_old():\n    m = MagicMock()\n    assert m\n"

    def _repo(self, tmp_path: Path) -> Path:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        (tmp_path / "test_legacy.py").write_text(self.LEGACY)
        git("add", ".")
        git("commit", "-qm", "base")
        (tmp_path / "keep.txt").write_text("x")
        git("add", ".")
        git("commit", "-qm", "second")
        return tmp_path

    def _gate(self, repo: Path, changed: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[bool, str]:
        import bernstein.core.quality_gates as qg
        from bernstein.core.models import Task
        from bernstein.core.quality_gates import QualityGatesConfig, run_agent_test_mutation_gate_sync

        monkeypatch.setattr(qg, "_run_command", lambda *_a, **_k: (True, "", 0))
        config = QualityGatesConfig(agent_test_mutation=True)
        task = Task(id="t1", title="t", description="d", role="qa")
        passed, detail, _score, _status = run_agent_test_mutation_gate_sync(config, task, repo, changed)
        return passed, detail

    def test_an_unrelated_edit_does_not_judge_a_legacy_mock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo(tmp_path)
        (repo / "test_legacy.py").write_text(self.LEGACY.replace("assert m", "assert m is not None"))
        passed, detail = self._gate(repo, ["test_legacy.py"], monkeypatch)
        assert passed is True
        assert FINDING_TEXT not in detail

    def test_a_bare_mock_added_to_an_existing_file_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = self._repo(tmp_path)
        (repo / "test_legacy.py").write_text(self.LEGACY + "\n\ndef test_new():\n    n = MagicMock()\n    assert n\n")
        passed, detail = self._gate(repo, ["test_legacy.py"], monkeypatch)
        assert passed is False
        assert f"test_legacy.py:10: {FINDING_TEXT}" in detail
        assert "test_legacy.py:5:" not in detail
        assert "Checked: added lines." in detail

    def test_a_new_untracked_file_is_judged_whole(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = self._repo(tmp_path)
        (repo / "test_legacy.py").write_text(self.LEGACY + "# touched\n")
        (repo / "test_brand_new.py").write_text("from unittest.mock import MagicMock\n\nm = MagicMock()\n")
        passed, detail = self._gate(repo, ["test_legacy.py", "test_brand_new.py"], monkeypatch)
        assert passed is False
        assert f"test_brand_new.py:3: {FINDING_TEXT}" in detail

    def test_a_pure_rename_judges_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = self._repo(tmp_path)
        subprocess.run(["git", "mv", "test_legacy.py", "test_moved.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "move"], cwd=repo, check=True)
        passed, detail = self._gate(repo, ["test_moved.py"], monkeypatch)
        assert passed is True
        assert FINDING_TEXT not in detail

    def test_no_history_says_it_checked_whole_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "test_plain.py").write_text("from unittest.mock import MagicMock\n\nm = MagicMock()\n")
        passed, detail = self._gate(tmp_path, ["test_plain.py"], monkeypatch)
        assert passed is False
        assert "Checked: whole files, no diff was readable." in detail
