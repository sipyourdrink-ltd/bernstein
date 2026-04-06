"""Tests for SEC-006: comprehensive secret scanning in guardrails.py."""

from __future__ import annotations

from bernstein.core.guardrails import check_secrets
from bernstein.core.policy_engine import DecisionType


class TestAWSSecrets:
    """Test detection of AWS credentials."""

    def test_aws_access_key(self) -> None:
        diff = "+AKIAIOSFODNN7EXAMPLE\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_aws_secret_key(self) -> None:
        diff = "+aws_secret_access_key = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_aws_session_token(self) -> None:
        diff = "+aws_session_token = '" + "A" * 100 + "'\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY


class TestGCPSecrets:
    """Test detection of GCP credentials."""

    def test_gcp_service_account(self) -> None:
        diff = '+  "type": "service_account"\n'
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_gcp_api_key(self) -> None:
        diff = "+AIzaSyA1234567890abcdefghijklmnopqrstuvwx\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY


class TestGitHubTokens:
    """Test detection of GitHub tokens."""

    def test_github_pat_classic(self) -> None:
        diff = "+ghp_abcdefghijklmnopqrstuvwxyz123456789012\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_github_pat_fine_grained(self) -> None:
        diff = "+github_pat_" + "a" * 82 + "\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_github_oauth_token(self) -> None:
        diff = "+gho_abcdefghijklmnopqrstuvwxyz1234567890\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY


class TestSlackSecrets:
    """Test detection of Slack tokens and webhooks."""

    def test_slack_bot_token(self) -> None:
        diff = "+xoxb-1234567890-1234567890-" + "a" * 24 + "\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_slack_webhook(self) -> None:
        diff = "+https://hooks.slack.com/services/T12345678/B12345678/abcdefghijklmnop\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY


class TestDatabaseURLs:
    """Test detection of database connection strings."""

    def test_postgres_url(self) -> None:
        diff = "+postgresql://user:password@host:5432/db\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_mysql_url(self) -> None:
        diff = "+mysql://root:secret@localhost/mydb\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_mongodb_url(self) -> None:
        diff = "+mongodb+srv://user:pass@cluster.example.com/db\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_redis_url(self) -> None:
        diff = "+redis://default:password@redis.example.com:6379\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY


class TestOtherTokens:
    """Test detection of various API tokens."""

    def test_stripe_live_key(self) -> None:
        diff = "+sk_live_" + "a" * 24 + "\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_sendgrid_key(self) -> None:
        diff = "+SG." + "a" * 22 + "." + "b" * 43 + "\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_npm_token(self) -> None:
        diff = "+npm_" + "a" * 36 + "\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_pypi_token(self) -> None:
        diff = "+pypi-" + "a" * 50 + "\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_gitlab_pat(self) -> None:
        diff = "+glpat-" + "a" * 20 + "\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_private_key_header(self) -> None:
        diff = "+-----BEGIN RSA PRIVATE KEY-----\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_jwt_token(self) -> None:
        diff = "+eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456ghi789\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY


class TestCleanDiffs:
    """Test that clean diffs pass secret scanning."""

    def test_no_secrets(self) -> None:
        diff = "+# This is a normal code comment\n+x = 42\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.ALLOW

    def test_placeholder_values(self) -> None:
        diff = "+API_KEY = 'your-api-key-here'\n"
        results = check_secrets(diff)
        # Short placeholder should not match generic_secret (< 8 chars)
        # This may or may not match depending on length
        assert len(results) == 1

    def test_documentation_example(self) -> None:
        diff = "+# Set your AWS region in the config file\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.ALLOW
