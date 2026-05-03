"""Tests for debug bundle generator (debug_bundle.py).

Covers:
- redact_secrets(): API keys, tokens, passwords, emails, JWTs, SSH keys, YAML
- redact_secrets(): clean text passes through unchanged
- collect_config(): reads and redacts bernstein.yaml
- collect_logs(): truncation, redaction, agent log limits
- collect_state(): task records, archive tail, runtime summary
- collect_diagnostics(): disk space, git status, worktree list
- create_debug_bundle(): produces valid zip with correct structure
- BundleManifest: tracks redaction count and file list
- generate_readme(): includes issue link
- Edge cases: missing .sdd, missing logs, empty project
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from bernstein.core.observability.debug_bundle import (
    BundleConfig,
    BundleManifest,
    collect_config,
    collect_diagnostics,
    collect_logs,
    collect_platform_info,
    collect_state,
    collect_version_info,
    create_debug_bundle,
    generate_readme,
    redact_secrets,
)

# ---------------------------------------------------------------------------
# redact_secrets() — API keys
# ---------------------------------------------------------------------------


class TestRedactApiKeys:
    def test_anthropic_api_key(self) -> None:
        text = "ANTHROPIC_API_KEY=sk-ant-api03-abc123xyz"
        result, count = redact_secrets(text)
        assert "sk-ant-api03-abc123xyz" not in result
        assert "***REDACTED***" in result
        assert count >= 1

    def test_openai_api_key(self) -> None:
        text = "OPENAI_API_KEY=sk-proj-abc123def456"
        result, count = redact_secrets(text)
        assert "sk-proj-abc123def456" not in result
        assert count >= 1

    def test_generic_api_key(self) -> None:
        text = "GITHUB_API_KEY=ghp_1234567890abcdef"
        result, count = redact_secrets(text)
        assert "ghp_1234567890abcdef" not in result
        assert count >= 1

    def test_api_key_with_export(self) -> None:
        text = "export MY_API_KEY=some-secret-value"
        result, count = redact_secrets(text)
        assert "some-secret-value" not in result
        assert count >= 1


# ---------------------------------------------------------------------------
# redact_secrets() — tokens
# ---------------------------------------------------------------------------


class TestRedactTokens:
    def test_token_env_var(self) -> None:
        text = "SONAR_TOKEN=sqp_abc123def456"
        result, count = redact_secrets(text)
        assert "sqp_abc123def456" not in result
        assert count >= 1

    def test_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
        result, count = redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert count >= 1

    def test_secret_env_var(self) -> None:
        text = "export GITHUB_SECRET=ghsec_abcdef1234567890"
        result, count = redact_secrets(text)
        assert "ghsec_abcdef1234567890" not in result
        assert count >= 1


# ---------------------------------------------------------------------------
# redact_secrets() — passwords
# ---------------------------------------------------------------------------


class TestRedactPasswords:
    def test_password_env(self) -> None:
        text = "export DATABASE_PASSWORD=hunter2"  # NOSONAR — test fixture for redaction
        result, count = redact_secrets(text)
        assert "hunter2" not in result
        assert count >= 1

    def test_credential_env(self) -> None:
        text = "AWS_CREDENTIAL=AKIAIOSFODNN7EXAMPLE"
        result, count = redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert count >= 1


# ---------------------------------------------------------------------------
# redact_secrets() — emails
# ---------------------------------------------------------------------------


class TestRedactEmails:
    def test_simple_email(self) -> None:
        text = "User alice@example.com logged in"
        result, count = redact_secrets(text)
        assert "alice@example.com" not in result
        assert "***REDACTED***" in result
        assert count >= 1

    def test_email_with_plus(self) -> None:
        text = "notify user+tag@domain.org"
        result, count = redact_secrets(text)
        assert "user+tag@domain.org" not in result
        assert count >= 1

    def test_multiple_emails(self) -> None:
        text = "from: a@b.com to: c@d.com"
        result, count = redact_secrets(text)
        assert "a@b.com" not in result
        assert "c@d.com" not in result
        assert count >= 2


# ---------------------------------------------------------------------------
# redact_secrets() — JWTs
# ---------------------------------------------------------------------------


class TestRedactJWTs:
    def test_jwt_token(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        text = f"token: {jwt}"
        result, count = redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert count >= 1

    def test_jwt_in_header(self) -> None:
        jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJodHRwczovL2V4YW1wbGUuY29tIn0.signature_here"
        text = f"Authorization: Bearer {jwt}"
        result, count = redact_secrets(text)
        assert "eyJhbGciOiJSUzI1Ni" not in result
        assert count >= 1


# ---------------------------------------------------------------------------
# redact_secrets() — SSH keys
# ---------------------------------------------------------------------------


class TestRedactSSHKeys:
    def test_rsa_private_key(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF\n-----END RSA PRIVATE KEY-----"
        result, count = redact_secrets(text)
        assert "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn" not in result
        assert count >= 1

    def test_openssh_key(self) -> None:
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n-----END OPENSSH PRIVATE KEY-----"
        result, count = redact_secrets(text)
        assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU" not in result
        assert count >= 1


# ---------------------------------------------------------------------------
# redact_secrets() — YAML sensitive values
# ---------------------------------------------------------------------------


class TestRedactYAMLValues:
    def test_yaml_token(self) -> None:
        text = "token: ghp_abc123"
        result, count = redact_secrets(text)
        assert "ghp_abc123" not in result
        assert count >= 1

    def test_yaml_secret(self) -> None:
        text = "client_secret: my-super-secret"
        result, count = redact_secrets(text)
        assert "my-super-secret" not in result
        assert count >= 1

    def test_yaml_password(self) -> None:
        text = "password: p@ssw0rd!"
        result, count = redact_secrets(text)
        assert "p@ssw0rd!" not in result
        assert count >= 1

    def test_yaml_auth_key(self) -> None:
        text = "auth_token: abc123"
        result, count = redact_secrets(text)
        assert "abc123" not in result
        assert count >= 1


# ---------------------------------------------------------------------------
# redact_secrets() — URL credentials
# ---------------------------------------------------------------------------


class TestRedactURLCredentials:
    def test_https_creds(self) -> None:
        text = "remote: https://user:pass123@github.com/repo.git"
        result, count = redact_secrets(text)
        assert "pass123" not in result
        assert count >= 1

    def test_http_creds(self) -> None:
        text = "url: http://admin:secret@localhost:8080/api"
        result, count = redact_secrets(text)
        assert "secret" not in result
        assert count >= 1


# ---------------------------------------------------------------------------
# redact_secrets() — clean text unchanged
# ---------------------------------------------------------------------------


class TestCleanTextUnchanged:
    def test_plain_text(self) -> None:
        text = "Task T001 completed in 3.2s"
        result, count = redact_secrets(text)
        assert result == text
        assert count == 0

    def test_empty_string(self) -> None:
        result, count = redact_secrets("")
        assert result == ""
        assert count == 0

    def test_log_line(self) -> None:
        text = "2024-01-15 10:30:00 INFO  [orchestrator] Spawning agent for task T042"
        result, count = redact_secrets(text)
        assert result == text
        assert count == 0

    def test_code_snippet(self) -> None:
        text = "def hello(name: str) -> str:\n    return f'Hello, {name}!'"
        result, count = redact_secrets(text)
        assert result == text
        assert count == 0


# ---------------------------------------------------------------------------
# collect_version_info()
# ---------------------------------------------------------------------------


class TestCollectVersionInfo:
    def test_contains_bernstein_version(self) -> None:
        info = collect_version_info()
        assert "bernstein:" in info

    def test_contains_python_version(self) -> None:
        info = collect_version_info()
        assert "python:" in info

    def test_contains_os_info(self) -> None:
        info = collect_version_info()
        assert "os:" in info


# ---------------------------------------------------------------------------
# collect_platform_info()
# ---------------------------------------------------------------------------


class TestCollectPlatformInfo:
    def test_contains_system(self) -> None:
        info = collect_platform_info()
        assert "system:" in info

    def test_contains_machine(self) -> None:
        info = collect_platform_info()
        assert "machine:" in info


# ---------------------------------------------------------------------------
# collect_config()
# ---------------------------------------------------------------------------


class TestCollectConfig:
    def test_missing_config(self, tmp_path: Path) -> None:
        text, count = collect_config(tmp_path)
        assert "not found" in text
        assert count == 0

    def test_config_with_secrets(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bernstein.yaml"
        config_file.write_text("model: claude-4\ntoken: sk-ant-secret123\npassword: hunter2\n")
        text, count = collect_config(tmp_path)
        assert "sk-ant-secret123" not in text
        assert "hunter2" not in text
        assert count >= 2

    def test_config_without_secrets(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bernstein.yaml"
        config_file.write_text("model: claude-4\nmax_agents: 5\n")
        text, count = collect_config(tmp_path)
        assert "model: claude-4" in text
        assert "max_agents: 5" in text
        assert count == 0


# ---------------------------------------------------------------------------
# collect_logs()
# ---------------------------------------------------------------------------


class TestCollectLogs:
    def test_missing_log_dir(self, tmp_path: Path) -> None:
        config = BundleConfig()
        logs = collect_logs(tmp_path, config)
        assert logs == {}

    def test_server_log_collected(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".sdd" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "server.log").write_text("line1\nline2\nline3\n")
        config = BundleConfig()
        logs = collect_logs(tmp_path, config)
        assert "server.log" in logs
        assert "line1" in logs["server.log"]

    def test_log_truncation(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".sdd" / "logs"
        log_dir.mkdir(parents=True)
        # Write 2000 lines, truncation at 10
        lines = [f"log line {i}" for i in range(2000)]
        (log_dir / "server.log").write_text("\n".join(lines))
        config = BundleConfig(max_log_lines=10)
        logs = collect_logs(tmp_path, config)
        content = logs["server.log"]
        # Should contain the last 10 lines
        assert "log line 1999" in content
        assert "log line 0" not in content

    def test_log_redaction(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".sdd" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "server.log").write_text("Starting server\nANTHROPIC_API_KEY=sk-ant-secret\n")
        config = BundleConfig()
        logs = collect_logs(tmp_path, config)
        assert "sk-ant-secret" not in logs["server.log"]

    def test_agent_log_limit(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".sdd" / "logs"
        log_dir.mkdir(parents=True)
        for i in range(10):
            (log_dir / f"agent_{i}.log").write_text(f"agent {i} output\n")
        config = BundleConfig(max_agent_logs=3)
        logs = collect_logs(tmp_path, config)
        agent_entries = [k for k in logs if k.startswith("agent_")]
        assert len(agent_entries) <= 3

    def test_extended_mode_no_truncation(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".sdd" / "logs"
        log_dir.mkdir(parents=True)
        lines = [f"log line {i}" for i in range(2000)]
        (log_dir / "server.log").write_text("\n".join(lines))
        config = BundleConfig(extended=True, max_log_lines=10)
        logs = collect_logs(tmp_path, config)
        content = logs["server.log"]
        # Extended mode should include everything
        assert "log line 0" in content
        assert "log line 1999" in content


# ---------------------------------------------------------------------------
# collect_state()
# ---------------------------------------------------------------------------


class TestCollectState:
    def test_missing_sdd(self, tmp_path: Path) -> None:
        config = BundleConfig()
        state = collect_state(tmp_path, config)
        assert state == {}

    def test_task_records(self, tmp_path: Path) -> None:
        backlog = tmp_path / ".sdd" / "backlog"
        backlog.mkdir(parents=True)
        records = [json.dumps({"id": f"T{i:03d}", "status": "done"}) for i in range(5)]
        (backlog / "tasks.jsonl").write_text("\n".join(records))
        config = BundleConfig()
        state = collect_state(tmp_path, config)
        assert "tasks.jsonl" in state
        assert "T004" in state["tasks.jsonl"]

    def test_task_record_limit(self, tmp_path: Path) -> None:
        backlog = tmp_path / ".sdd" / "backlog"
        backlog.mkdir(parents=True)
        records = [json.dumps({"id": f"T{i:03d}"}) for i in range(200)]
        (backlog / "tasks.jsonl").write_text("\n".join(records))
        config = BundleConfig(max_task_records=10)
        state = collect_state(tmp_path, config)
        content = state["tasks.jsonl"]
        # Should have last 10 records
        assert "T199" in content
        assert "T000" not in content

    def test_runtime_summary(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "pid").write_text("12345")
        (runtime / "port").write_text("8052")
        config = BundleConfig()
        state = collect_state(tmp_path, config)
        assert "runtime_summary.json" in state
        parsed = json.loads(state["runtime_summary.json"])
        assert parsed["pid"] == "12345"


# ---------------------------------------------------------------------------
# collect_diagnostics()
# ---------------------------------------------------------------------------


class TestCollectDiagnostics:
    def test_disk_space(self, tmp_path: Path) -> None:
        diags = collect_diagnostics(tmp_path)
        assert "disk_space.txt" in diags
        assert "GiB" in diags["disk_space.txt"]

    def test_git_status(self, tmp_path: Path) -> None:
        diags = collect_diagnostics(tmp_path)
        # Even if not a git repo, it should return something
        assert "git_status.txt" in diags

    def test_worktree_list(self, tmp_path: Path) -> None:
        diags = collect_diagnostics(tmp_path)
        assert "worktree_list.txt" in diags


# ---------------------------------------------------------------------------
# generate_readme()
# ---------------------------------------------------------------------------


class TestGenerateReadme:
    def test_contains_issue_link(self) -> None:
        manifest = BundleManifest(
            bernstein_version="1.0.0",
            platform_info="Linux x86_64",
            timestamp="20240115T103000Z",
            files_included=("a.txt", "b.txt"),
            redactions_applied=5,
        )
        readme = generate_readme(manifest)
        assert "github.com/sipyourdrink-ltd/bernstein/issues" in readme

    def test_contains_version(self) -> None:
        manifest = BundleManifest(
            bernstein_version="1.2.3",
            platform_info="Darwin arm64",
            timestamp="20240115T103000Z",
            files_included=("a.txt",),
            redactions_applied=0,
        )
        readme = generate_readme(manifest)
        assert "1.2.3" in readme

    def test_contains_redaction_count(self) -> None:
        manifest = BundleManifest(
            bernstein_version="1.0.0",
            platform_info="Linux x86_64",
            timestamp="20240115T103000Z",
            files_included=(),
            redactions_applied=42,
        )
        readme = generate_readme(manifest)
        assert "42" in readme

    def test_lists_files(self) -> None:
        manifest = BundleManifest(
            bernstein_version="1.0.0",
            platform_info="Linux x86_64",
            timestamp="20240115T103000Z",
            files_included=("config/bernstein.yaml", "logs/server.log"),
            redactions_applied=0,
        )
        readme = generate_readme(manifest)
        assert "config/bernstein.yaml" in readme
        assert "logs/server.log" in readme


# ---------------------------------------------------------------------------
# create_debug_bundle() — zip structure and content
# ---------------------------------------------------------------------------


class TestCreateDebugBundle:
    def test_produces_zip(self, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        assert path.exists()
        assert zipfile.is_zipfile(path)

    def test_zip_contains_version(self, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            version_files = [n for n in names if n.endswith("bernstein_version.txt")]
            assert len(version_files) == 1
            content = zf.read(version_files[0]).decode()
            assert "bernstein:" in content

    def test_zip_contains_platform(self, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            platform_files = [n for n in names if n.endswith("platform.txt")]
            assert len(platform_files) == 1

    def test_zip_contains_readme(self, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            readme_files = [n for n in names if n.endswith("README.md")]
            assert len(readme_files) == 1
            content = zf.read(readme_files[0]).decode()
            assert "github.com/sipyourdrink-ltd/bernstein/issues" in content

    def test_zip_contains_config(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text("model: claude-4\n")
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            cfg_files = [n for n in names if n.endswith("bernstein.yaml")]
            assert len(cfg_files) == 1

    def test_zip_contains_diagnostics(self, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            diag_files = [n for n in names if "/diagnostics/" in n]
            assert len(diag_files) >= 1

    def test_secrets_redacted_in_zip(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text("token: sk-ant-secret-value\n")
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, manifest = create_debug_bundle(tmp_path, config)
        assert manifest.redactions_applied >= 1
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                content = zf.read(name).decode()
                assert "sk-ant-secret-value" not in content


# ---------------------------------------------------------------------------
# BundleManifest — tracking
# ---------------------------------------------------------------------------


class TestBundleManifest:
    def test_manifest_tracks_redactions(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text("token: secret1\npassword: secret2\n")
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        _path, manifest = create_debug_bundle(tmp_path, config)
        assert manifest.redactions_applied >= 2

    def test_manifest_tracks_files(self, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        _path, manifest = create_debug_bundle(tmp_path, config)
        assert len(manifest.files_included) >= 4  # version, platform, config, diagnostics

    def test_manifest_has_timestamp(self, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        _path, manifest = create_debug_bundle(tmp_path, config)
        assert len(manifest.timestamp) > 0
        assert "T" in manifest.timestamp

    def test_manifest_is_frozen(self) -> None:
        manifest = BundleManifest(
            bernstein_version="1.0.0",
            platform_info="test",
            timestamp="now",
            files_included=(),
            redactions_applied=0,
        )
        try:
            manifest.redactions_applied = 99  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass  # Expected — frozen dataclass


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_project(self, tmp_path: Path) -> None:
        """Bundle creation works with no .sdd, no config, nothing."""
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        assert path.exists()
        assert zipfile.is_zipfile(path)

    def test_missing_sdd_handled(self, tmp_path: Path) -> None:
        """No .sdd directory does not crash."""
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        assert path.exists()

    def test_missing_log_files_handled(self, tmp_path: Path) -> None:
        """Existing .sdd/logs with no log files does not crash."""
        (tmp_path / ".sdd" / "logs").mkdir(parents=True)
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        assert path.exists()

    def test_auto_generated_output_path(self, tmp_path: Path, monkeypatch: object) -> None:
        """When output_path is None, zip is created in cwd."""
        import os

        # Use monkeypatch to change cwd to tmp_path
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            config = BundleConfig(output_path=None)
            path, _manifest = create_debug_bundle(tmp_path, config)
            assert path.exists()
            assert path.suffix == ".zip"
            assert "bernstein-debug-" in path.name
        finally:
            os.chdir(original_cwd)

    def test_bundle_config_is_frozen(self) -> None:
        config = BundleConfig()
        try:
            config.extended = True  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass  # Expected — frozen dataclass

    def test_redact_preserves_structure(self) -> None:
        """Redaction of YAML keeps the key visible, only the value is masked."""
        text = "token: my-secret-token"
        result, _count = redact_secrets(text)
        assert "token:" in result
        assert "my-secret-token" not in result

    def test_multiple_secrets_in_one_text(self) -> None:
        text = "ANTHROPIC_API_KEY=sk-ant-abc\nOPENAI_API_KEY=sk-xyz\nuser: alice@corp.com\n"
        result, count = redact_secrets(text)
        assert "sk-ant-abc" not in result
        assert "sk-xyz" not in result
        assert "alice@corp.com" not in result
        assert count >= 3

    def test_zip_structure_has_prefix(self, tmp_path: Path) -> None:
        """All entries in the zip are under a bernstein-debug-* prefix."""
        output = tmp_path / "out.zip"
        config = BundleConfig(output_path=output)
        path, _manifest = create_debug_bundle(tmp_path, config)
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                assert name.startswith("bernstein-debug-")
