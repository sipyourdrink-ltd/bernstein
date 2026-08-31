from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from bernstein.core.planning.engagement_library import (
    EngagementLibrary,
    EngagementPhase,
    EngagementPlaybook,
    ScannerConfig,
    _load_playbook_file,
    _parse_phase,
    _parse_scanner_config,
    load_engagement_library,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# ScannerConfig tests
# ---------------------------------------------------------------------------


class TestScannerConfig:
    def test_scanner_config_has_adapter_and_config(self) -> None:
        config = ScannerConfig(adapter="test-adapter", config={"key": "value"})
        assert config.adapter == "test-adapter"
        assert config.config == {"key": "value"}

    def test_scanner_config_default_config_is_empty_dict(self) -> None:
        config = ScannerConfig(adapter="minimal")
        assert config.config == {}


# ---------------------------------------------------------------------------
# EngagementPhase tests
# ---------------------------------------------------------------------------


class TestEngagementPhase:
    def test_phase_has_all_fields(self) -> None:
        scanners = (ScannerConfig(adapter="scan1"),)
        phase = EngagementPhase(
            name="Scan",
            action="scanner",
            scanners=scanners,
            scope_ref="scope:repo",
            config={"threshold": 0.5},
        )
        assert phase.name == "Scan"
        assert phase.action == "scanner"
        assert phase.scanners == scanners
        assert phase.scope_ref == "scope:repo"
        assert phase.config == {"threshold": 0.5}

    def test_phase_default_config_is_empty_dict(self) -> None:
        phase = EngagementPhase(
            name="Verify",
            action="verify",
            scanners=(),
            scope_ref="scope:repo",
        )
        assert phase.config == {}


# ---------------------------------------------------------------------------
# EngagementPlaybook tests
# ---------------------------------------------------------------------------


class TestEngagementPlaybook:
    def test_playbook_has_all_fields(self) -> None:
        phases = (
            EngagementPhase(
                name="Scan",
                action="scanner",
                scanners=(ScannerConfig(adapter="test"),),
                scope_ref="scope:repo",
            ),
        )
        playbook = EngagementPlaybook(
            playbook_id="test-playbook",
            name="Test Playbook",
            description="A test playbook",
            version="1.0",
            tags=("security", "test"),
            scope_ref="scope:repo",
            phases=phases,
        )
        assert playbook.playbook_id == "test-playbook"
        assert playbook.name == "Test Playbook"
        assert playbook.description == "A test playbook"
        assert playbook.version == "1.0"
        assert playbook.tags == ("security", "test")
        assert playbook.scope_ref == "scope:repo"
        assert playbook.phases == phases


# ---------------------------------------------------------------------------
# EngagementLibrary tests
# ---------------------------------------------------------------------------


class TestEngagementLibrary:
    def test_get_returns_playbook(self) -> None:
        playbook = EngagementPlaybook(
            playbook_id="pb-1",
            name="Playbook 1",
            description="Desc",
            version="1.0",
            tags=(),
            scope_ref="scope:repo",
            phases=(),
        )
        library = EngagementLibrary(playbooks={"pb-1": playbook})
        assert library.get("pb-1") == playbook

    def test_get_returns_none_for_missing(self) -> None:
        library = EngagementLibrary(playbooks={})
        assert library.get("nonexistent") is None

    def test_get_empty_library_returns_empty_dict(self) -> None:
        library = EngagementLibrary(playbooks={})
        assert library.playbooks == {}


# ---------------------------------------------------------------------------
# load_engagement_library tests
# ---------------------------------------------------------------------------


class TestLoadEngagementLibrary:
    def test_root_does_not_exist_returns_empty_library(self) -> None:
        library = load_engagement_library(Path("/nonexistent/path/12345"))
        assert library.playbooks == {}

    def test_loads_yaml_from_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            yaml_content = {
                "id": "pb-1",
                "name": "Test Playbook",
                "description": "A description",
                "version": "1.0",
                "tags": ["test"],
                "scope_ref": "scope:repo",
                "phases": [
                    {
                        "name": "Scan",
                        "action": "scanner",
                        "scope_ref": "scope:repo",
                    }
                ],
            }
            (root / "playbooks").mkdir()
            (root / "playbooks" / "test.yaml").write_text(yaml.safe_dump(yaml_content))

            library = load_engagement_library(root)
            assert "pb-1" in library.playbooks

    def test_loads_yml_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            yaml_content = {
                "id": "pb-yml",
                "name": "YSML Playbook",
                "description": "Desc",
                "version": "1.0",
                "scope_ref": "scope:repo",
                "phases": [
                    {
                        "name": "Verify",
                        "action": "verify",
                        "scope_ref": "scope:repo",
                    }
                ],
            }
            (root / "playbooks").mkdir(parents=True)
            (root / "playbooks" / "test.yml").write_text(yaml.safe_dump(yaml_content))

            library = load_engagement_library(root)
            assert "pb-yml" in library.playbooks

    def test_multiple_yaml_files_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            yaml_content1 = {
                "id": "pb-1",
                "name": "Playbook 1",
                "description": "Desc 1",
                "version": "1.0",
                "scope_ref": "scope:repo1",
                "phases": [{"name": "Phase 1", "action": "recon", "scope_ref": "scope:repo1"}],
            }
            yaml_content2 = {
                "id": "pb-2",
                "name": "Playbook 2",
                "description": "Desc 2",
                "version": "2.0",
                "scope_ref": "scope:repo2",
                "phases": [{"name": "Phase 2", "action": "scan", "scope_ref": "scope:repo2"}],
            }
            (root / "pb1.yaml").write_text(yaml.safe_dump(yaml_content1))
            (root / "pb2.yaml").write_text(yaml.safe_dump(yaml_content2))

            library = load_engagement_library(root)
            assert "pb-1" in library.playbooks
            assert "pb-2" in library.playbooks


# ---------------------------------------------------------------------------
# _load_playbook_file tests
# ---------------------------------------------------------------------------


class TestLoadPlaybookFile:
    def test_handles_yamlexception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid.yaml"
            path.write_text("not: valid: yaml: : :")
            assert _load_playbook_file(path) is None

    def test_handles_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nonexistent.yaml"
            assert _load_playbook_file(path) is None

    def test_returns_none_if_not_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "list.yaml"
            path.write_text("- item1\n- item2\n")
            assert _load_playbook_file(path) is None

    def test_returns_none_if_playbook_id_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "no-id.yaml"
            yaml_content = {
                "id": "   ",
                "name": "Test",
                "scope_ref": "scope:repo",
                "phases": [{"name": "Phase", "action": "recon", "scope_ref": "scope:repo"}],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            assert _load_playbook_file(path) is None

    def test_returns_none_if_name_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "no-name.yaml"
            yaml_content = {
                "id": "pb-1",
                "name": "   ",
                "scope_ref": "scope:repo",
                "phases": [{"name": "Phase", "action": "recon", "scope_ref": "scope:repo"}],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            assert _load_playbook_file(path) is None

    def test_returns_none_if_phases_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "no-phases.yaml"
            yaml_content = {
                "id": "pb-1",
                "name": "Test",
                "scope_ref": "scope:repo",
                "phases": [],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            assert _load_playbook_file(path) is None

    def test_strips_whitespace_from_id_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "whitespace.yaml"
            yaml_content = {
                "id": "  pb-1  ",
                "name": "  Test Playbook  ",
                "scope_ref": "scope:repo",
                "phases": [{"name": "Phase", "action": "recon", "scope_ref": "scope:repo"}],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            result = _load_playbook_file(path)
            assert result is not None
            assert result.playbook_id == "pb-1"
            assert result.name == "Test Playbook"

    def test_parses_tags_from_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "with-tags.yaml"
            yaml_content = {
                "id": "pb-1",
                "name": "Test",
                "scope_ref": "scope:repo",
                "tags": ["  tag1  ", "tag2", ""],
                "phases": [{"name": "Phase", "action": "recon", "scope_ref": "scope:repo"}],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            result = _load_playbook_file(path)
            assert result is not None
            assert result.tags == ("tag1", "tag2")

    def test_tags_empty_if_not_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "notags.yaml"
            yaml_content = {
                "id": "pb-1",
                "name": "Test",
                "scope_ref": "scope:repo",
                "tags": "not-a-list",
                "phases": [{"name": "Phase", "action": "recon", "scope_ref": "scope:repo"}],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            result = _load_playbook_file(path)
            assert result is not None
            assert result.tags == ()

    def test_parses_version_or_defaults_to_10(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "no-version.yaml"
            yaml_content = {
                "id": "pb-1",
                "name": "Test",
                "scope_ref": "scope:repo",
                "phases": [{"name": "Phase", "action": "recon", "scope_ref": "scope:repo"}],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            result = _load_playbook_file(path)
            assert result is not None
            assert result.version == "1.0"

    def test_parses_config_for_scanner_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "scanner-phase.yaml"
            yaml_content = {
                "id": "pb-1",
                "name": "Test",
                "scope_ref": "scope:repo",
                "phases": [
                    {
                        "name": "Scan",
                        "action": "scanner",
                        "scope_ref": "scope:repo",
                        "config": {"threshold": 0.5},
                        "scanners": [{"adapter": "test-adapter", "config": {"opt": 1}}],
                    }
                ],
            }
            path.write_text(yaml.safe_dump(yaml_content))
            result = _load_playbook_file(path)
            assert result is not None
            assert result.phases[0].config == {"threshold": 0.5}
            assert len(result.phases[0].scanners) == 1
            assert result.phases[0].scanners[0].adapter == "test-adapter"


# ---------------------------------------------------------------------------
# _parse_phase tests
# ---------------------------------------------------------------------------


class TestParsePhase:
    def test_returns_none_if_not_dict(self) -> None:
        assert _parse_phase("not a dict") is None

    def test_returns_none_if_name_empty(self) -> None:
        assert _parse_phase({"name": "   ", "action": "recon", "scope_ref": "scope:repo"}) is None

    def test_returns_none_if_action_empty(self) -> None:
        assert _parse_phase({"name": "Scan", "action": "   ", "scope_ref": "scope:repo"}) is None

    def test_returns_none_if_scope_ref_empty(self) -> None:
        assert _parse_phase({"name": "Scan", "action": "recon", "scope_ref": "   "}) is None

    def test_parses_verification_phase_without_scanners(self) -> None:
        result = _parse_phase(
            {
                "name": "Verify",
                "action": "verify",
                "scope_ref": "scope:repo",
            }
        )
        assert result is not None
        assert result.name == "Verify"
        assert result.action == "verify"
        assert result.scanners == ()

    def test_parses_scanners_for_scanner_action(self) -> None:
        result = _parse_phase(
            {
                "name": "Scan",
                "action": "scanner",
                "scope_ref": "scope:repo",
                "scanners": [
                    {"adapter": "adapter1"},
                    {"adapter": "adapter2", "config": {"opt": 1}},
                ],
            }
        )
        assert result is not None
        assert len(result.scanners) == 2
        assert result.scanners[0].adapter == "adapter1"
        assert result.scanners[0].config == {}
        assert result.scanners[1].adapter == "adapter2"
        assert result.scanners[1].config == {"opt": 1}

    def test_ignores_invalid_scanner_configs(self) -> None:
        result = _parse_phase(
            {
                "name": "Scan",
                "action": "scanner",
                "scope_ref": "scope:repo",
                "scanners": [
                    {"adapter": "valid"},
                    {"adapter": ""},  # Invalid: empty adapter
                    "not a dict",  # Invalid: not a dict
                ],
            }
        )
        assert result is not None
        assert len(result.scanners) == 1
        assert result.scanners[0].adapter == "valid"

    def test_parses_config(self) -> None:
        result = _parse_phase(
            {
                "name": "Scan",
                "action": "scanner",
                "scope_ref": "scope:repo",
                "config": {"key": "value"},
            }
        )
        assert result is not None
        assert result.config == {"key": "value"}

    def test_config_empty_if_not_dict(self) -> None:
        result = _parse_phase(
            {
                "name": "Scan",
                "action": "scanner",
                "scope_ref": "scope:repo",
                "config": "not-a-dict",
            }
        )
        assert result is not None
        assert result.config == {}


# ---------------------------------------------------------------------------
# _parse_scanner_config tests
# ---------------------------------------------------------------------------


class TestParseScannerConfig:
    def test_returns_none_if_not_dict(self) -> None:
        assert _parse_scanner_config("not a dict") is None

    def test_returns_none_if_adapter_empty(self) -> None:
        assert _parse_scanner_config({"adapter": "   "}) is None

    def test_parses_adapter_and_config(self) -> None:
        result = _parse_scanner_config({"adapter": "test-adapter", "config": {"opt": 1}})
        assert result is not None
        assert result.adapter == "test-adapter"
        assert result.config == {"opt": 1}

    def test_config_empty_if_missing(self) -> None:
        result = _parse_scanner_config({"adapter": "minimal"})
        assert result is not None
        assert result.config == {}

    def test_config_empty_if_not_dict(self) -> None:
        result = _parse_scanner_config({"adapter": "test", "config": "not-a-dict"})
        assert result is not None
        assert result.config == {}

    def test_strips_whitespace_from_adapter(self) -> None:
        result = _parse_scanner_config({"adapter": "  stripped  "})
        assert result is not None
        assert result.adapter == "stripped"
