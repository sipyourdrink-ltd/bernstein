"""Tests for the weekly transitive-pin staleness report (#4001).

The diff logic is exercised against synthetic pin sets rather than the real
files: what a fresh resolve produces changes every week by design, so an
assertion against the live tree would be a test that reports the weather.

The one place the real files are read is the invocation guard. #3995 was a
compiled artifact drifting from the command documented to produce it, and
this script recompiles with that command: if the two disagree again, every
package reports as moved on the first run and the report is noise rather
than a finding.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from check_docs_requirements_staleness import (
    PIP_COMPILE_ARGV,
    PinDrift,
    diff_pins,
    parse_pins,
)

_HASH_TAIL = " \\\n    --hash=sha256:" + "0" * 64


class TestDiffPins:
    def test_identical_resolutions_report_nothing(self) -> None:
        pins = {"babel": "2.18.0", "mkdocs": "1.6.1"}

        assert diff_pins(pins, dict(pins)) == []

    def test_a_moved_transitive_pin_is_reported_with_both_versions(self) -> None:
        drifts = diff_pins({"certifi": "2026.4.1"}, {"certifi": "2026.7.2"})

        assert [d.package for d in drifts] == ["certifi"]
        assert drifts[0].kind == "moved"
        assert "2026.4.1" in drifts[0].render()
        assert "2026.7.2" in drifts[0].render()

    def test_a_package_a_fresh_resolve_adds_is_reported(self) -> None:
        # A direct dependency gaining a new requirement shows up here, not in
        # the direct-constraint gate, which only knows the four named in the
        # .in file.
        drifts = diff_pins({}, {"properdocs": "1.6.7"})

        assert drifts[0].kind == "added"
        assert drifts[0].committed is None

    def test_a_package_a_fresh_resolve_drops_is_reported(self) -> None:
        drifts = diff_pins({"orphaned": "1.0.0"}, {})

        assert drifts[0].kind == "removed"
        assert drifts[0].resolved is None

    def test_drifts_are_name_sorted_so_two_runs_read_the_same(self) -> None:
        drifts = diff_pins({"zzz": "1", "aaa": "1"}, {"zzz": "2", "aaa": "2"})

        assert [d.package for d in drifts] == ["aaa", "zzz"]

    def test_names_are_compared_canonically(self) -> None:
        # PEP 503: Foo_Bar and foo-bar are one project. Comparing raw strings
        # would report every underscore-spelled package as both added and
        # removed, every week, forever.
        assert diff_pins(parse_pins("Foo_Bar==1.0\n"), parse_pins("foo-bar==1.0\n")) == []


class TestParsePins:
    def test_hash_continuations_are_not_pins(self) -> None:
        text = f"babel==2.18.0{_HASH_TAIL}\n    --hash=sha256:{'1' * 64}\n    # via mkdocs\n"

        assert parse_pins(text) == {"babel": "2.18.0"}

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        assert parse_pins("# a comment\n\nmkdocs==1.6.1\n") == {"mkdocs": "1.6.1"}


class TestTheRecompileUsesTheDocumentedCommand:
    """#3995 one layer on: a recompile built on the wrong command reports noise."""

    def in_file_command(self) -> str:
        lines = (REPO_ROOT / "docs" / "requirements.in").read_text(encoding="utf-8").splitlines()
        marker = next(
            (i for i, line in enumerate(lines) if "Regenerate after any change here with:" in line),
            None,
        )
        assert marker is not None, "docs/requirements.in no longer documents a regeneration command"
        collected: list[str] = []
        for line in lines[marker + 1 :]:
            if not line.startswith("#"):
                break
            body = line[1:]
            if body.strip() == "":
                if collected:
                    break
                continue
            if not body.startswith("   "):
                break
            collected.append(body.strip().rstrip("\\").strip())
        assert collected, "found the marker but no indented command under it"
        return " ".join(collected)

    @pytest.mark.parametrize(
        "flag",
        ["--generate-hashes", "--strip-extras", "--output-file", "pip-compile"],
        ids=["hashes", "strip-extras", "output-file", "compiler"],
    )
    def test_every_flag_this_script_compiles_with_is_documented(self, flag: str) -> None:
        assert flag in PIP_COMPILE_ARGV, f"{flag} vanished from PIP_COMPILE_ARGV"
        assert flag in self.in_file_command(), (
            f"docs/requirements.in no longer documents {flag}, so this script would recompile "
            f"with a command that does not reproduce the committed file and report every "
            f"package as moved"
        )

    def test_the_committed_file_records_the_same_invocation(self) -> None:
        header = "\n".join(
            line
            for line in (REPO_ROOT / "docs" / "requirements.txt").read_text(encoding="utf-8").splitlines()[:12]
            if line.startswith("#")
        )

        assert "--strip-extras" in header
        assert "--strip-extras" in self.in_file_command()

    def test_quiet_is_passed_so_progress_output_is_not_parsed_as_pins(self) -> None:
        assert "--quiet" in PIP_COMPILE_ARGV


class TestRenderNamesTheVersionsNotJustThePackage:
    def test_a_report_line_carries_enough_to_act_on(self) -> None:
        # "certifi moved" costs the reader a lookup; the whole point of the
        # weekly report is that the issue body is smaller than the diff.
        line = PinDrift(package="certifi", committed="2026.4.1", resolved="2026.7.2").render()

        assert "certifi" in line
        assert "2026.4.1" in line
        assert "2026.7.2" in line
