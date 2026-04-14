"""Tests for migration_reversibility and large_file quality gates."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.gate_runner import (
    GatePipelineStep,
    GateRunner,
    build_default_pipeline,
)
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import QualityGatesConfig

from bernstein.core.quality.gate_commands import _migration_downgrade_is_pass


def _task() -> Task:
    return Task(
        id="T-test",
        title="Test task",
        description="Test.",
        role="qa",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
    )


def _step(name: str, *, required: bool = True) -> GatePipelineStep:
    return GatePipelineStep(name=name, required=required, condition="always")


def _runner(tmp_path: Path, **config_kwargs: object) -> GateRunner:
    cfg = QualityGatesConfig(**config_kwargs)  # type: ignore[arg-type]
    return GateRunner(cfg, tmp_path)


# ---------------------------------------------------------------------------
# _migration_downgrade_is_pass helper
# ---------------------------------------------------------------------------


class TestMigrationDowngradeIsPass:
    def test_no_downgrade_function(self) -> None:
        source = "def upgrade():\n    op.add_column('t', sa.Column('x', sa.Integer))\n"
        assert _migration_downgrade_is_pass(source) is False

    def test_downgrade_with_real_body(self) -> None:
        source = (
            "def upgrade():\n    op.add_column('t', sa.Column('x', sa.Integer))\n\n"
            "def downgrade():\n    op.drop_column('t', 'x')\n"
        )
        assert _migration_downgrade_is_pass(source) is False

    def test_downgrade_is_pass(self) -> None:
        source = "def upgrade():\n    op.add_column('t', sa.Column('x', sa.Integer))\n\ndef downgrade():\n    pass\n"
        assert _migration_downgrade_is_pass(source) is True

    def test_downgrade_is_docstring_then_pass(self) -> None:
        source = 'def downgrade():\n    """No-op."""\n    pass\n'
        assert _migration_downgrade_is_pass(source) is True

    def test_downgrade_is_docstring_only_body(self) -> None:
        source = 'def downgrade():\n    """No-op.\n    This migration is not reversible.\n    """\n'
        # Docstring-only body → treated as empty → is_pass = True
        assert _migration_downgrade_is_pass(source) is True

    def test_invalid_syntax_returns_false(self) -> None:
        assert _migration_downgrade_is_pass("def )(invalid") is False


# ---------------------------------------------------------------------------
# migration_reversibility gate — Alembic
# ---------------------------------------------------------------------------


class TestMigrationReversibilityAlembic:
    def _make_versions_dir(self, tmp_path: Path) -> Path:
        versions = tmp_path / "alembic" / "versions"
        versions.mkdir(parents=True)
        return versions

    def test_no_migrations_skips(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(_step("migration_reversibility"), tmp_path)
        assert result.status == "skipped"
        assert not result.blocked

    def test_all_reversible_passes(self, tmp_path: Path) -> None:
        versions = self._make_versions_dir(tmp_path)
        (versions / "001_init.py").write_text(
            "def upgrade():\n    op.create_table('t')\n\ndef downgrade():\n    op.drop_table('t')\n"
        )
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(_step("migration_reversibility"), tmp_path)
        assert result.status == "pass"
        assert not result.blocked
        assert result.metadata["migration_count"] == 1

    def test_missing_downgrade_fails(self, tmp_path: Path) -> None:
        versions = self._make_versions_dir(tmp_path)
        (versions / "001_init.py").write_text("def upgrade():\n    op.create_table('t')\n")
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(
            _step("migration_reversibility", required=True), tmp_path
        )
        assert result.status == "fail"
        assert result.blocked
        assert "missing downgrade()" in result.details

    def test_pass_only_downgrade_fails(self, tmp_path: Path) -> None:
        versions = self._make_versions_dir(tmp_path)
        (versions / "001_init.py").write_text(
            "def upgrade():\n    op.create_table('t')\n\ndef downgrade():\n    pass\n"
        )
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(
            _step("migration_reversibility", required=True), tmp_path
        )
        assert result.status == "fail"
        assert result.blocked
        assert "empty (pass-only)" in result.details

    def test_skips_dunder_init(self, tmp_path: Path) -> None:
        versions = self._make_versions_dir(tmp_path)
        (versions / "__init__.py").write_text("")
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(_step("migration_reversibility"), tmp_path)
        assert result.status == "skipped"

    def test_not_required_does_not_block(self, tmp_path: Path) -> None:
        versions = self._make_versions_dir(tmp_path)
        (versions / "001.py").write_text("def upgrade():\n    pass\n")
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(
            _step("migration_reversibility", required=False), tmp_path
        )
        assert result.status == "fail"
        assert not result.blocked


# ---------------------------------------------------------------------------
# migration_reversibility gate — SQL up/down pairs
# ---------------------------------------------------------------------------


class TestMigrationReversibilitySql:
    def test_matched_up_down_passes(self, tmp_path: Path) -> None:
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "001_up.sql").write_text("CREATE TABLE t (id INT);")
        (mig_dir / "001_down.sql").write_text("DROP TABLE t;")
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(_step("migration_reversibility"), tmp_path)
        assert result.status == "pass"

    def test_missing_down_sql_fails(self, tmp_path: Path) -> None:
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "001_up.sql").write_text("CREATE TABLE t (id INT);")
        runner = _runner(tmp_path)
        result = runner._run_migration_reversibility_gate_sync(
            _step("migration_reversibility", required=True), tmp_path
        )
        assert result.status == "fail"
        assert result.blocked
        assert "no matching down migration" in result.details


# ---------------------------------------------------------------------------
# large_file gate
# ---------------------------------------------------------------------------


class TestLargeFileGate:
    def test_no_changed_files_passes(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        result = runner._run_large_file_gate_sync(_step("large_file", required=False), tmp_path, [])
        assert result.status == "pass"
        assert not result.blocked

    def test_small_file_passes(self, tmp_path: Path) -> None:
        f = tmp_path / "small.py"
        f.write_text("\n".join(f"x = {i}" for i in range(100)))
        runner = _runner(tmp_path, large_file_threshold=500)
        result = runner._run_large_file_gate_sync(_step("large_file", required=False), tmp_path, ["small.py"])
        assert result.status == "pass"

    def test_oversized_file_warns(self, tmp_path: Path) -> None:
        f = tmp_path / "big.py"
        f.write_text("\n".join(f"x = {i}" for i in range(501)))
        runner = _runner(tmp_path, large_file_threshold=500)
        result = runner._run_large_file_gate_sync(_step("large_file", required=False), tmp_path, ["big.py"])
        assert result.status == "warn"
        assert not result.blocked
        assert "big.py" in result.details
        assert result.metadata["oversized_files"] == 1

    def test_exactly_threshold_passes(self, tmp_path: Path) -> None:
        f = tmp_path / "exact.py"
        f.write_text("\n".join(f"x = {i}" for i in range(500)))
        runner = _runner(tmp_path, large_file_threshold=500)
        result = runner._run_large_file_gate_sync(_step("large_file", required=False), tmp_path, ["exact.py"])
        assert result.status == "pass"

    def test_required_step_still_never_blocks(self, tmp_path: Path) -> None:
        """large_file never blocks even when required=True — it's a heuristic."""
        f = tmp_path / "huge.py"
        f.write_text("\n".join(f"x = {i}" for i in range(1000)))
        runner = _runner(tmp_path, large_file_threshold=500)
        result = runner._run_large_file_gate_sync(_step("large_file", required=True), tmp_path, ["huge.py"])
        assert result.status == "warn"
        assert not result.blocked

    def test_nonexistent_file_skipped_gracefully(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path, large_file_threshold=500)
        result = runner._run_large_file_gate_sync(_step("large_file", required=False), tmp_path, ["nonexistent.py"])
        assert result.status == "pass"

    def test_custom_threshold(self, tmp_path: Path) -> None:
        f = tmp_path / "medium.py"
        f.write_text("\n".join(f"x = {i}" for i in range(151)))
        runner = _runner(tmp_path, large_file_threshold=150)
        result = runner._run_large_file_gate_sync(_step("large_file", required=False), tmp_path, ["medium.py"])
        assert result.status == "warn"
        assert result.metadata["threshold"] == 150


