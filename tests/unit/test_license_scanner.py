"""Tests for license scanner: copyleft detection in git diffs."""

from __future__ import annotations

from bernstein.core.license_scanner import _scan_diff_for_licenses, check_license_obligations
from bernstein.core.policy_engine import DecisionType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _added(content: str, filepath: str = "src/foo.py") -> str:
    """Build a minimal diff with one added line."""
    return (
        f"diff --git a/{filepath} b/{filepath}\n"
        f"--- a/{filepath}\n"
        f"+++ b/{filepath}\n"
        f"@@ -1,1 +1,2 @@\n"
        f" existing\n"
        f"+{content}\n"
    )


def _removed(content: str, filepath: str = "src/foo.py") -> str:
    """Build a minimal diff with one removed line (should not trigger)."""
    return (
        f"diff --git a/{filepath} b/{filepath}\n"
        f"--- a/{filepath}\n"
        f"+++ b/{filepath}\n"
        f"@@ -1,2 +1,1 @@\n"
        f" existing\n"
        f"-{content}\n"
    )


# ---------------------------------------------------------------------------
# SPDX identifier — strong copyleft
# ---------------------------------------------------------------------------


class TestSpdxStrongCopyleft:
    def test_gpl3_spdx_hard_blocks(self) -> None:
        diff = _added("# SPDX-License-Identifier: GPL-3.0-or-later")
        results = check_license_obligations(diff)
        assert len(results) == 1
        assert results[0].type == DecisionType.DENY
        assert results[0].bypass_immune

    def test_gpl2_spdx_hard_blocks(self) -> None:
        diff = _added("# SPDX-License-Identifier: GPL-2.0-only")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY

    def test_agpl3_spdx_hard_blocks(self) -> None:
        diff = _added("# SPDX-License-Identifier: AGPL-3.0-only")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY

    def test_osl_spdx_hard_blocks(self) -> None:
        diff = _added("# SPDX-License-Identifier: OSL-3.0")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY

    def test_detail_names_license_id(self) -> None:
        diff = _added("# SPDX-License-Identifier: GPL-3.0")
        results = check_license_obligations(diff)
        assert "GPL-3.0" in results[0].reason


# ---------------------------------------------------------------------------
# SPDX identifier — weak copyleft
# ---------------------------------------------------------------------------


class TestSpdxWeakCopyleft:
    def test_lgpl21_spdx_soft_flags(self) -> None:
        diff = _added("# SPDX-License-Identifier: LGPL-2.1-or-later")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK

    def test_mpl2_spdx_soft_flags(self) -> None:
        diff = _added("# SPDX-License-Identifier: MPL-2.0")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK

    def test_lgpl3_spdx_soft_flags(self) -> None:
        diff = _added("# SPDX-License-Identifier: LGPL-3.0-only")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK

    def test_eupl_spdx_soft_flags(self) -> None:
        diff = _added("# SPDX-License-Identifier: EUPL-1.2")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK

    def test_detail_names_license_id(self) -> None:
        diff = _added("# SPDX-License-Identifier: LGPL-2.1")
        results = check_license_obligations(diff)
        assert "LGPL-2.1" in results[0].reason


# ---------------------------------------------------------------------------
# SPDX compound expressions
# ---------------------------------------------------------------------------


class TestSpdxCompoundExpressions:
    def test_gpl_or_mit_blocks_due_to_gpl(self) -> None:
        diff = _added("# SPDX-License-Identifier: GPL-3.0-or-later OR MIT")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY

    def test_lgpl_and_apache_soft_flags(self) -> None:
        diff = _added("# SPDX-License-Identifier: LGPL-2.1-only AND Apache-2.0")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK

    def test_permissive_only_passes(self) -> None:
        diff = _added("# SPDX-License-Identifier: MIT AND Apache-2.0")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ALLOW


# ---------------------------------------------------------------------------
# Prose license headers
# ---------------------------------------------------------------------------


class TestProseLicenseHeaders:
    def test_gpl_prose_hard_blocks(self) -> None:
        diff = _added("# This file is licensed under the GNU General Public License.")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY

    def test_agpl_prose_hard_blocks(self) -> None:
        diff = _added("# GNU Affero General Public License, version 3 or later.")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY

    def test_lgpl_prose_soft_flags(self) -> None:
        diff = _added("# GNU Lesser General Public License as published by the FSF.")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK

    def test_mpl_prose_soft_flags(self) -> None:
        diff = _added("# This Source Code Form is subject to the terms of the Mozilla Public License.")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK

    def test_gpl_version3_prose_hard_blocks(self) -> None:
        diff = _added("# Released under the GNU General Public License version 3.")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY

    def test_lgpl_version21_prose_soft_flags(self) -> None:
        diff = _added("# GNU Lesser General Public License version 2.1")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ASK


# ---------------------------------------------------------------------------
# Removed lines must NOT trigger
# ---------------------------------------------------------------------------


