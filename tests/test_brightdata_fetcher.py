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
        """_is_cf_challenge should detect actual CF challenge pages but NOT
        real pages that happen to include CF script references."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        mod = _import_fetcher()
        fetcher = mod.BrightDataFetcher()

        # Real CF challenge pages — detected by <title> tag
        assert fetcher._is_cf_challenge(
            '<html><head><title>Just a moment...</title></head><body>challenge-platform</body></html>'
        ) is True
        assert fetcher._is_cf_challenge(
            '<html><head><title>Attention Required! | Cloudflare</title></head></html>'
        ) is True
        assert fetcher._is_cf_challenge(
            '<html><head><title>请稍候…</title></head></html>'
        ) is True
        # CF block page with cf.errors.css
        assert fetcher._is_cf_challenge(
            '<html><head><link rel="stylesheet" href="/cdn-cgi/styles/cf.errors.css"></head></html>'
        ) is True
        # "You have been blocked" with ray ID
        assert fetcher._is_cf_challenge(
            '<html><body>You have been blocked. Ray ID: abc123</body></html>'
        ) is True
        # Short page with turnstile (likely challenge)
        assert fetcher._is_cf_challenge(
            '<html><body><div class="cf-turnstile"></div></body></html>'
        ) is True

        # Real RENTCafe page with CF script references — should NOT be flagged
        # This is the key test: RENTCafe pages include /cdn-cgi/challenge-platform/
        # scripts even after CF passes, but they are NOT challenge pages.
        rentcafe_html = (
            '<!doctype html><html lang="en">'
            '<head><title>RENTCafe - We Make Renting Easy</title>'
            '<script src="/cdn-cgi/challenge-platform/scripts/precursor/main.js"></script>'
            '</head><body><div id="FFloorPlan">Floor plans here</div></body></html>'
        )
        assert fetcher._is_cf_challenge(rentcafe_html) is False

        # Other normal pages
        assert fetcher._is_cf_challenge("<html><body>Normal page</body></html>") is False
        assert fetcher._is_cf_challenge("Available Units") is False
        # Empty/short strings
        assert fetcher._is_cf_challenge("") is False
        assert fetcher._is_cf_challenge("ab") is False

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
        # format=json returns {status_code, headers, body}
        # Body must be >200 chars to avoid triggering retry logic
        body = "<html><body>" + "Floor plans here. " * 20 + "</body></html>"
        mock_resp.json.return_value = {
            "status_code": 200,
            "headers": {"content-type": "text/html"},
            "body": body,
        }
        mock_resp.headers = {}
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
        assert payload["format"] == "json"
        assert payload["method"] == "GET"
        # render should NOT be in the default payload — it's added only
        # on retry if the first attempt returns a CF challenge or 5xx.
        # The floorplan IDs are in server-side rendered HTML, so we don't
        # need JS rendering by default.
        assert "render" not in payload

        # Verify auth header
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test_key_123"

        # Without render=true, we don't send x-unblock-expect
        # (the floorplan IDs are in server-side HTML, no need to wait for JS)
        assert "x-unblock-expect" not in headers
        assert "headers" not in payload  # "headers" is NOT a valid body field

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
        mock_resp.headers = {}
        mock_post.return_value = mock_resp

        fetcher = mod.BrightDataFetcher()
        from scrapers.base import BlockedError
        with pytest.raises(BlockedError, match="credits exhausted"):
            fetcher.fetch_html("https://example.com/test")

    @patch("requests.post")
    def test_cf_challenge_in_response_raises_blocked(self, mock_post, monkeypatch):
        """If API returns a CF challenge page on both initial and render retry,
        raise BlockedError."""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "test_key")
        monkeypatch.setenv("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "5000")
        mod = _import_fetcher()
        mod._credit_used = 0
        mod._credit_month = mod._current_month()

        cf_body = (
            "<html><head><title>Just a moment...</title></head>"
            "<body><!-- challenge-platform --></body></html>"
        )

        # First call (no render): returns CF challenge
        # Subsequent calls (with render): also return CF challenge
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status_code": 403,
            "headers": {},
            "body": cf_body,
        }
        mock_resp.headers = {}
        mock_post.return_value = mock_resp

        fetcher = mod.BrightDataFetcher()
        from scrapers.base import BlockedError
        with pytest.raises(BlockedError, match="CF challenge page"):
            fetcher.fetch_html("https://example.com/test")
