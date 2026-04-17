"""Tests for always-allow rules with content matching (T469)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.always_allow import (
    AlwaysAllowEngine,
    AlwaysAllowRule,
    AlwaysAllowTamperError,
    check_always_allow,
    load_always_allow_rules,
    write_always_allow_manifest,
)
from bernstein.core.guardrails import (
    _check_always_allow_for_diff,
    check_always_allow_tool,
)

# ---------------------------------------------------------------------------
# Rule/pattern matching
# ---------------------------------------------------------------------------


class TestAlwaysAllowMatch:
    """Test rule matching with various input patterns."""

    def test_exact_tool_match(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="test-grep-src",
                    tool="grep",
                    input_pattern="src/.*",
                ),
            ],
        )
        result = check_always_allow("grep", "src/foo/bar.py", engine)
        assert result.matched
        assert result.rule_id == "test-grep-src"

    def test_case_insensitive_tool(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="test-grep",
                    tool="grep",
                    input_pattern="src/.*",
                ),
            ],
        )
        result = check_always_allow("GREP", "src/foo.py", engine)
        assert result.matched

    def test_no_match_wrong_tool(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="test-grep",
                    tool="grep",
                    input_pattern="src/.*",
                ),
            ],
        )
        result = check_always_allow("bash", "/etc/passwd", engine)
        assert not result.matched

    def test_no_match_wrong_path(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="test-grep-src",
                    tool="grep",
                    input_pattern="src/.*",
                ),
            ],
        )
        result = check_always_allow("grep", "/etc/shadow", engine)
        assert not result.matched

    def test_glob_pattern_match(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="test-read-tests",
                    tool="read_file",
                    input_pattern="tests/*",
                ),
            ],
        )
        result = check_always_allow("read_file", "tests/test_foo.py", engine)
        assert result.matched

    def test_glob_pattern_no_match(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="test-read-tests",
                    tool="read_file",
                    input_pattern="tests/*",
                ),
            ],
        )
        result = check_always_allow("read_file", "src/foo.py", engine)
        assert not result.matched

    def test_custom_input_field(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="test-bash-src",
                    tool="bash",
                    input_pattern="pytest.*",
                    input_field="command",
                ),
            ],
        )
        result = check_always_allow("bash", "pytest tests/", engine, input_field="command")
        assert result.matched

    def test_description_in_reason(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="safe-grep",
                    tool="grep",
                    input_pattern="src/.*",
                    description="grep on source files is safe",
                ),
            ],
        )
        result = check_always_allow("grep", "src/main.py", engine)
        assert "safe-grep" in result.reason
        assert "grep on source files is safe" in result.reason

    def test_multiple_rules_first_match_wins(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(id="rule-1", tool="grep", input_pattern="src/.*"),
                AlwaysAllowRule(id="rule-2", tool="grep", input_pattern="src/main.*"),
            ],
        )
        result = check_always_allow("grep", "src/main.py", engine)
        assert result.matched
        assert result.rule_id == "rule-1"

    def test_empty_engine_no_match(self) -> None:
        engine = AlwaysAllowEngine(rules=[])
        result = check_always_allow("grep", "src/file.py", engine)
        assert not result.matched


class TestPatternMatching:
    """Test the _pattern_matches helper."""

    def test_regex_dot_star(self) -> None:
        from bernstein.core.always_allow import _pattern_matches

        assert _pattern_matches("src/.*", "src/foo/bar.py")
        assert not _pattern_matches("src/.*", "lib/foo.py")

    def test_regex_anchors(self) -> None:
        from bernstein.core.always_allow import _pattern_matches

        assert _pattern_matches("^tests/.*", "tests/test_a.py")
        assert not _pattern_matches("^tests/.*", "src/tests/file.py")

    def test_glob_star(self) -> None:
        from bernstein.core.always_allow import _pattern_matches

        assert _pattern_matches("docs/*", "docs/README.md")
        assert not _pattern_matches("docs/*", "src/docs/file.py")

    def test_invalid_regex_falls_back_to_glob(self) -> None:
        from bernstein.core.always_allow import _pattern_matches

        # "[test" without closing "]" is invalid regex — should fall back to glob
        assert _pattern_matches("src/[test", "src/[test")  # glob literal match after fallback
        assert not _pattern_matches("src/[test", "src/any")


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------


def _write_trusted_rules(workdir: Path, body: str) -> Path:
    """Write *body* to the trusted orchestrator-only rules path."""
    trusted_dir = workdir / ".sdd" / "config"
    trusted_dir.mkdir(parents=True, exist_ok=True)
    path = trusted_dir / "always_allow.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoadAlwaysAllowRules:
    def test_load_from_trusted_sdd_location(self, tmp_path: Path) -> None:
        _write_trusted_rules(
            tmp_path,
            """
- id: safe-grep
  tool: grep
  input_pattern: src/.*
  description: Grep on source is safe
""",
        )
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "safe-grep"

    def test_load_from_legacy_rules_yaml_with_manifest(self, tmp_path: Path) -> None:
        bernstein = tmp_path / ".bernstein"
        bernstein.mkdir()
        rules = bernstein / "rules.yaml"
        rules.write_text(
            """
