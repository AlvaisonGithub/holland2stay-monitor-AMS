"""
scrapers/zenrows_fetcher.py — ZenRows Fetch API wrapper for CF WAF bypass
=========================================================================

Alternative to BrightData Web Unlocker for RENTCafe/SecureRC domains.

Problem
-------
BrightData's free tier (no KYC) blocks residential proxy access to domains
with restrictive robots.txt (Disallow: /). Both ``thisisourdomain.securerc.co.uk``
and ``southeast-thisisourdomain.securerc.co.uk`` have ``Disallow: /``, so
BrightData returns a KYC error for all OurDomain buildings.

Solution: ZenRows Fetch API
----------------------------
ZenRows does NOT check robots.txt and does NOT require KYC for its free tier.
Key advantages over BrightData for this use case:

- **5,000 free credits/month, no credit card required**
- **No KYC / business verification** — personal users can sign up
- **Does NOT respect robots.txt** — can access Disallow: / domains
- **Adaptive Stealth Mode** (``mode=auto``) — starts cheap, escalates only
  if needed. Failed attempts are not charged.
- Bypasses Cloudflare WAF, JS challenges, CAPTCHAs

Credit costs (per request):
    Raw HTML + premium proxy:   10 credits  (normal — CF WAF bypass)
    JS render + premium proxy:  25 credits  (fallback — CF JS challenge)

With explicit ``premium_proxy=true`` (no ``js_render``), we get raw server
HTML preserving ``onclick`` attributes that contain floorplan IDs.
If CF challenge detected, retry with ``js_render=true`` (25 credits).

With floorplan ID caching (12h TTL), budget is manageable within
5,000 credits/month.

Docs: https://docs.zenrows.com/
Sign up: https://app.zenrows.com/register

Usage
-----
Set environment variables::

    ZENROWS_API_KEY=xxx

Then use as a drop-in replacement for ``BrightDataFetcher``::

    fetcher = ZenRowsFetcher()
    html = fetcher.fetch_html("https://...securerc.co.uk/.../floorplans.aspx")
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Delayed exception loading (same pattern as brightdata_fetcher.py)
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
# ZenRows free tier = 5,000 credits/month. We track usage to avoid
# exceeding the limit. With premium_proxy=True (needed for CF WAF bypass),
# each request costs 10 credits.
_CREDIT_LIMIT_DEFAULT = 4500  # leave 500 as buffer
_credit_used: int = 0
_credit_month: int = 0  # 1-12, reset when month changes


def _current_month() -> int:
    from datetime import date
    return date.today().month


def _maybe_reset_credits() -> None:
    """Reset credit counter if we've crossed into a new month."""
    global _credit_used, _credit_month
    month = _current_month()
    if month != _credit_month:
        logger.info(
            "ZenRows credit counter reset (month %d → %d, used %d last month)",
            _credit_month, month, _credit_used,
        )
        _credit_used = 0
        _credit_month = month


def get_credit_usage() -> int:
    """Return current month's ZenRows credit usage."""
    _maybe_reset_credits()
    return _credit_used


def get_credit_limit() -> int:
    """Return configured monthly credit limit."""
    try:
        return int(os.environ.get("ZENROWS_MONTHLY_CREDIT_LIMIT", "") or _CREDIT_LIMIT_DEFAULT)
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
            "ZenRows credit limit reached: %d/%d — further requests will fail. "
            "Wait until next month or upgrade plan.",
            _credit_used, limit,
        )
    elif _credit_used >= limit * 0.8:
        logger.warning(
            "ZenRows credit usage at %d/%d (%.0f%%) — approaching limit",
            _credit_used, limit, _credit_used / limit * 100,
        )


def is_credit_available() -> bool:
    """Check if credits are still available."""
    return credits_remaining() > 0


