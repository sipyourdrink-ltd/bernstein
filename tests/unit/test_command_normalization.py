"""Tests for SEC-001: command normalization against evasion in auto_approve.py."""

from __future__ import annotations

from bernstein.core.auto_approve import (
    Decision,
    classify_command,
    normalize_command,
)

# ---------------------------------------------------------------------------
# normalize_command unit tests
# ---------------------------------------------------------------------------


class TestNormalizeCommand:
    """Test the normalization layer that defeats evasion techniques."""

    def test_passthrough_simple_command(self) -> None:
        assert normalize_command("ls -la") == "ls -la"

    def test_strip_zero_width_spaces(self) -> None:
        # Zero-width space between r and m
        result = normalize_command("r\u200bm -rf /")
        assert "rm" in result

    def test_fullwidth_latin_homoglyphs(self) -> None:
        # Fullwidth 'r' (\uff52) and 'm' (\uff4d)
        result = normalize_command("\uff52\uff4d -rf /tmp")
        assert "rm" in result

    def test_cyrillic_homoglyphs(self) -> None:
        # Cyrillic 'с' (\u0441) looks like latin 'c'
        result = normalize_command("\u0441at /etc/passwd")
        assert "cat" in result

    def test_hex_escape_sequences(self) -> None:
        # \x72\x6d = rm
        result = normalize_command("\\x72\\x6d -rf /tmp")
        assert "rm" in result

    def test_octal_escape_sequences(self) -> None:
        # \162\155 = rm
        result = normalize_command("\\162\\155 -rf /tmp")
        assert "rm" in result

    def test_ansi_c_quoting(self) -> None:
        # $'\x72\x6d' = rm
        result = normalize_command("$'\\x72\\x6d' -rf /tmp")
        assert "rm" in result

    def test_backtick_substitution_extracted(self) -> None:
        # `echo rm` should expose "rm" for pattern matching
        result = normalize_command("`echo rm` -rf /")
        assert "rm" in result
        assert "echo rm" in result

    def test_dollar_paren_substitution_extracted(self) -> None:
        # $(echo rm) should expose "rm" for pattern matching
        result = normalize_command("$(echo rm) -rf /")
        assert "rm" in result
        assert "echo rm" in result

    def test_env_var_expansion(self) -> None:
        # ${HOME} → HOME
        result = normalize_command("cat ${HOME}/.bashrc")
        assert "HOME" in result
        assert "${" not in result

    def test_whitespace_normalization(self) -> None:
        result = normalize_command("rm   -rf    /tmp")
        assert result == "rm -rf /tmp"

    def test_combined_evasion(self) -> None:
        # Multiple evasion techniques combined
        result = normalize_command("$'\\x72\\x6d' \u200b-rf /")
        assert "rm" in result


# ---------------------------------------------------------------------------
# Classify command with evasion detection
# ---------------------------------------------------------------------------


class TestClassifyCommandEvasion:
    """Test that classify_command catches evasion attempts."""

    def test_normal_rm_rf_denied(self) -> None:
        result = classify_command("rm -rf /tmp")
        assert result.decision == Decision.DENY

    def test_hex_encoded_rm_denied(self) -> None:
        result = classify_command("$'\\x72\\x6d' -rf /tmp")
        assert result.decision == Decision.DENY

    def test_backtick_echo_rm_not_allowed(self) -> None:
        result = classify_command("`echo rm` -rf /")
        assert result.decision != Decision.APPROVE

    def test_dollar_paren_echo_rm_not_allowed(self) -> None:
        result = classify_command("$(echo rm) -rf /")
        assert result.decision != Decision.APPROVE

    def test_zero_width_space_evasion_not_allowed(self) -> None:
        result = classify_command("r\u200bm -rf /tmp")
        assert result.decision != Decision.APPROVE

    def test_fullwidth_rm_not_allowed(self) -> None:
        result = classify_command("\uff52\uff4d -rf /tmp")
        assert result.decision != Decision.APPROVE

    def test_base64_pipe_evasion_not_allowed(self) -> None:
        result = classify_command("echo cm0gLXJmIC8= | base64 -d | sh")
        assert result.decision != Decision.APPROVE

    def test_safe_command_still_approved(self) -> None:
        result = classify_command("ls -la")
        assert result.decision == Decision.APPROVE

    def test_git_status_still_approved(self) -> None:
        result = classify_command("git status")
        assert result.decision == Decision.APPROVE

    def test_sudo_with_homoglyph_not_allowed(self) -> None:
        # Fullwidth 's' (\uff53) + 'u' (\uff55) + 'd' (\uff44) + 'o' (\uff4f)
        result = classify_command("\uff53\uff55\uff44\uff4f apt install malware")
        assert result.decision != Decision.APPROVE
