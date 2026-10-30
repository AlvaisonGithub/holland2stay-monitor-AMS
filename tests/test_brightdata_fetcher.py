"""Tests for the BrightData Web Unlocker fetcher module."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# We need to import after patching env, so use importlib
def _import_fetcher():
    import importlib
    import scrapers.brightdata_fetcher as mod
    importlib.reload(mod)
    return mod


class TestBrightDataFetcher:
    """Test the BrightData Web Unlocker API fetcher."""

    def test_no_api_key_raises_blocked_error(self, monkeypatch):
        """When BRIGHTDATA_API_KEY is not set, fetch_html should raise BlockedError."""
        monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
        mod = _import_fetcher()
        fetcher = mod.BrightDataFetcher()
        assert not fetcher.is_configured

        from scrapers.base import BlockedError
        with pytest.raises(BlockedError, match="API key not set"):
            fetcher.fetch_html("https://example.com/test")

    def test_credit_tracking_resets_on_new_month(self, monkeypatch):
        """Credit counter should reset when the month changes."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        mod = _import_fetcher()

        # Simulate usage in month 1
        mod._credit_month = 1
        mod._credit_used = 4000

        # Mock current month to be 2
        with patch("scrapers.brightdata_fetcher._current_month", return_value=2):
            mod._maybe_reset_credits()

        assert mod._credit_used == 0
        assert mod._credit_month == 2

    def test_is_configured_with_api_key(self, monkeypatch):
        """is_configured should return True when API key is set."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key_123")
        mod = _import_fetcher()
        fetcher = mod.BrightDataFetcher()
        assert fetcher.is_configured is True

    def test_not_configured_without_api_key(self, monkeypatch):
        """is_configured should return False when API key is empty."""
        monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
        mod = _import_fetcher()
        fetcher = mod.BrightDataFetcher()
        assert fetcher.is_configured is False

    def test_credit_limit_from_env(self, monkeypatch):
        """Credit limit should be configurable via env var."""
        monkeypatch.setenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "3000")
        mod = _import_fetcher()
        assert mod.get_credit_limit() == 3000

    def test_credit_limit_default(self, monkeypatch):
        """Credit limit should default to 4500 when not set."""
        monkeypatch.delenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", raising=False)
        mod = _import_fetcher()
        assert mod.get_credit_limit() == 4500

    def test_credits_remaining(self, monkeypatch):
        """credits_remaining should return limit - used."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        monkeypatch.setenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "5000")
        mod = _import_fetcher()
        mod._credit_used = 1000
        mod._credit_month = mod._current_month()
        assert mod.credits_remaining() == 4000

    def test_credit_exhausted_raises_error(self, monkeypatch):
        """When credits are exhausted, fetch should raise BlockedError."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        monkeypatch.setenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "100")
        mod = _import_fetcher()
        mod._credit_used = 100
        mod._credit_month = mod._current_month()

        fetcher = mod.BrightDataFetcher()
        fetcher.ensure_initialized("https://example.com/")

        from scrapers.base import BlockedError
        with pytest.raises(BlockedError, match="credit limit reached"):
            fetcher.fetch_html("https://example.com/test")

    def test_cf_challenge_detection(self, monkeypatch):
        """_is_cf_challenge should detect various CF page types."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        mod = _import_fetcher()
        fetcher = mod.BrightDataFetcher()

        assert fetcher._is_cf_challenge("Just a moment...") is True
        assert fetcher._is_cf_challenge("请稍候…") is True
        assert fetcher._is_cf_challenge('<html>challenge-platform</html>') is True
        assert fetcher._is_cf_challenge("Attention Required! | Cloudflare") is True
        assert fetcher._is_cf_challenge("you have been blocked") is True

        # Normal pages should not be flagged
        assert fetcher._is_cf_challenge("<html><body>Normal page</body></html>") is False
        assert fetcher._is_cf_challenge("Available Units") is False

    def test_close_is_noop(self, monkeypatch):
        """close() should be a no-op (no browser resources)."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        mod = _import_fetcher()
        fetcher = mod.BrightDataFetcher()
        # Should not raise
        fetcher.close()

    def test_ensure_initialized_sets_origin(self, monkeypatch):
        """ensure_initialized should extract and store the origin."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        mod = _import_fetcher()
        fetcher = mod.BrightDataFetcher()
        fetcher.ensure_initialized("https://thisisourdomain.securerc.co.uk/onlineleasing/test/floorplans.aspx")
        assert fetcher._origin == "https://thisisourdomain.securerc.co.uk"
        assert fetcher.is_initialized is True

    @patch("requests.post")
    def test_successful_fetch_returns_html(self, mock_post, monkeypatch):
        """A successful API call should return the HTML content."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key_123")
        monkeypatch.setenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "5000")
        mod = _import_fetcher()

        # Reset credits
        mod._credit_used = 0
        mod._credit_month = mod._current_month()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Floor plans here</body></html>"
        mock_post.return_value = mock_resp

        fetcher = mod.BrightDataFetcher()
        html = fetcher.fetch_html("https://example.com/floorplans.aspx")

        assert "Floor plans here" in html
        assert mod.get_credit_usage() == 1  # consumed 1 credit

        # Verify API call format
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["zone"] == "web_unlocker1"
        assert payload["url"] == "https://example.com/floorplans.aspx"
        assert payload["format"] == "raw"
        assert payload["method"] == "GET"

        # Verify auth header
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test_key_123"

    @patch("requests.post")
    def test_402_raises_blocked_error(self, mock_post, monkeypatch):
        """HTTP 402 (payment required) should raise BlockedError."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        monkeypatch.setenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "5000")
        mod = _import_fetcher()
        mod._credit_used = 0
        mod._credit_month = mod._current_month()

        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.text = '{"message": "Credits exhausted"}'
        mock_resp.json.return_value = {"message": "Credits exhausted"}
        mock_post.return_value = mock_resp

        fetcher = mod.BrightDataFetcher()
        from scrapers.base import BlockedError
        with pytest.raises(BlockedError, match="credits exhausted"):
            fetcher.fetch_html("https://example.com/test")

    @patch("requests.post")
    def test_cf_challenge_in_response_raises_blocked(self, mock_post, monkeypatch):
        """If API returns a CF challenge page, raise BlockedError."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        monkeypatch.setenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "5000")
        mod = _import_fetcher()
        mod._credit_used = 0
        mod._credit_month = mod._current_month()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Just a moment... <!-- challenge-platform -->"
        mock_post.return_value = mock_resp

        fetcher = mod.BrightDataFetcher()
        from scrapers.base import BlockedError
        with pytest.raises(BlockedError, match="CF challenge page"):
            fetcher.fetch_html("https://example.com/test")