class TestRemovedLinesIgnored:
    def test_removed_gpl_header_passes(self) -> None:
        diff = _removed("# SPDX-License-Identifier: GPL-3.0-or-later")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ALLOW

    def test_removed_lgpl_header_passes(self) -> None:
        diff = _removed("# GNU Lesser General Public License v2.1")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ALLOW


# ---------------------------------------------------------------------------
# Clean diff
# ---------------------------------------------------------------------------


class TestCleanDiff:
    def test_empty_diff_passes(self) -> None:
        results = check_license_obligations("")
        assert results[0].type == DecisionType.ALLOW

    def test_pure_code_diff_passes(self) -> None:
        diff = _added("def greet(name: str) -> str:")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ALLOW

    def test_mit_license_passes(self) -> None:
        diff = _added("# SPDX-License-Identifier: MIT")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ALLOW

    def test_apache_license_passes(self) -> None:
        diff = _added("# SPDX-License-Identifier: Apache-2.0")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ALLOW

    def test_bsd_license_passes(self) -> None:
        diff = _added("# SPDX-License-Identifier: BSD-3-Clause")
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.ALLOW


# ---------------------------------------------------------------------------
# Strong beats weak
# ---------------------------------------------------------------------------


class TestSeverityPrecedence:
    def test_strong_beats_weak_in_same_diff(self) -> None:
        diff = _added("# SPDX-License-Identifier: LGPL-2.1", "src/lib.py") + _added(
            "# SPDX-License-Identifier: GPL-3.0", "src/core.py"
        )
        results = check_license_obligations(diff)
        assert results[0].type == DecisionType.DENY  # hard block because GPL present

    def test_multiple_files_reported_in_files_list(self) -> None:
        diff = _added("# SPDX-License-Identifier: GPL-3.0", "src/a.py") + _added(
            "# SPDX-License-Identifier: GPL-2.0", "src/b.py"
        )
        results = check_license_obligations(diff)
        assert "src/a.py" in results[0].files
        assert "src/b.py" in results[0].files


# ---------------------------------------------------------------------------
# Deduplication — multi-line boilerplate should not multiply hits
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_repeated_gpl_lines_produce_single_result(self) -> None:
        # Typical GPL boilerplate spans multiple lines
        filepath = "src/foo.py"
        diff = (
            f"diff --git a/{filepath} b/{filepath}\n"
            f"--- a/{filepath}\n+++ b/{filepath}\n@@ -1,1 +1,5 @@\n"
            "+# GNU General Public License\n"
            "+# This program is free software; you can redistribute it\n"
            "+# under the terms of the GNU General Public License\n"
            "+# as published by the Free Software Foundation.\n"
        )
        hits = _scan_diff_for_licenses(diff)
        # All GPL hits from the same file should be deduplicated per label
        gpl_hits = [h for h in hits if "GPL" in h.license_id]
        unique_ids = {h.license_id for h in gpl_hits}
        # Deduplication: at most one hit per (file, license_id)
        assert len(gpl_hits) == len(unique_ids)


# ---------------------------------------------------------------------------
# Integration: run_guardrails includes license_obligations
# ---------------------------------------------------------------------------


class TestRunGuardrailsIntegration:
    def test_run_guardrails_includes_license_check(self, tmp_path: object) -> None:
        from pathlib import Path

        from bernstein.core.guardrails import GuardrailsConfig, run_guardrails
        from bernstein.core.models import Complexity, Scope, Task

        task = Task(
            id="T-001",
            title="test",
            description="test",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            owned_files=[],
        )
        diff = _added("# SPDX-License-Identifier: GPL-3.0")
        results = run_guardrails(diff, task, GuardrailsConfig(), Path(str(tmp_path)))
        checks = {r.check for r in results}
        assert "license_obligations" in checks

    def test_run_guardrails_skips_license_when_disabled(self, tmp_path: object) -> None:
        from pathlib import Path

        from bernstein.core.guardrails import GuardrailsConfig, run_guardrails
        from bernstein.core.models import Complexity, Scope, Task

        task = Task(
            id="T-001",
            title="test",
            description="test",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            owned_files=[],
        )
        diff = _added("# SPDX-License-Identifier: GPL-3.0")
        results = run_guardrails(diff, task, GuardrailsConfig(license_scan=False), Path(str(tmp_path)))
        checks = {r.check for r in results}
        assert "license_obligations" not in checks

    def test_gpl_in_diff_is_hard_blocked_end_to_end(self, tmp_path: object) -> None:
        from pathlib import Path

        from bernstein.core.guardrails import GuardrailsConfig, run_guardrails
        from bernstein.core.models import Complexity, Scope, Task

        task = Task(
            id="T-002",
            title="test",
            description="test",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            owned_files=[],
        )
        diff = _added("# SPDX-License-Identifier: AGPL-3.0-or-later")
        results = run_guardrails(diff, task, GuardrailsConfig(), Path(str(tmp_path)))
        license_result = next(r for r in results if r.check == "license_obligations")
        assert not license_result.passed
        assert license_result.blocked
