"""
TDD tests for compliance control registry and suite control declarations (Issue #5455).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.compliance_cmd import compliance_group
from bernstein.compliance.controls import (
    Control,
    get_default_registry,
)
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.suite import BenchSuite, BenchTask


class TestControlRegistry:
    def test_registry_contains_at_least_30_controls(self) -> None:
        registry = get_default_registry()
        controls = registry.list_controls()
        assert len(controls) >= 30

    def test_controls_cover_all_mandated_frameworks(self) -> None:
        registry = get_default_registry()
        controls = registry.list_controls()
        frameworks_present = set()
        for c in controls:
            assert c.control_id.startswith("CTL-")
            assert len(c.title) > 0
            assert len(c.description) > 0
            assert len(c.evidence_kinds) > 0
            for fw in c.references:
                frameworks_present.add(fw)

        expected_frameworks = {
            "eu_ai_act",
            "owasp_asi",
            "owasp_skills",
            "nist_ai_rmf",
            "iso_42001",
            "finos_aigf",
        }
        for fw in expected_frameworks:
            assert fw in frameworks_present, f"Framework {fw} missing from registry"

    def test_control_lookup(self) -> None:
        registry = get_default_registry()
        c = registry.get("CTL-GOV-01")
        assert c is not None
        assert c.control_id == "CTL-GOV-01"
        assert "eu_ai_act" in c.references
        assert "audit_chain" in c.evidence_kinds or "policy" in c.evidence_kinds or len(c.evidence_kinds) > 0

    def test_filter_by_framework(self) -> None:
        registry = get_default_registry()
        eu_controls = registry.list_controls(framework="eu_ai_act")
        assert len(eu_controls) > 0
        for c in eu_controls:
            assert "eu_ai_act" in c.references

    def test_validate_control_ids(self) -> None:
        registry = get_default_registry()
        assert registry.validate_control_ids(["CTL-GOV-01", "CTL-ROB-01"]) == []
        invalid = registry.validate_control_ids(["CTL-GOV-01", "INVALID-99", "NONEXISTENT"])
        assert invalid == ["INVALID-99", "NONEXISTENT"]

    def test_markdown_table_generation(self) -> None:
        registry = get_default_registry()
        md = registry.to_markdown_table()
        assert "| Control ID | Title | Frameworks | Evidence Kinds |" in md
        assert "CTL-GOV-01" in md


class TestBenchSuiteControlEnforcement:
    def test_golden_suite_declares_valid_controls(self) -> None:
        suite = build_golden_suite_v1()
        assert len(suite.controls) > 0
        suite.validate_controls()

    def test_suite_without_controls_fails_validation(self) -> None:
        suite = BenchSuite(
            version="unmapped-v1",
            tasks=[BenchTask(id="t1", description="t1", steps=("s1",), assertions=())],
            controls=[],
        )
        with pytest.raises(ValueError, match="must declare at least one control ID"):
            suite.validate_controls()

    def test_suite_with_unregistered_control_fails_validation(self) -> None:
        suite = BenchSuite(
            version="bad-control-v1",
            tasks=[BenchTask(id="t1", description="t1", steps=("s1",), assertions=())],
            controls=["CTL-NON-EXISTENT-XYZ"],
        )
        with pytest.raises(ValueError, match="unregistered control IDs"):
            suite.validate_controls()

    def test_suite_hash_includes_controls(self) -> None:
        t = BenchTask(id="t1", description="t1", steps=("s1",), assertions=())
        suite1 = BenchSuite(version="v1", tasks=[t], controls=["CTL-GOV-01"])
        suite2 = BenchSuite(version="v1", tasks=[t], controls=["CTL-ROB-01"])
        suite3 = BenchSuite(version="v1", tasks=[t], controls=["CTL-GOV-01"])

        assert suite1.suite_hash == suite3.suite_hash
        assert suite1.suite_hash != suite2.suite_hash

    def test_suite_save_and_load_roundtrip_with_controls(self, tmp_path: Path) -> None:
        t = BenchTask(id="t1", description="t1", steps=("s1",), assertions=())
        suite = BenchSuite(version="v1", tasks=[t], controls=["CTL-GOV-01", "CTL-ROB-01"])
        suite_file = tmp_path / "suite.json"
        suite.save(suite_file)

        loaded = BenchSuite.load(suite_file)
        assert loaded.controls == ["CTL-GOV-01", "CTL-ROB-01"]
        assert loaded.suite_hash == suite.suite_hash


class TestComplianceControlsCLI:
    def test_compliance_controls_text(self) -> None:
        """Through the real root ``cli``, not the group object, so a break in
        ``cli.add_command(compliance_group, "compliance")`` is caught here."""
        from bernstein.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["compliance", "controls"])
        assert result.exit_code == 0
        assert "CTL-GOV-01" in result.output
        assert "Control ID" in result.output or "Title" in result.output

    def test_compliance_controls_json(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 30
        assert any(c["control_id"] == "CTL-GOV-01" for c in data)

    def test_compliance_controls_framework_filter(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--framework", "eu_ai_act", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) > 0
        for c in data:
            assert "eu_ai_act" in c["references"]

    def test_compliance_controls_coverage(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--coverage"])
        assert result.exit_code == 0
        assert "golden-v1" in result.output or "Coverage" in result.output


class TestControlsAreEnforcedAndDocumented:
    """What #5455's acceptance criteria require, pinned against the real entry points."""

    MAIN_GOLDEN_V1_HASH = "9c553f1f303ede1ef69131f3f9d2645dc7f32ac7b868b7b3108a410ac269fb97"

    def test_every_builtin_suite_declares_a_registered_control(self) -> None:
        """Every suite ``bench`` resolves by name declares controls the registry knows.

        Iterates ``builtin_suite_builders()`` -- the same list ``_get_suite``,
        ``compliance controls --coverage`` and the docs table read -- so a
        built-in added there without a declaration fails here, not at the
        operator's first ``bench run``.
        """
        from bernstein.eval.bench.bench_cli import builtin_suite_builders

        builders = builtin_suite_builders()
        assert builders, "no built-in suites registered"
        for name, build in builders.items():
            suite = build()
            assert suite.controls, f"{name} declares no controls"
            assert get_default_registry().validate_control_ids(suite.controls) == [], name

    @pytest.mark.parametrize("subcommand", ["run", "verify", "reliability-verify", "reliability-check"])
    def test_every_bench_subcommand_refuses_an_unmapped_suite(self, tmp_path: Path, subcommand: str) -> None:
        """``validate_controls`` sits in ``_get_suite``, which every subcommand goes through (#5455).

        Parametrised over all four so moving the call into one subcommand
        cannot leave the others ungated. Drives the real root ``cli``. The
        verify-style subcommands parse their artifact before resolving the
        suite, so a real bundle and a real reliability receipt are produced
        first from a suite that *is* mapped, then handed over with the bare one.
        """
        from bernstein.cli.main import cli

        runner = CliRunner()
        task = BenchTask(id="t", description="d", steps=("s",), assertions=())
        bare_path = tmp_path / "bare.json"
        BenchSuite(version="bare-v1", tasks=[task]).save(bare_path)
        good_path = tmp_path / "good.json"
        BenchSuite(version="good-v1", tasks=[task], controls=["CTL-EVAL-01"]).save(good_path)

        bundle = tmp_path / "bundle.json"
        receipt = tmp_path / "receipt.json"
        assert runner.invoke(cli, ["bench", "run", str(good_path), "--out", str(bundle), "--stub-signer"]).exit_code == 0
        assert (
            runner.invoke(
                cli, ["bench", "run", str(good_path), "--out", str(receipt), "--stub-signer", "--reliability", "2"]
            ).exit_code
            == 0
        )

        args = {
            "run": ["bench", "run", str(bare_path), "--out", str(tmp_path / "out.json"), "--stub-signer"],
            "verify": ["bench", "verify", str(bundle), "--suite", str(bare_path)],
            "reliability-verify": ["bench", "reliability-verify", str(receipt), "--suite", str(bare_path)],
            "reliability-check": ["bench", "reliability-check", str(receipt), "--suite", str(bare_path)],
        }[subcommand]
        result = runner.invoke(cli, args)

        assert result.exit_code != 0
        assert "must declare at least one control" in result.output, result.output

    def test_a_control_registered_on_the_default_registry_reaches_the_gate(self, tmp_path: Path) -> None:
        """The singleton is the extension point: a custom control admits a suite that declares it.

        This is why ``get_default_registry`` returns the shared instance rather
        than a fresh copy -- a fresh copy would be leak-proof and would also
        cut off the only way a plugin's control can become known to the gate.
        """
        registry = get_default_registry()
        registry.register(Control(control_id="CTL-ORG-TEST", title="t", description="d"))
        try:
            suite = BenchSuite(
                version="org-v1",
                tasks=[BenchTask(id="t", description="d", steps=("s",), assertions=())],
                controls=["CTL-ORG-TEST"],
            )
            suite.validate_controls()  # admitted because the gate reads the same registry
        finally:
            registry._controls.pop("CTL-ORG-TEST", None)

    def test_a_suite_declaring_no_controls_keeps_the_hash_main_published(self) -> None:
        """The compatibility guarantee, pinned to the value ``main`` computes today.

        ``golden-v1``'s tasks with no declaration must hash to exactly what
        the pre-#5455 code produced, or every bundle and receipt that names
        that hash is orphaned. If golden-v1's tasks change deliberately, this
        literal changes with them -- that is the point of pinning it.
        """
        bare = BenchSuite(version="golden-v1", tasks=list(build_golden_suite_v1().tasks))

        assert bare.suite_hash == self.MAIN_GOLDEN_V1_HASH
        assert "controls" not in bare.to_dict()

    def test_controls_order_and_duplicates_do_not_fork_identity(self) -> None:
        """One declaration, one hash -- the same rule ``holdout_hash`` follows."""
        tasks = list(build_golden_suite_v1().tasks)
        a = BenchSuite(version="v", tasks=tasks, controls=["CTL-EVAL-01", "CTL-ROB-01"])
        b = BenchSuite(version="v", tasks=tasks, controls=["CTL-ROB-01", "CTL-EVAL-01"])
        c = BenchSuite(version="v", tasks=tasks, controls=["CTL-ROB-01", "CTL-EVAL-01", "CTL-ROB-01"])

        assert a.suite_hash == b.suite_hash == c.suite_hash

    @pytest.mark.parametrize(
        "bad",
        ["CTL-EVAL-01", {"CTL-EVAL-01": True}, [None], [1], [""]],
        ids=["string", "dict", "null-item", "int-item", "empty-item"],
    )
    def test_load_rejects_a_malformed_controls_field(self, tmp_path: Path, bad: object) -> None:
        """A field bound into identity is shape-checked at load, with a message that names the problem."""
        good = BenchSuite(
            version="v", tasks=[BenchTask(id="t", description="d", steps=("s",), assertions=())], controls=["CTL-EVAL-01"]
        )
        path = tmp_path / "s.json"
        good.save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["controls"] = bad
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(ValueError, match="must be a list of non-empty strings"):
            BenchSuite.load(path)

    def test_the_docs_table_is_generated_from_the_registry(self) -> None:
        """``docs/compliance/regulator-mapped-packs.md`` carries the registry table verbatim.

        Expected output is rendered from ``builtin_suite_builders()``, so a
        third built-in suite is reflected in the doc or this fails. There is
        no generator script: this test *is* the drift check, and its failure
        message says how to regenerate.
        """
        from bernstein.eval.bench.bench_cli import builtin_suite_builders

        doc = (Path(__file__).resolve().parents[3] / "docs" / "compliance" / "regulator-mapped-packs.md").read_text(
            encoding="utf-8"
        )
        start = doc.index("-->", doc.index("<!-- controls-table:start")) + len("-->")
        end = doc.index("<!-- controls-table:end -->")
        in_doc = doc[start:end].strip()

        suites = [build() for build in builtin_suite_builders().values()]
        expected = get_default_registry().to_markdown_table(suites=suites).strip()

        assert in_doc == expected, (
            "regulator-mapped-packs.md controls table has drifted from ControlRegistry. Regenerate with: "
            "get_default_registry().to_markdown_table(suites=[b() for b in builtin_suite_builders().values()])"
        )
