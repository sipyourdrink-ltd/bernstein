"""Tests for SEC-017: Sensitive file detection and handling."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.sensitive_file_detector import (
    DetectionConfidence,
    SensitiveCategory,
    SensitiveFileConfig,
    SensitiveFileDetector,
)


class TestPathDetection:
    def test_pem_file(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("certs/server.pem")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.PRIVATE_KEY
        assert result.confidence == DetectionConfidence.HIGH

    def test_key_file(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("ssl/private.key")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.PRIVATE_KEY

    def test_env_file(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path(".env")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.ENVIRONMENT_FILE

    def test_env_production(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path(".env.production")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.ENVIRONMENT_FILE

    def test_env_local(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("config/.env.local")
        assert result.is_sensitive

    def test_credentials_json(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("credentials.json")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.CREDENTIALS

    def test_ssh_key(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("id_rsa")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.SSH_KEY

    def test_ssh_ecdsa_key(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path(".ssh/id_ecdsa")
        assert result.is_sensitive

    def test_aws_credentials(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path(".aws/credentials")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.CLOUD_CREDENTIALS

    def test_kubeconfig(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path(".kube/config")
        assert result.is_sensitive

    def test_safe_python_file(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("src/main.py")
        assert not result.is_sensitive

    def test_safe_readme(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("README.md")
        assert not result.is_sensitive

    def test_htpasswd(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path(".htpasswd")
        assert result.is_sensitive

    def test_p12_file(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("cert.p12")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.PRIVATE_KEY

    def test_service_account_json(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_path("service_account.json")
        assert result.is_sensitive
        assert result.category == SensitiveCategory.CLOUD_CREDENTIALS


class TestContentDetection:
    def test_private_key_header(self) -> None:
        detector = SensitiveFileDetector()
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow..."
        result = detector.scan_content("key.txt", content)
        assert result.is_sensitive
        assert result.category == SensitiveCategory.PRIVATE_KEY
        assert result.line_number == 1

    def test_ec_private_key(self) -> None:
        detector = SensitiveFileDetector()
        content = "-----BEGIN EC PRIVATE KEY-----\nMHQC..."
        result = detector.scan_content("key.txt", content)
        assert result.is_sensitive

    def test_openssh_key(self) -> None:
        detector = SensitiveFileDetector()
        content = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz..."
        result = detector.scan_content("id_rsa", content)
        assert result.is_sensitive
        assert result.category == SensitiveCategory.SSH_KEY

    def test_anthropic_api_key(self) -> None:
        detector = SensitiveFileDetector()
        content = "ANTHROPIC_API_KEY=sk-ant-abc123def456ghi789jkl012mno"
        result = detector.scan_content("config.txt", content)
        assert result.is_sensitive

    def test_aws_access_key(self) -> None:
        detector = SensitiveFileDetector()
        content = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
        result = detector.scan_content("creds.txt", content)
        assert result.is_sensitive
        assert result.category == SensitiveCategory.CLOUD_CREDENTIALS

    def test_password_in_config(self) -> None:
        detector = SensitiveFileDetector()
        content = "database:\n  password: SuperSecretPassword123!"
        result = detector.scan_content("config.yaml", content)
        assert result.is_sensitive
        assert result.category == SensitiveCategory.CONFIG_SECRET

    def test_safe_content(self) -> None:
        detector = SensitiveFileDetector()
        content = "def hello():\n    print('Hello, world!')\n"
        result = detector.scan_content("main.py", content)
        assert not result.is_sensitive

    def test_line_number_reported(self) -> None:
        detector = SensitiveFileDetector()
        content = "line 1\nline 2\n-----BEGIN PRIVATE KEY-----\ndata"
        result = detector.scan_content("file.txt", content)
        assert result.is_sensitive
        assert result.line_number == 3


class TestScanPaths:
    def test_scan_multiple_paths(self) -> None:
        detector = SensitiveFileDetector()
        summary = detector.scan_paths(
            [
                "src/main.py",
                ".env",
                "README.md",
                "id_rsa",
            ]
        )
        assert summary.total_scanned == 4
        assert summary.sensitive_count == 2
        assert not summary.safe_for_commit

    def test_all_safe(self) -> None:
        detector = SensitiveFileDetector()
        summary = detector.scan_paths(["src/main.py", "README.md"])
        assert summary.safe_for_commit

    def test_empty_list(self) -> None:
        detector = SensitiveFileDetector()
        summary = detector.scan_paths([])
        assert summary.total_scanned == 0
        assert summary.safe_for_commit


class TestScanFile:
    def test_scan_nonexistent_file(self) -> None:
        detector = SensitiveFileDetector()
        result = detector.scan_file(Path("/nonexistent/file.py"))
        assert not result.is_sensitive

    def test_scan_real_file_by_path_only(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("DB_HOST=localhost\n")
        detector = SensitiveFileDetector()
        result = detector.scan_file(env_file)
        # Path-based detection should catch this
        assert result.is_sensitive

    def test_scan_file_with_content(self, tmp_path: Path) -> None:
        src_file = tmp_path / "config.txt"
        src_file.write_text("-----BEGIN RSA PRIVATE KEY-----\ndata\n")
        detector = SensitiveFileDetector()
        result = detector.scan_file(src_file)
        assert result.is_sensitive

    def test_content_scanning_disabled(self, tmp_path: Path) -> None:
        src_file = tmp_path / "config.txt"
        src_file.write_text("-----BEGIN RSA PRIVATE KEY-----\ndata\n")
        config = SensitiveFileConfig(scan_content=False)
        detector = SensitiveFileDetector(config)
        result = detector.scan_file(src_file)
        # Path doesn't match, content scanning disabled
        assert not result.is_sensitive


class TestIgnorePaths:
    def test_ignored_path_not_flagged(self) -> None:
        config = SensitiveFileConfig(ignore_paths=["test_fixtures"])
        detector = SensitiveFileDetector(config)
        result = detector.scan_path("test_fixtures/.env")
        assert not result.is_sensitive

    def test_non_ignored_path_flagged(self) -> None:
        config = SensitiveFileConfig(ignore_paths=["test_fixtures"])
        detector = SensitiveFileDetector(config)
        result = detector.scan_path("production/.env")
        assert result.is_sensitive


class TestCustomPatterns:
    def test_extra_path_pattern(self) -> None:
        config = SensitiveFileConfig(
            extra_path_patterns=[
                (r".*\.secret$", SensitiveCategory.CONFIG_SECRET, DetectionConfidence.HIGH),
            ],
        )
        detector = SensitiveFileDetector(config)
        result = detector.scan_path("app.secret")
        assert result.is_sensitive

    def test_extra_content_pattern(self) -> None:
        config = SensitiveFileConfig(
            extra_content_patterns=[
                (r"CUSTOM_TOKEN_[A-Z0-9]{20,}", SensitiveCategory.TOKEN_FILE, DetectionConfidence.HIGH),
            ],
        )
        detector = SensitiveFileDetector(config)
        result = detector.scan_content(
            "config.txt",
            "auth = CUSTOM_TOKEN_ABCDEF1234567890ABCD",
        )
        assert result.is_sensitive


class TestScanDirectory:
    def test_scan_directory(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / ".env").write_text("SECRET=foo")
        detector = SensitiveFileDetector()
        summary = detector.scan_directory(tmp_path)
        assert summary.total_scanned == 2
        assert summary.sensitive_count >= 1
        assert not summary.safe_for_commit

    def test_scan_empty_directory(self, tmp_path: Path) -> None:
        detector = SensitiveFileDetector()
        summary = detector.scan_directory(tmp_path)
        assert summary.total_scanned == 0
        assert summary.safe_for_commit
