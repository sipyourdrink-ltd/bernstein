"""Tests for ``govern discover --assist`` (issue #5020)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import (
    _build_findings_from_inventory,
    _build_playbook_prompt,
    _connector_configured,
    _parse_playbook_json,
    governance_group,
)


class TestConnectorConfigured:
    """Tests for _connector_configured()."""

    def test_returns_false_when_no_seed_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When no bernstein.yaml exists, no connector is configured."""
        monkeypatch.chdir(tmp_path)
        assert _connector_configured() is False

    def test_returns_false_when_provider_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When internal_llm_provider is 'none', connector is not configured."""
        monkeypatch.chdir(tmp_path)
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\ninternal_llm_provider: none\n", encoding="utf-8")
        monkeypatch.setenv("BERNSTEIN_SEED", str(seed_file))
        assert _connector_configured() is False

    def test_returns_true_when_provider_is_configured(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When internal_llm_provider is set to a real provider, connector is configured."""
        monkeypatch.chdir(tmp_path)
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\ninternal_llm_provider: openrouter_free\n", encoding="utf-8")
        monkeypatch.setenv("BERNSTEIN_SEED", str(seed_file))
        assert _connector_configured() is True


class TestBuildFindingsFromInventory:
    """Tests for _build_findings_from_inventory()."""

    def test_empty_inventory(self) -> None:
        """An empty inventory produces an empty findings document."""
        inventory: dict[str, object] = {"surfaces": []}
        result = _build_findings_from_inventory(inventory, "sha256:abc123", 1234567890)
        assert result["inventory_hash"] == "sha256:abc123"
        assert result["timestamp"] == 1234567890
        assert result["findings"] == []

    def test_single_surface(self) -> None:
        """A single surface produces a single finding with readable=True."""
        inventory: dict[str, object] = {
            "surfaces": [
                {
                    "surface": "git:remote:origin",
                    "observed_value": "https://github.com/example/repo",
                    "evidence_ref": "git-remote:origin",
                }
            ]
        }
        result = _build_findings_from_inventory(inventory, "sha256:abc123", 1234567890)
        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0]["surface"] == "git:remote:origin"
        assert findings[0]["observed_value"] == "https://github.com/example/repo"
        assert findings[0]["readable"] is True

    def test_unreadable_surface(self) -> None:
        """A surface with empty observed_value is marked unreadable."""
        inventory: dict[str, object] = {
            "surfaces": [{"surface": "env:MISSING_VAR", "observed_value": "", "evidence_ref": "env-var:MISSING_VAR"}]
        }
        result = _build_findings_from_inventory(inventory, "sha256:abc123", 1234567890)
        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0]["readable"] is False

    def test_multiple_surfaces(self) -> None:
        """Multiple surfaces produce multiple findings."""
        inventory: dict[str, object] = {
            "surfaces": [
                {
                    "surface": "git:remote:origin",
                    "observed_value": "https://github.com/example/repo",
                    "evidence_ref": "ref1",
                },
                {"surface": "env:BERNSTEIN_AUDIT_KEY", "observed_value": "set", "evidence_ref": "ref2"},
            ]
        }
        result = _build_findings_from_inventory(inventory, "sha256:xyz789", 1000000000)
        findings = result["findings"]
        assert len(findings) == 2
        assert findings[0]["surface"] == "git:remote:origin"
        assert findings[1]["surface"] == "env:BERNSTEIN_AUDIT_KEY"


class TestBuildPlaybookPrompt:
    """Tests for _build_playbook_prompt()."""

    def test_prompt_contains_findings(self) -> None:
        """Prompt includes findings in the output."""
        findings_dict: dict[str, object] = {
            "findings": [
                {
                    "surface": "git:remote:origin",
                    "observed_value": "https://github.com/example/repo",
                    "readable": True,
                    "evidence_ref": "ref1",
                }
            ]
        }
        prompt = _build_playbook_prompt(findings_dict, None)
        assert "git:remote:origin" in prompt
        assert "https://github.com/example/repo" in prompt
        assert "readable" in prompt

    def test_prompt_contains_unreadable_marker(self) -> None:
        """Unreadable surfaces are marked as UNREADABLE in the prompt."""
        findings_dict: dict[str, object] = {
            "findings": [{"surface": "env:MISSING", "observed_value": "", "readable": False, "evidence_ref": "ref1"}]
        }
        prompt = _build_playbook_prompt(findings_dict, None)
        assert "UNREADABLE" in prompt

    def test_prompt_contains_seed(self) -> None:
        """Prompt includes the seed when provided."""
        findings_dict: dict[str, object] = {"findings": []}
        prompt = _build_playbook_prompt(findings_dict, "my-seed-123")
        assert "my-seed-123" in prompt
        assert "Seed for reproducibility" in prompt

    def test_prompt_excludes_seed_when_none(self) -> None:
        """Prompt does not include seed line when seed is None."""
        findings_dict: dict[str, object] = {"findings": []}
        prompt = _build_playbook_prompt(findings_dict, None)
        assert "Seed for reproducibility" not in prompt


