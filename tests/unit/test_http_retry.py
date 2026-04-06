"""Tests for ORCH-003: HTTP retry with exponential backoff and jitter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from bernstein.core.http_retry import (
    RetryConfig,
    compute_backoff,
    is_retryable_exception,
    is_retryable_response,
    retry_http,
    retry_request,
)

# ---------------------------------------------------------------------------
# compute_backoff
# ---------------------------------------------------------------------------


class TestComputeBackoff:
    """Tests for the backoff calculation."""

    def test_exponential_increase(self) -> None:
        d0 = compute_backoff(0, 1.0, 60.0, jitter=False)
        d1 = compute_backoff(1, 1.0, 60.0, jitter=False)
        d2 = compute_backoff(2, 1.0, 60.0, jitter=False)
        assert d0 == pytest.approx(1.0)
        assert d1 == pytest.approx(2.0)
        assert d2 == pytest.approx(4.0)

    def test_capped_at_max_delay(self) -> None:
        delay = compute_backoff(100, 1.0, 30.0, jitter=False)
        assert delay == pytest.approx(30.0)

    def test_jitter_within_range(self) -> None:
        for _ in range(100):
            delay = compute_backoff(3, 1.0, 60.0, jitter=True)
            assert 0 <= delay <= 8.0  # 1.0 * 2^3 = 8.0

    def test_zero_base_delay(self) -> None:
        delay = compute_backoff(5, 0.0, 30.0, jitter=False)
        assert delay == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# is_retryable_response / is_retryable_exception
# ---------------------------------------------------------------------------


class TestRetryableChecks:
    """Tests for retryable status and exception checks."""

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
    def test_retryable_status_codes(self, status: int) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        assert is_retryable_response(resp, RetryConfig()) is True

    @pytest.mark.parametrize("status", [200, 201, 400, 401, 403, 404])
    def test_non_retryable_status_codes(self, status: int) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        assert is_retryable_response(resp, RetryConfig()) is False

    def test_connect_error_is_retryable(self) -> None:
        assert is_retryable_exception(httpx.ConnectError("refused")) is True

    def test_timeout_is_retryable(self) -> None:
        assert is_retryable_exception(httpx.ReadTimeout("timeout")) is True

    def test_value_error_not_retryable(self) -> None:
        assert is_retryable_exception(ValueError("bad value")) is False


# ---------------------------------------------------------------------------
# retry_http decorator
# ---------------------------------------------------------------------------


class TestRetryHttpDecorator:
    """Tests for the retry_http decorator."""

    def test_no_retry_on_success(self) -> None:
        call_count = 0

        @retry_http(max_retries=3)
        def fetch() -> httpx.Response:
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            return resp

        result = fetch()
        assert result.status_code == 200
        assert call_count == 1

    @patch("bernstein.core.http_retry.time.sleep")
    def test_retries_on_retryable_status(self, mock_sleep: MagicMock) -> None:
        call_count = 0

        @retry_http(max_retries=2, base_delay_s=0.01, max_delay_s=0.1)
        def fetch() -> httpx.Response:
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 503 if call_count < 3 else 200
            return resp

        result = fetch()
        assert result.status_code == 200
        assert call_count == 3
        assert mock_sleep.call_count == 2

    @patch("bernstein.core.http_retry.time.sleep")
    def test_retries_on_retryable_exception(self, mock_sleep: MagicMock) -> None:
        call_count = 0

        @retry_http(max_retries=2, base_delay_s=0.01)
        def fetch() -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.ConnectError("connection refused")
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            return resp

        result = fetch()
        assert result.status_code == 200
        assert call_count == 3

    def test_raises_non_retryable_exception(self) -> None:
        @retry_http(max_retries=3)
        def fetch() -> httpx.Response:
            raise ValueError("bad value")

        with pytest.raises(ValueError, match="bad value"):
            fetch()

    @patch("bernstein.core.http_retry.time.sleep")
    def test_exhausts_retries(self, mock_sleep: MagicMock) -> None:
        @retry_http(max_retries=2, base_delay_s=0.01)
        def fetch() -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(httpx.ConnectError):
            fetch()

    @patch("bernstein.core.http_retry.time.sleep")
    def test_returns_last_retryable_response_when_exhausted(self, mock_sleep: MagicMock) -> None:
        @retry_http(max_retries=1, base_delay_s=0.01)
        def fetch() -> httpx.Response:
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 503
            return resp

        result = fetch()
        assert result.status_code == 503


# ---------------------------------------------------------------------------
# retry_request functional API
# ---------------------------------------------------------------------------


class TestRetryRequest:
    """Tests for the retry_request function."""

    def test_success_returns_response(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        mock_client.request.return_value = resp

        result = retry_request(mock_client, "GET", "http://localhost/tasks")
        assert result.status_code == 200

    @patch("bernstein.core.http_retry.time.sleep")
    def test_retries_and_recovers(self, mock_sleep: MagicMock) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 503
        ok_resp = MagicMock(spec=httpx.Response)
        ok_resp.status_code = 200
        mock_client.request.side_effect = [error_resp, ok_resp]

        config = RetryConfig(max_retries=2, base_delay_s=0.01)
        result = retry_request(mock_client, "GET", "http://localhost/tasks", config=config)
        assert result.status_code == 200
        assert mock_client.request.call_count == 2


# ---------------------------------------------------------------------------
# RetryConfig
# ---------------------------------------------------------------------------


class TestRetryConfig:
    """Tests for RetryConfig defaults."""

    def test_defaults(self) -> None:
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.base_delay_s == pytest.approx(1.0)
        assert config.max_delay_s == pytest.approx(30.0)
        assert config.jitter is True
        assert 429 in config.retryable_status_codes
        assert 503 in config.retryable_status_codes