class ZenRowsFetcher:
    """
    ZenRows Fetch API wrapper for RENTCafe HTML scraping.

    Drop-in replacement for ``BrightDataFetcher`` / ``RentCafeBrowserFetcher``.
    Same interface:
    - ``ensure_initialized(entry_url)`` — no-op (API is stateless)
    - ``fetch_html(url, referer, ajax)`` — GET HTML via ZenRows API
    - ``close()`` — no-op (no browser to close)

    Strategy: explicit ``premium_proxy=true`` without ``js_render`` to get
    raw server-side HTML. This is critical for RENTCafe because floorplan IDs
    (``subPointerId=NNN``) live in server-rendered ``onclick`` attributes
    that get stripped during JS hydration.

    If raw HTML returns CF challenge, retries with ``js_render=true``
    (25 credits fallback).

    Credit costs:
    - Raw HTML + premium proxy:  10 credits (normal)
    - JS render + premium proxy: 25 credits (fallback only)
    """

    API_URL = "https://api.zenrows.com/v1/"

    def __init__(self, headless: bool = True):
        # headless param kept for interface compatibility, ignored
        self._api_key: Optional[str] = os.environ.get("ZENROWS_API_KEY", "").strip() or None
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
    def __enter__(self) -> "ZenRowsFetcher":
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """No-op — no browser resources to release."""
        pass

    # ── Init (no-op, kept for interface compat) ──────────────────────
    def ensure_initialized(self, entry_url: str) -> None:
        """
        No-op initialization. ZenRows API is stateless — each request
        is independent. This method exists for interface compatibility.
        """
        from urllib.parse import urlparse
        parsed = urlparse(entry_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._entry_url = entry_url
        self._initialized = True
        logger.debug("ZenRowsFetcher initialized (origin=%s)", self._origin)

    # ── CF challenge detection ───────────────────────────────────────
    def _is_cf_challenge(self, html: str) -> bool:
        """
        Check if returned HTML is a Cloudflare challenge/block page.

        Same logic as BrightDataFetcher._is_cf_challenge — RENTCafe pages
        served through Cloudflare include challenge-platform script references
        even on real pages. We only flag actual CF interstitial pages.
        """
        if not html or len(html) < 20:
            return False

        lower = html.lower()

        # Strong signals: check <title> for CF-specific titles
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = title_match.group(1).strip().lower() if title_match else ""

        if "just a moment" in title:
            return True
        if "attention required" in title and "cloudflare" in title:
            return True
        if "请稍候" in title:
            return True

        # Body-level strong signals
        if "cf.errors.css" in lower:
            return True
        if "you have been blocked" in lower and "ray id" in lower:
            return True
        if "cf-turnstile" in lower and len(html) < 5000:
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
        GET HTML from a URL via ZenRows Fetch API.

        Strategy: use explicit ``premium_proxy=true`` without ``js_render``
        to get raw server-side HTML. This is critical for RENTCafe floorplans
        because floorplan IDs (``subPointerId=NNN``) live in server-rendered
        ``onclick`` attributes that get stripped during JS hydration.

        If the raw-HTML attempt returns a CF challenge page, retry with
        ``js_render=true`` + ``premium_proxy=true`` (25 credits) as fallback.

        Credit costs:
        - Raw HTML + premium proxy:  10 credits (normal)
        - JS render + premium proxy: 25 credits (fallback only)

        Parameters
        ----------
        url         : Full URL to fetch
        referer     : Referer header (optional, forwarded via custom_headers)
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
                "ZenRows API key not set. Set ZENROWS_API_KEY env var. "
                "Free tier: 5,000 credits/month, no credit card, no KYC. "
                "Sign up: https://app.zenrows.com/register"
            )

        # Check credit budget
        _maybe_reset_credits()
        if not is_credit_available():
            raise _exc("BlockedError")(
                f"ZenRows monthly credit limit reached ({get_credit_usage()}/"
                f"{get_credit_limit()}). Wait until next month or upgrade plan."
            )

        # Lazy init if not initialized
        if not self._initialized:
            self.ensure_initialized(url)

        # Build API request parameters.
        #
        # Strategy: explicit params instead of mode=auto for deterministic
        # raw server-side HTML. This is critical because:
        #
        # - RENTCafe floorplan IDs (subPointerId=NNN) live in server-rendered
        #   onclick attributes — JS hydration strips them.
        # - mode=auto sometimes uses js_render (returns JS-hydrated DOM),
        #   sometimes doesn't (returns raw HTML) — non-deterministic.
        # - We need raw HTML consistently for regex extraction to work.
        #
        # Configuration: premium_proxy=true, js_render NOT set (defaults false)
        # - Bypasses CF WAF (needs residential proxy)
        # - Returns raw server HTML (preserves onclick attributes)
        # - Cost: 10 credits per request
        #
        # Fallback: if CF challenge detected, retry with js_render=true +
        # premium_proxy=true (25 credits). JS render may solve CF JS challenge
        # but will lose onclick patterns — caller needs JS-rendered fallback
        # patterns for floorplan ID extraction.

        params: dict[str, str] = {
            "apikey": self._api_key,
            "url": url,
            "premium_proxy": "true",  # Bypass CF WAF (residential proxy)
            # js_render NOT set — defaults to false (raw server HTML)
        }

        # Add proxy_country for Netherlands (better geo-matching)
        country = os.environ.get("ZENROWS_COUNTRY", "").strip()
        if country:
            params["proxy_country"] = country

        # Return original status code for debugging
        params["original_status"] = "true"

        # For AJAX calls, pass custom headers.
        # ZenRows API: set custom_headers=true in query params (boolean toggle),
        # then pass the actual headers as standard HTTP request headers on the
        # requests.get() call. ZenRows forwards them to the target site.
        # Do NOT use header_X-Requested-With query params — that causes REQS004.
        req_headers: dict[str, str] = {}
        if ajax:
            params["custom_headers"] = "true"
            req_headers["X-Requested-With"] = "XMLHttpRequest"
            req_headers["Accept"] = "text/html, */*; q=0.01"
            if referer:
                req_headers["Referer"] = referer

        import requests as _requests

        # ZenRows with premium proxies can take 15-30s for CF WAF bypass
        api_timeout = max(timeout_ms / 1000, 45)
        start = time.monotonic()

        try:
            resp = _requests.get(
                self.API_URL,
                params=params,
                headers=req_headers,
                timeout=api_timeout,
            )
        except _requests.exceptions.Timeout:
            raise _exc("ScrapeNetworkError")(
                f"ZenRows API timeout after {api_timeout}s for url={url}"
            )
        except _requests.exceptions.ConnectionError as e:
            raise _exc("ScrapeNetworkError")(
                f"ZenRows API connection error: {e}"
            )

        elapsed = time.monotonic() - start

        # Handle API-level errors
        if resp.status_code == 429:
            raise _exc("RateLimitError")(
                "ZenRows API rate limited (429). Reduce request frequency."
            )

        if resp.status_code == 402:
            # Payment required — free tier exhausted
            try:
                err = resp.json()
                msg = err.get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise _exc("BlockedError")(
                f"ZenRows API returned 402 (credits exhausted): {msg}"
            )

        if resp.status_code == 401:
            raise _exc("BlockedError")(
                "ZenRows API key invalid (401). Check ZENROWS_API_KEY."
            )

        if resp.status_code == 422:
            # Unprocessable entity — bad parameters or URL
            try:
                err = resp.json()
                msg = err.get("detail", err.get("message", resp.text[:300]))
            except Exception:
                msg = resp.text[:300]
            raise _exc("ScrapeNetworkError")(
                f"ZenRows API returned 422 (invalid request): {msg}"
            )

        if resp.status_code >= 400:
            try:
                err = resp.json()
                msg = err.get("detail", err.get("message", resp.text[:300]))
            except Exception:
                msg = resp.text[:300]
            raise _exc("ScrapeNetworkError")(
                f"ZenRows API error HTTP {resp.status_code}: {msg}"
            )

        html = resp.text

        # Estimate credit cost: premium_proxy=true = 10 credits per request.
        estimated_cost = 10
        _consume_credit(estimated_cost)

        # Log response details for debugging
        logger.info(
            "ZenRows fetch: url=%s, api_status=%d, %.1fs, %d chars, "
            "credits=~%d/%d (est)",
            url, resp.status_code, elapsed, len(html),
            get_credit_usage(), get_credit_limit(),
        )

        # Check if returned HTML is a CF challenge page.
        # If so, retry with js_render=true + premium_proxy=true (25 credits).
        # JS rendering may solve CF JS challenges that raw HTML can't bypass.
        if self._is_cf_challenge(html):
            logger.warning(
                "ZenRows raw HTML was CF challenge for %s. "
                "Retrying with js_render=true (25 credits). "
                "HTML first 500 chars: %s",
                url, html[:500],
            )
            # Retry with JS rendering
            retry_params = dict(params)
            retry_params["js_render"] = "true"
            # Wait for page to load after JS execution
            retry_params["wait_for"] = "body"

            retry_start = time.monotonic()
            try:
                retry_resp = _requests.get(
                    self.API_URL,
                    params=retry_params,
                    headers=req_headers,
                    timeout=api_timeout,
                )
            except _requests.exceptions.Timeout:
                raise _exc("ScrapeNetworkError")(
                    f"ZenRows API timeout after {api_timeout}s (js_render retry) for url={url}"
                )
            except _requests.exceptions.ConnectionError as e:
                raise _exc("ScrapeNetworkError")(
                    f"ZenRows API connection error (js_render retry): {e}"
                )

            retry_elapsed = time.monotonic() - retry_start

            if retry_resp.status_code >= 400:
                try:
                    err = retry_resp.json()
                    msg = err.get("detail", err.get("message", retry_resp.text[:300]))
                except Exception:
                    msg = retry_resp.text[:300]
                raise _exc("ScrapeNetworkError")(
                    f"ZenRows js_render retry error HTTP {retry_resp.status_code}: {msg}"
                )

            html = retry_resp.text
            _consume_credit(15)  # js_render + premium_proxy = 25 total (10 already counted)

            logger.info(
                "ZenRows js_render retry: url=%s, api_status=%d, %.1fs, %d chars, "
                "credits=~%d/%d (est)",
                url, retry_resp.status_code, retry_elapsed, len(html),
                get_credit_usage(), get_credit_limit(),
            )

            # Check again for CF challenge
            if self._is_cf_challenge(html):
                logger.warning(
                    "ZenRows js_render retry also returned CF challenge for %s. "
                    "HTML first 500 chars: %s",
                    url, html[:500],
                )
                raise _exc("BlockedError")(
                    f"ZenRows returned CF challenge page for {url} even with js_render. "
                    "The site may have protection that ZenRows cannot bypass."
                )

        # Check for empty or suspiciously short responses
        if len(html) < 200:
            logger.warning(
                "ZenRows returned very short response (%d chars) for %s. "
                "Content: %s",
                len(html), url, html[:200],
            )

        logger.debug(
            "ZenRows fetch OK: %s (%.1fs, %d chars, credits used: %d/%d)",
            url, elapsed, len(html), get_credit_usage(), get_credit_limit(),
        )

        return html