class TestParsePlaybookJson:
    """Tests for _parse_playbook_json()."""

    def test_parses_valid_json(self) -> None:
        """A valid JSON playbook is returned as a dict."""
        raw = '{"forbidden": [], "required": [], "permitted": []}'
        result = _parse_playbook_json(raw)
        assert result == {"forbidden": [], "required": [], "permitted": []}

    def test_strips_code_fences(self) -> None:
        """Markdown code fences are stripped before parsing."""
        raw = '```json\n{"forbidden": [], "required": [], "permitted": []}\n```'
        result = _parse_playbook_json(raw)
        assert result == {"forbidden": [], "required": [], "permitted": []}

    def test_strips_partial_code_fences(self) -> None:
        """Even partial code fences are stripped."""
        raw = '```\n{"forbidden": [], "required": [], "permitted": []}\n```'
        result = _parse_playbook_json(raw)
        assert result == {"forbidden": [], "required": [], "permitted": []}

    def test_raises_on_invalid_json(self) -> None:
        """Invalid JSON raises ValueError."""
        raw = "not valid json at all"
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_playbook_json(raw)

    def test_raises_on_non_object(self) -> None:
        """A JSON array or string raises ValueError."""
        raw = '["not", "an", "object"]'
        with pytest.raises(ValueError, match="JSON object"):
            _parse_playbook_json(raw)


class TestGovernDiscoverCli:
    """End-to-end CLI tests for ``govern discover``."""

    def test_discover_no_connector_emits_findings_and_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no connector is configured, findings are written and exit is 0."""
        monkeypatch.chdir(tmp_path)

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\ninternal_llm_provider: none\n", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            governance_group,
            ["discover", "--workdir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output

        output_dir = tmp_path / ".sdd" / "govern"
        assert output_dir.exists()

        findings_files = list(output_dir.glob("findings-*.json"))
        assert len(findings_files) == 1

        with findings_files[0].open(encoding="utf-8") as fh:
            findings = json.load(fh)

        assert "findings" in findings
        assert "inventory_hash" in findings
        assert "timestamp" in findings

    def test_discover_with_inventory_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When --inventory is provided, it is used instead of discovery."""
        monkeypatch.chdir(tmp_path)

        inventory_file = tmp_path / "inventory.json"
        inventory_file.write_text(
            json.dumps(
                {
                    "surfaces": [
                        {
                            "surface": "git:remote:origin",
                            "observed_value": "https://github.com/test/repo",
                            "evidence_ref": "ref1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\ninternal_llm_provider: none\n", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            governance_group,
            ["discover", "--inventory", str(inventory_file), "--workdir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output

        output_dir = tmp_path / ".sdd" / "govern"
        findings_files = list(output_dir.glob("findings-*.json"))
        assert len(findings_files) == 1

        with findings_files[0].open(encoding="utf-8") as fh:
            findings = json.load(fh)

        assert findings["findings"][0]["surface"] == "git:remote:origin"

    def test_findings_document_is_content_addressed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The findings file name contains the content hash prefix."""
        monkeypatch.chdir(tmp_path)

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\ninternal_llm_provider: none\n", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            governance_group,
            ["discover", "--workdir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output

        output_dir = tmp_path / ".sdd" / "govern"
        findings_files = list(output_dir.glob("findings-*.json"))
        assert len(findings_files) == 1

        filename = findings_files[0].name
        assert filename.startswith("findings-")
        assert len(filename) > len("findings-")

    def test_malformed_model_output_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When connector is configured but model output is malformed, exit is 1."""
        monkeypatch.chdir(tmp_path)

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\ninternal_llm_provider: openrouter_free\n", encoding="utf-8")

        monkeypatch.setenv("OPENROUTER_API_KEY_FREE", "fake-key")

        mock_response = "This is not JSON output at all"

        import bernstein.core.llm as llm_module

        async def fake_call_llm(*args: object, **kwargs: object) -> str:
            return mock_response

        monkeypatch.setattr(llm_module, "call_llm", fake_call_llm)

        runner = CliRunner()
        result = runner.invoke(
            governance_group,
            ["discover", "--workdir", str(tmp_path)],
        )

        assert result.exit_code == 1, result.output
        assert "parse failed" in result.output.lower() or "Model output" in result.output

    def test_same_findings_and_seed_produce_recorded_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two runs with same inventory and same seed produce the same prompt content."""
        monkeypatch.chdir(tmp_path)

        inventory_file = tmp_path / "inventory.json"
        inventory_file.write_text(
            json.dumps(
                {
                    "surfaces": [
                        {
                            "surface": "git:remote:origin",
                            "observed_value": "https://github.com/test/repo",
                            "evidence_ref": "ref1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\ninternal_llm_provider: openrouter_free\n", encoding="utf-8")

        monkeypatch.setenv("OPENROUTER_API_KEY_FREE", "fake-key-for-test")

        captured_prompts: list[str] = []

        async def capturing_call_llm(*args: object, **kwargs: object) -> str:
            prompt = str(args[0]) if args else str(kwargs.get("prompt", ""))
            captured_prompts.append(prompt)
            return json.dumps({"forbidden": [], "required": [], "permitted": []})

        import bernstein.core.llm as llm_module

        monkeypatch.setattr(llm_module, "call_llm", capturing_call_llm)

        seed_content = "my-reproducibility-seed"

        output_dir1 = tmp_path / "run1"
        output_dir2 = tmp_path / "run2"
        output_dir1.mkdir()
        output_dir2.mkdir()

        runner = CliRunner()

        result1 = runner.invoke(
            governance_group,
            [
                "discover",
                "--inventory",
                str(inventory_file),
                "--output-dir",
                str(output_dir1),
                "--seed",
                seed_content,
                "--workdir",
                str(tmp_path),
            ],
        )
        assert result1.exit_code == 0, result1.output

        result2 = runner.invoke(
            governance_group,
            [
                "discover",
                "--inventory",
                str(inventory_file),
                "--output-dir",
                str(output_dir2),
                "--seed",
                seed_content,
                "--workdir",
                str(tmp_path),
            ],
        )
        assert result2.exit_code == 0, result2.output

        assert len(captured_prompts) == 2
        assert captured_prompts[0] == captured_prompts[1]
        assert seed_content in captured_prompts[0]
