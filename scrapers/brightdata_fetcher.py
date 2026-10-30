"""
scrapers/brightdata_fetcher.py — BrightData Web Unlocker API fetcher
====================================================================

Replaces CloakBrowser as the Cloudflare bypass method for RENTCafe/
SecureRC domains.

Problem
-------
Railway (and other cloud platform) IPs are blocked by Cloudflare WAF
firewall rules on ``*.securerc.co.uk``. This is IP-level blocking —
no browser automation tool (CloakBrowser, Playwright, nodriver) can
bypass it. curl_cffi TLS impersonation also gets 403 regardless of
fingerprint.

Solution
--------
BrightData Web Unlocker API handles CF challenge solving, CAPTCHA
solving, JS rendering, and residential proxy rotation in a single API
call. The API returns clean HTML.

Free tier: 5,000 requests/month, no credit card required.
Docs: https://docs.brightdata.com/

Usage
-----
Set environment variables::

    BRIGHTDATA_API_KEY=xxx
    BRIGHTDATA_ZONE=web_unlocker1   # or agent_unlocker for agent accounts

Then use as a drop-in replacement for ``RentCafeBrowserFetcher``::

    fetcher = BrightDataFetcher()
    html = fetcher.fetch_html("https://...securerc.co.uk/.../floorplans.aspx")

Credit budgeting
----------------
Each ``fetch_html`` call consumes 1 API credit. With 5,000 free
credits/month, usage must be budgeted:

- 3 buildings × ~8 requests per round = ~24 credits per round
- At 30-min intervals: 48 rounds/day × 24 = 1,152/day ≈ 34,560/month (too many)
- At 2-hour intervals: 12 rounds/day × 24 = 288/day ≈ 8,640/month (too many)
- At 4-hour intervals: 6 rounds/day × 24 = 144/day ≈ 4,320/month (fits!)

Floorplan ID caching reduces per-round credits from ~24 to ~21 (only
AJAX calls cost credits after first floorplan fetch). With a 3-hour
interval: 8 rounds × 21 + 3 × 2 refreshes = 174/day ≈ 5,220/month
(very close to limit — set RENTCAFE_CHECK_INTERVAL=10800 to be safe).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Delayed exception loading (same pattern as rentcafe_browser.py)
_exc_cache: dict[str, type] = {}


def _exc(name: str) -> type:
    if name not in _exc_cache:
        from scrapers.base import (  # noqa: E402
            BlockedError,
            RateLimitError,
            ScrapeNetworkError,
        )
        _exc_cache.update({
            "BlockedError": BlockedError,
            "RateLimitError": RateLimitError,
            "ScrapeNetworkError": ScrapeNetworkError,
        })
    return _exc_cache[name]


# ── Credit tracking (process-level, persists across scrape rounds) ──────
#
# BrightData free tier = 5,000 credits/month. We track usage to avoid
# exceeding the limit. When close to the limit, log warnings.
_CREDIT_LIMIT_DEFAULT = 4500  # leave 500 as buffer
_credit_used: int = 0
_credit_month: int = 0  # 1-12, reset when month changes
_credit_reset_day: int = 1  # reset on day 1 of each month


def _current_month() -> int:
    from datetime import date
    return date.today().month


def _maybe_reset_credits() -> None:
    """Reset credit counter if we've crossed into a new month."""
    global _credit_used, _credit_month
    month = _current_month()
    if month != _credit_month:
        logger.info(
            "BrightData credit counter reset (month %d → %d, used %d last month)",
            _credit_month, month, _credit_used,
        )
        _credit_used = 0
        _credit_month = month


def get_credit_usage() -> int:
    """Return current month's BrightData credit usage."""
    _maybe_reset_credits()
    return _credit_used


