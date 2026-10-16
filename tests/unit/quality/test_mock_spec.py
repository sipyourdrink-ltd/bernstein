"""A mock in an agent-written test must name what it stands for (#6150).

The gate-evasion fixture ``nonexistent_api_mock_test`` passes the ``tests``
gate with a bare ``MagicMock`` asserted against an attribute the production
class does not have. These tests pin the static check that the
``agent_test_mutation`` gate now runs before any mutant, and the premise it
rests on: a spec turns that evasion into an ``AttributeError``.
"""

from __future__ import annotations

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