# ---------------------------------------------------------------------------
# build_default_pipeline includes new gates when enabled
# ---------------------------------------------------------------------------


class TestBuildDefaultPipelineNewGates:
    def test_migration_reversibility_in_pipeline(self) -> None:
        config = QualityGatesConfig(migration_reversibility_check=True, large_file_check=False)
        pipeline = build_default_pipeline(config)
        names = [s.name for s in pipeline]
        assert "migration_reversibility" in names

    def test_large_file_in_pipeline(self) -> None:
        config = QualityGatesConfig(large_file_check=True, migration_reversibility_check=False)
        pipeline = build_default_pipeline(config)
        names = [s.name for s in pipeline]
        assert "large_file" in names

    def test_large_file_not_required_by_default(self) -> None:
        config = QualityGatesConfig(large_file_check=True)
        pipeline = build_default_pipeline(config)
        step = next(s for s in pipeline if s.name == "large_file")
        assert not step.required

    def test_migration_reversibility_required(self) -> None:
        config = QualityGatesConfig(migration_reversibility_check=True)
        pipeline = build_default_pipeline(config)
        step = next(s for s in pipeline if s.name == "migration_reversibility")
        assert step.required

    def test_neither_gate_in_pipeline_when_disabled(self) -> None:
        config = QualityGatesConfig(migration_reversibility_check=False, large_file_check=False)
        pipeline = build_default_pipeline(config)
        names = [s.name for s in pipeline]
        assert "migration_reversibility" not in names
        assert "large_file" not in names