def get_credit_limit() -> int:
    """Return configured monthly credit limit."""
    try:
        return int(os.environ.get("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "") or _CREDIT_LIMIT_DEFAULT)
    except ValueError:
        return _CREDIT_LIMIT_DEFAULT


def credits_remaining() -> int:
    """Return remaining credits for this month."""
    return max(0, get_credit_limit() - get_credit_usage())


def _consume_credit(n: int = 1) -> None:
    """Record consumption of n API credits."""
    global _credit_used
    _maybe_reset_credits()
    _credit_used += n
    limit = get_credit_limit()
    if _credit_used >= limit:
        logger.error(
            "BrightData credit limit reached: %d/%d — further requests will fail. "
            "Set BRIGHTDATA_API_KEY with a paid plan or wait until next month.",
            _credit_used, limit,
        )
    elif _credit_used >= limit * 0.8:
        logger.warning(
            "BrightData credit usage at %d/%d (%.0f%%) — approaching limit",
            _credit_used, limit, _credit_used / limit * 100,
        )


def is_credit_available() -> bool:
    """Check if credits are still available."""
    return credits_remaining() > 0


class BrightDataFetcher:
    """
    BrightData Web Unlocker API wrapper for RENTCafe HTML scraping.

    Drop-in replacement for ``RentCafeBrowserFetcher``. Same interface:
    - ``ensure_initialized(entry_url)`` — no-op (API is stateless)
    - ``fetch_html(url, referer, ajax)`` — GET HTML via Web Unlocker API
    - ``close()`` — no-op (no browser to close)

    Each ``fetch_html`` call = 1 API credit.
    """

    API_URL = "https://api.brightdata.com/request"

    def __init__(self, headless: bool = True):
        # headless param kept for interface compatibility, ignored
        self._api_key: Optional[str] = os.environ.get("BRIGHTDATA_API_KEY", "").strip() or None
        self._zone: str = os.environ.get("BRIGHTDATA_ZONE", "").strip() or "web_unlocker1"
        self._initialized = False
        self._entry_url: Optional[str] = None
        self._origin: Optional[str] = None

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_configured(self) -> bool:
        """Check if API key is set."""
        return bool(self._api_key)

    # ── Context manager ──────────────────────────────────────────────
    def __enter__(self) -> "BrightDataFetcher":
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """No-op — no browser resources to release."""
        pass

    # ── Init (no-op, kept for interface compat) ──────────────────────
    def ensure_initialized(self, entry_url: str) -> None:
        """
        No-op initialization. BrightData API is stateless — each request
        is independent. This method exists for interface compatibility
        with RentCafeBrowserFetcher.
        """
        from urllib.parse import urlparse
        parsed = urlparse(entry_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._entry_url = entry_url
        self._initialized = True
        logger.debug("BrightDataFetcher initialized (zone=%s, origin=%s)", self._zone, self._origin)

    # ── CF challenge detection ───────────────────────────────────────
    def _is_cf_challenge(self, html: str) -> bool:
        """Check if returned HTML is a Cloudflare challenge/block page."""
        lower = html.lower()
        return (
            "just a moment" in lower
            or "请稍候" in html
            or "challenge-platform" in lower
            or "cf-turnstile" in lower
            or "attention required" in lower
            or "cf.errors.css" in lower
            or "you have been blocked" in lower
        )

    # ── HTML fetch ───────────────────────────────────────────────────
    def fetch_html(
        self,
        url: str,
        *,
        referer: str = "",
        ajax: bool = False,
        timeout_ms: int = 30_000,
    ) -> str:
        """
        GET HTML from a URL via BrightData Web Unlocker API.

        Each call consumes 1 API credit. The API handles:
        - Cloudflare WAF bypass (IP-level blocks)
        - JS challenge solving (Turnstile)
        - CAPTCHA solving
        - Residential proxy rotation
        - JS rendering (enabled via render=true)

        Parameters
        ----------
        url         : Full URL to fetch
        referer     : Referer header (optional, passed as custom header)
        ajax        : If True, add X-Requested-With: XMLHttpRequest
        timeout_ms  : API timeout in milliseconds

        Returns
        -------
        Response body as text (HTML string)

        Raises
        ------
        BlockedError       Credits exhausted or CF challenge persists
        RateLimitError     API rate limited (429)
        ScrapeNetworkError Network/API errors
        """
        if not self._api_key:
            raise _exc("BlockedError")(
                "BrightData API key not set. Set BRIGHTDATA_API_KEY env var. "
                "Free tier: 5,000 requests/month, no credit card. "
                "Sign up: https://brightdata.com/products/web-unlocker"
            )

        # Check credit budget
        _maybe_reset_credits()
        if not is_credit_available():
            raise _exc("BlockedError")(
                f"BrightData monthly credit limit reached ({get_credit_usage()}/"
                f"{get_credit_limit()}). Wait until next month or upgrade plan."
            )

        # Lazy init if not initialized
        if not self._initialized:
            self.ensure_initialized(url)

        # Build request payload
        #
        # render=true: Force JS rendering via browser. RENTCafe floorplans.aspx
        # requires JS to fully render floorplan selectors. Without render=true,
        # the API may return a CF challenge page or an incomplete HTML.
        #
        # x-unblock-expect: Wait for RENTCafe-specific elements before returning.
        # This ensures the page is fully loaded, not just CF-cleared.
        # For AJAX calls (rcLoadContent.ashx), we don't need to wait for elements
        # since the response is server-generated HTML fragment.
        payload: dict = {
            "zone": self._zone,
            "url": url,
            "format": "raw",
            "method": "GET",
            "render": "true",  # Force JS rendering — essential for CF challenge + RENTCafe JS
        }

        # Add country code for Netherlands (better geo-matching)
        country = os.environ.get("BRIGHTDATA_COUNTRY", "").strip()
        if country:
            payload["country"] = country

        # For non-AJAX (page) requests, wait for RENTCafe elements to appear.
        # This prevents getting an incomplete page back.
        # x-unblock-expect uses CSS selectors to wait for content.
        if not ajax:
            # Wait for floorplan-related elements that appear after JS hydration
            payload["headers"] = {
                "x-unblock-expect": json.dumps({
                    "element": "[data-selenium-id], .floor-plan-selector, #FFloorPlan, body"
                }),
            }
        else:
            # AJAX calls: pass custom headers for X-Requested-With
            # Note: Custom headers require "Custom Headers & Cookies" enabled
            # in the zone. If not enabled, the API auto-generates headers.
            # We try it anyway — if it fails, the response is still usable.
            ajax_headers: dict[str, str] = {
                "Accept": "text/html, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }
            if referer:
                ajax_headers["Referer"] = referer
            payload["headers"] = ajax_headers

        # Make API request
        import requests as _requests

        # With render=true, CF challenge solving + JS rendering can take 30-60s.
        # Increase timeout for page requests (non-AJAX).
        api_timeout = max(timeout_ms / 1000, 60) if not ajax else max(timeout_ms / 1000, 45)
        start = time.monotonic()

        try:
            resp = _requests.post(
                self.API_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                json=payload,
                timeout=api_timeout,
            )
        except _requests.exceptions.Timeout:
            raise _exc("ScrapeNetworkError")(
                f"BrightData API timeout after {api_timeout}s for url={url}"
            )
        except _requests.exceptions.ConnectionError as e:
            raise _exc("ScrapeNetworkError")(
                f"BrightData API connection error: {e}"
            )

        elapsed = time.monotonic() - start

        # BrightData charges per request — consume credit
        _consume_credit(1)

        # Handle API-level errors
        if resp.status_code == 429:
            raise _exc("RateLimitError")(
                "BrightData API rate limited (429). Reduce request frequency."
            )

        if resp.status_code == 402:
            # Payment required — free tier exhausted or account issue
            try:
                err = resp.json()
                msg = err.get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise _exc("BlockedError")(
                f"BrightData API returned 402 (credits exhausted or account issue): {msg}"
            )

        if resp.status_code == 401:
            raise _exc("BlockedError")(
                "BrightData API key invalid (401). Check BRIGHTDATA_API_KEY."
            )

        if resp.status_code >= 400:
            try:
                err = resp.json()
                msg = err.get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise _exc("ScrapeNetworkError")(
                f"BrightData API error HTTP {resp.status_code}: {msg}"
            )

        # Extract HTML from response
        # format=raw returns the page content directly
        html = resp.text

        # Log response details for debugging
        logger.info(
            "BrightData fetch: url=%s, status=%d, %.1fs, %d chars, credits=%d/%d",
            url, resp.status_code, elapsed, len(html), get_credit_usage(), get_credit_limit(),
        )

        # Check if returned HTML is still a CF challenge (shouldn't happen
        # with Web Unlocker + render=true, but guard against it)
        if self._is_cf_challenge(html):
            logger.warning(
                "BrightData returned CF challenge page for %s (unexpected with render=true). "
                "HTML first 500 chars: %s",
                url, html[:500],
            )
            raise _exc("BlockedError")(
                f"BrightData returned CF challenge page for {url}. "
                "Web Unlocker may not be able to bypass this CF level."
            )

        # Check for empty or suspiciously short responses
        if len(html) < 200:
            logger.warning(
                "BrightData returned very short response (%d chars) for %s. "
                "Content: %s",
                len(html), url, html[:200],
            )

        logger.debug(
            "BrightData fetch OK: %s (%.1fs, %d chars, credits used: %d/%d)",
            url, elapsed, len(html), get_credit_usage(), get_credit_limit(),
        )

        return html