always_allow:
  - id: safe-bash
    tool: bash
    input_pattern: pytest.*
""",
            encoding="utf-8",
        )
        # Orchestrator signs the legacy file so the loader trusts it.
        write_always_allow_manifest(tmp_path, rules)
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 1
        assert engine.rules[0].tool == "bash"

    def test_load_missing_config_returns_empty_engine(self, tmp_path: Path) -> None:
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 0

    def test_load_skips_entries_without_tool(self, tmp_path: Path) -> None:
        _write_trusted_rules(
            tmp_path,
            """
- input_pattern: src/.*
- tool: grep
  input_pattern: .*
""",
        )
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 1
        assert engine.rules[0].tool == "grep"

    def test_load_skips_entries_without_pattern(self, tmp_path: Path) -> None:
        _write_trusted_rules(
            tmp_path,
            """
- tool: grep
""",
        )
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 0


class TestAlwaysAllowTamperDetection:
    """Security regression tests for audit-046 (agent-writable rules file)."""

    def _write_agent_writable(self, workdir: Path, body: str) -> Path:
        """Write rules into the legacy agent-writable ``.bernstein/`` path."""
        bern = workdir / ".bernstein"
        bern.mkdir(parents=True, exist_ok=True)
        path = bern / "always_allow.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_agent_writable_without_manifest_is_rejected(self, tmp_path: Path) -> None:
        """A `.bernstein/always_allow.yaml` with no manifest MUST NOT load."""
        self._write_agent_writable(
            tmp_path,
            "- id: evil\n  tool: '*'\n  input_pattern: '**'\n",
        )
        with pytest.raises(AlwaysAllowTamperError):
            load_always_allow_rules(tmp_path)

    def test_agent_writable_without_manifest_strict_false_returns_empty(self, tmp_path: Path) -> None:
        """``strict=False`` swallows the error and returns an empty engine."""
        self._write_agent_writable(
            tmp_path,
            "- id: evil\n  tool: '*'\n  input_pattern: '**'\n",
        )
        engine = load_always_allow_rules(tmp_path, strict=False)
        assert engine.rules == []

    def test_tampered_rules_file_rejected(self, tmp_path: Path) -> None:
        """Post-manifest modification of the rules file must fail verification."""
        rules = self._write_agent_writable(
            tmp_path,
            "- id: safe\n  tool: grep\n  input_pattern: src/.*\n",
        )
        write_always_allow_manifest(tmp_path, rules)

        # Agent tampers with the file AFTER the orchestrator signed it.
        rules.write_text(
            "- id: evil\n  tool: '*'\n  input_pattern: '**'\n",
            encoding="utf-8",
        )

        with pytest.raises(AlwaysAllowTamperError):
            load_always_allow_rules(tmp_path)

    def test_trusted_sdd_location_accepted_without_manifest(self, tmp_path: Path) -> None:
        """Good path: rules inside `.sdd/config/` need no manifest."""
        _write_trusted_rules(
            tmp_path,
            "- id: safe-grep\n  tool: grep\n  input_pattern: src/.*\n",
        )
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 1

    def test_tamper_event_emitted_to_guardrails_jsonl(self, tmp_path: Path) -> None:
        """Tamper attempts are recorded to the guardrails metrics log."""
        self._write_agent_writable(
            tmp_path,
            "- id: evil\n  tool: '*'\n  input_pattern: '**'\n",
        )
        with pytest.raises(AlwaysAllowTamperError):
            load_always_allow_rules(tmp_path)

        metrics = tmp_path / ".sdd" / "metrics" / "guardrails.jsonl"
        assert metrics.exists(), "Expected tamper event to be recorded"
        events = [json.loads(line) for line in metrics.read_text().splitlines() if line]
        assert any(e.get("check") == "always_allow_tamper" for e in events)
        assert any(e.get("result") == "blocked" for e in events)

    def test_manifest_with_mismatched_sha_rejected(self, tmp_path: Path) -> None:
        """Manually-forged manifest with wrong digest is rejected."""
        rules = self._write_agent_writable(
            tmp_path,
            "- id: safe\n  tool: grep\n  input_pattern: src/.*\n",
        )
        manifest = tmp_path / ".sdd" / "config" / "always_allow.manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"version": 1, "path": str(rules), "sha256": "deadbeef" * 8}),
            encoding="utf-8",
        )
        with pytest.raises(AlwaysAllowTamperError):
            load_always_allow_rules(tmp_path)


# ---------------------------------------------------------------------------
# Guardrails integration
# ---------------------------------------------------------------------------


class TestAlwaysAllowGuardrailsIntegration:
    def test_check_always_allow_for_diff_matches_src_files(self) -> None:
        """Files under src/ matched by rule get always-allow decision."""
        diff = "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(id="safe-src", tool="write_file", input_pattern="src/.*"),
            ],
        )
        decisions = _check_always_allow_for_diff(diff, engine)
        assert len(decisions) == 1
        assert "Always-allowed" in decisions[0].reason
        assert "src/foo.py" in decisions[0].reason

    def test_check_always_allow_for_diff_no_match(self) -> None:
        """Files NOT matching rules get neutral ALLOW."""
        diff = "diff --git a/etc/shadow b/etc/shadow\n--- /dev/null\n+++ b/etc/shadow\n@@ -0,0 +1 @@\n+secret\n"
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(id="safe-src", tool="write_file", input_pattern="src/.*"),
            ],
        )
        decisions = _check_always_allow_for_diff(diff, engine)
        assert len(decisions) == 1
        assert "No always-allow matches" in decisions[0].reason

    def test_check_always_allow_tool_live_invocation(self) -> None:
        """check_always_allow_tool matches tool args during execution."""
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="safe-grep",
                    tool="grep",
                    input_pattern="src/.*",
                    input_field="path",
                ),
            ],
        )
        result = check_always_allow_tool(
            "grep",
            {"path": "src/auth.py", "pattern": "def login"},
            engine,
        )
        assert result.matched

    def test_check_always_allow_tool_no_match(self) -> None:
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="safe-grep",
                    tool="grep",
                    input_pattern="src/.*",
                    input_field="path",
                ),
            ],
        )
        result = check_always_allow_tool(
            "grep",
            {"path": "/etc/passwd", "pattern": "root"},
            engine,
        )
        assert not result.matched

    def test_safe_grep_allowed_unsafe_denied(self) -> None:
        """Always-allow rule matches grep on src/ but NOT on /etc."""
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="safe-grep",
                    tool="grep",
                    input_pattern="src/.*",
                    description="grep only on src/",
                ),
            ],
        )

        # Safe grep → allowed
        safe_result = check_always_allow("grep", "src/foo.py", engine)
        assert safe_result.matched

        # Dangerous grep → not matched (falls to ask/deny below)
        unsafe_result = check_always_allow("grep", "/etc/shadow", engine)
        assert not unsafe_result.matched


# ---------------------------------------------------------------------------
# Content pattern matching (T469) content matching
# ---------------------------------------------------------------------------


class TestContentPatternMatching:
    """Test content_patterns field on always-allow rules."""

    def test_content_patterns_all_match(self) -> None:
        """Rule fires when input_pattern AND all content_patterns match."""
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="grep-src-only",
                    tool="grep",
                    input_pattern="src/.*",
                    content_patterns=["--include=*.py", "--recursive"],
                    description="Recursive python grep on src only",
                ),
            ],
        )
        result = check_always_allow(
            "grep",
            "src/foo.py",
            engine,
            full_content="grep --include=*.py --recursive src/foo.py",
        )
        assert result.matched
        assert result.rule_id == "grep-src-only"

    def test_content_patterns_one_fails(self) -> None:
        """Rule does NOT fire when a content_pattern doesn't match."""
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="grep-src-only",
                    tool="grep",
                    input_pattern="src/.*",
                    content_patterns=["--include=*.py"],
                    description="Py only grep",
                ),
            ],
        )
        result = check_always_allow(
            "grep",
            "src/foo.py",
            engine,
            full_content="grep --include=*.txt src/foo.py",
        )
        assert not result.matched

    def test_content_patterns_without_full_content_falls_back(self) -> None:
        """When full_content is None, content patterns match against input_value."""
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="src-rule",
                    tool="read_file",
                    input_pattern="src/.*",
                    content_patterns=["src/"],
                    description="Read src files",
                ),
            ],
        )
        # Without full_content, content_patterns check against input_value
        result = check_always_allow("read_file", "src/main.py", engine)
        assert result.matched

    def test_content_patterns_load_from_yaml(self, tmp_path: Path) -> None:
        """content_patterns loaded from YAML file."""
        _write_trusted_rules(
            tmp_path,
            """
- id: safe-grep-src
  tool: grep
  input_pattern: src/.*
  content_patterns:
    - "src/"
    - "--include"
  description: Safe grep on source
""",
        )
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 1
        rule = engine.rules[0]
        assert rule.content_patterns == ["src/", "--include"]

    def test_content_patterns_empty_when_not_specified(self, tmp_path: Path) -> None:
        """content_patterns defaults to empty list when absent."""
        _write_trusted_rules(
            tmp_path,
            """
- id: simple-rule
  tool: grep
  input_pattern: src/.*
""",
        )
        engine = load_always_allow_rules(tmp_path)
        assert len(engine.rules) == 1
        assert engine.rules[0].content_patterns == []

    def test_guardrails_check_always_allow_tool_with_content(self) -> None:
        """Integration: check_always_allow_tool builds full_content from args."""
        engine = AlwaysAllowEngine(
            rules=[
                AlwaysAllowRule(
                    id="safe-grep-content",
                    tool="grep",
                    input_pattern="src/.*",
                    input_field="path",
                    content_patterns=["--include"],
                    description="Content-checked grep",
                ),
            ],
        )
        result = check_always_allow_tool(
            "grep",
            {"path": "src/auth.py", "command": "grep --include *.py -r src/"},
            engine,
        )
        assert result.matched
