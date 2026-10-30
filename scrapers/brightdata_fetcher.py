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
import re
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
        """
        Check if returned HTML is a Cloudflare challenge/block page.

        Important: RENTCafe pages served through Cloudflare include
        ``/cdn-cgi/challenge-platform/`` script references even on the
        REAL page (after CF passes). We must NOT flag those as challenge
        pages. Only flag pages whose <title> or body content clearly
        indicates a CF interstitial/challenge page.

        False positives would discard 27K+ chars of valid RENTCafe HTML!
        """
        if not html or len(html) < 20:
            return False

        lower = html.lower()

        # Strong signals: these only appear on actual CF challenge pages
        # Check the <title> tag — CF challenge pages have distinctive titles
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = title_match.group(1).strip().lower() if title_match else ""

        if "just a moment" in title:
            return True
        if "attention required" in title and "cloudflare" in title:
            return True
        if "请稍候" in title:
            return True

        # Body-level strong signals (not just script references)
        # "cf.errors.css" only appears on CF block pages
        if "cf.errors.css" in lower:
            return True

        # "you have been blocked" in the visible body (not script)
        if "you have been blocked" in lower and "ray id" in lower:
            return True

        # cf-turnstile as the MAIN content (not just a reference in a script)
        # Real pages may reference turnstile in scripts but don't have the
        # visible turnstile widget as primary content
        if "cf-turnstile" in lower and len(html) < 5000:
            # Short page with turnstile = likely challenge page
            return True

        return False

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

        API body parameters (per docs.brightdata.com/api-reference):
            zone, url, format, method, country, data_format, render, debug, async

        Note: "headers" is NOT a valid body parameter. Custom headers go
        via the x-unblock-custom-headers HTTP header on the API request.
        x-unblock-expect is also an HTTP header, not a body field.

        Parameters
        ----------
        url         : Full URL to fetch
        referer     : Referer header (optional, passed as custom header for AJAX)
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
        # Official API body parameters (per docs.brightdata.com/api-reference):
        #   zone, url, format, method, country, data_format, render, debug, async
        #
        # NOTE: "headers" is NOT a valid body parameter. Custom headers/cookies
        # are ignored by default unless "Custom Headers & Cookies" is enabled
        # in the zone config. The API auto-generates optimal headers.
        #
        # Strategy for render:
        # - For floorplans.aspx (non-AJAX page): try WITHOUT render first.
        #   The floorplan IDs (subPointerId=, myFloorPlanId=) are in the
        #   server-side rendered HTML — they don't need JS. Without render,
        #   BrightData returns the raw server HTML (faster, cheaper, and
        #   preserves the onclick attributes that contain the IDs).
        #   If that returns a CF challenge, we retry with render=true.
        # - For AJAX calls: never use render (server-generated HTML fragment).
        #
        # format=json: Returns {status_code, headers, body} instead of raw HTML.
        # This lets us inspect the target's actual HTTP status code.
        use_render = False if not ajax else False  # Default: no render

        payload: dict = {
            "zone": self._zone,
            "url": url,
            "format": "json",  # Structured response with status_code + body
            "method": "GET",
        }

        # Add render only if needed (will be added in retry logic if first
        # attempt returns a CF challenge)
        if use_render:
            payload["render"] = "true"

        # Add country code for Netherlands (better geo-matching)
        country = os.environ.get("BRIGHTDATA_COUNTRY", "").strip()
        if country:
            payload["country"] = country

        # Build HTTP headers for the API request itself.
        api_headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        if not ajax and use_render:
            # Only use x-unblock-expect when render=true (JS rendering).
            # Without render, the content is server-side HTML — no need to
            # wait for elements. The "body" selector in the expect header
            # was matching too early and returning before JS loaded content.
            api_headers["x-unblock-expect"] = json.dumps({
                "element": "[data-selenium-id], .floor-plan-selector, #FFloorPlan",
            })
        elif ajax:
            # For AJAX calls: tell the API to add X-Requested-With header
            # to the outbound request to the target site.
            # NOTE: This requires "Custom Headers & Cookies" enabled in the
            # zone config. If not enabled, the API auto-generates headers.
            # We set it via x-unblock-custom-headers (HTTP header, not payload).
            custom_headers: dict[str, str] = {
                "Accept": "text/html, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }
            if referer:
                custom_headers["Referer"] = referer
            api_headers["x-unblock-custom-headers"] = json.dumps(custom_headers)

        # Make API request
        import requests as _requests

        # With render=true, CF challenge solving + JS rendering can take 30-60s.
        # Increase timeout for page requests (non-AJAX).
        api_timeout = max(timeout_ms / 1000, 60) if not ajax else max(timeout_ms / 1000, 45)
        start = time.monotonic()

        try:
            resp = _requests.post(
                self.API_URL,
                headers=api_headers,
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

        # Parse JSON response (format=json returns structured data)
        try:
            data = resp.json()
        except Exception as e:
            # If JSON parsing fails, fall back to raw text
            logger.warning(
                "BrightData response not JSON (url=%s, %.1fs, %d chars). "
                "Falling back to raw text. Error: %s",
                url, elapsed, len(resp.text), e,
            )
            html = resp.text
            target_status = resp.status_code
        else:
            # format=json returns: {status_code, headers, body}
            target_status = data.get("status_code", 0)
            html = data.get("body", "")

        # Check the x-brd-debug header for debugging info
        debug_header = resp.headers.get("x-brd-debug", "")
        if debug_header:
            logger.debug("BrightData debug: %s", debug_header)

        # Log response details for debugging
        logger.info(
            "BrightData fetch: url=%s, api_status=%d, target_status=%s, %.1fs, %d chars, credits=%d/%d",
            url, resp.status_code, target_status or "?", elapsed, len(html),
            get_credit_usage(), get_credit_limit(),
        )

        # Check if returned HTML is a CF challenge page.
        # If so (and we didn't use render), retry with render=true below.
        # The final CF-challenge raise is AFTER the retry loop.
        needs_render_retry = (
            not ajax
            and not use_render
            and (
                self._is_cf_challenge(html)
                or (isinstance(target_status, int) and target_status >= 500)
                or len(html) < 200
            )
        )

        # Retry strategies for 5xx errors, CF challenges, or empty responses.
        # Since we started without render, the main retry is adding render=true.
        # Also try different countries for 5xx errors.
        if needs_render_retry or (isinstance(target_status, int) and target_status >= 500 and not ajax):
            retry_strategies: list[tuple[str, dict, dict]] = []

            if not use_render and not ajax:
                # Strategy 1: Add render=true (JS rendering to solve CF challenge)
                render_payload = {**payload, "render": "true"}
                render_headers = dict(api_headers)
                render_headers["x-unblock-expect"] = json.dumps({
                    "element": "[data-selenium-id], .floor-plan-selector, #FFloorPlan",
                })
                retry_strategies.append(("render", render_payload, render_headers))

            if isinstance(target_status, int) and target_status >= 500:
                # Strategy 2: Try US country (different proxy pool, may avoid 502)
                us_payload = {**payload}
                if not use_render:
                    us_payload["render"] = "true"
                us_payload["country"] = "us"
                us_headers = dict(api_headers)
                if "x-unblock-expect" not in us_headers:
                    us_headers["x-unblock-expect"] = json.dumps({
                        "element": "[data-selenium-id], .floor-plan-selector, #FFloorPlan",
                    })
                retry_strategies.append(("us-render", us_payload, us_headers))

                # Strategy 3: Try GB country
                gb_payload = {**payload}
                if not use_render:
                    gb_payload["render"] = "true"
                gb_payload["country"] = "gb"
                gb_headers = dict(us_headers)
                retry_strategies.append(("gb-render", gb_payload, gb_headers))

            for strategy_name, retry_payload, retry_hdrs in retry_strategies:
                if not is_credit_available():
                    break
                logger.info(
                    "BrightData: target returned %d, retrying with strategy=%s for %s",
                    target_status, strategy_name, url,
                )
                try:
                    retry_resp = _requests.post(
                        self.API_URL,
                        headers=retry_hdrs,
                        json=retry_payload,
                        timeout=api_timeout,
                    )
                except Exception as retry_err:
                    logger.warning("BrightData retry (%s) failed: %s", strategy_name, retry_err)
                    continue

                if retry_resp.status_code >= 400:
                    logger.warning(
                        "BrightData retry (%s) API error %d for %s",
                        strategy_name, retry_resp.status_code, url,
                    )
                    _consume_credit(1)
                    continue

                try:
                    retry_data = retry_resp.json()
                    retry_status = retry_data.get("status_code", 0)
                    retry_html = retry_data.get("body", "")
                except Exception:
                    retry_status = 0
                    retry_html = retry_resp.text

                retry_elapsed = time.monotonic() - start
                _consume_credit(1)

                logger.info(
                    "BrightData retry (%s): target_status=%s, %.1fs, %d chars for %s",
                    strategy_name, retry_status or "?", retry_elapsed, len(retry_html), url,
                )

                if retry_status and retry_status < 500 and len(retry_html) > 200:
                    # Retry succeeded!
                    html = retry_html
                    target_status = retry_status
                    elapsed = retry_elapsed
                    logger.info("BrightData retry (%s) succeeded for %s", strategy_name, url)
                    break
                else:
                    logger.warning(
                        "BrightData retry (%s) still failing: target_status=%s, %d chars",
                        strategy_name, retry_status or "?", len(retry_html),
                    )

        # After all retry strategies, if the HTML is STILL a CF challenge,
        # we've exhausted our options — raise BlockedError.
        # (This catches the case where both the initial no-render attempt AND
        # all render retries returned CF challenge pages.)
        if self._is_cf_challenge(html):
            logger.warning(
                "BrightData returned CF challenge page for %s after all retries. "
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

        # Detect KYC requirement — BrightData's free tier (no KYC) blocks
        # residential proxy access to certain domains with restrictive
        # robots.txt. This returns a 402 status with an error message.
        if "kyc" in html.lower() and "brightdata.com/cp/kyc" in html.lower():
            logger.error(
                "BrightData KYC required for %s. This domain is blocked in "
                "immediate residential (no KYC) mode. Fill in KYC form at "
                "https://brightdata.com/cp/kyc to enable full residential access. "
                "Response: %s",
                url, html[:300],
            )

        # If target returned a server error (5xx), log it prominently.
        # BrightData successfully reached the target but the target itself
        # returned an error. This is NOT a BrightData failure.
        if isinstance(target_status, int) and target_status >= 500:
            logger.warning(
                "BrightData: target server returned HTTP %d for %s "
                "(%.1fs, %d chars). Target may be down or blocking BrightData's proxy.",
                target_status, url, elapsed, len(html),
            )

        logger.debug(
            "BrightData fetch OK: %s (%.1fs, %d chars, credits used: %d/%d)",
            url, elapsed, len(html), get_credit_usage(), get_credit_limit(),
        )

        return html
