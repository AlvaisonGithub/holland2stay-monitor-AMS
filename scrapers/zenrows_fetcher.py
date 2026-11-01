"""
scrapers/zenrows_fetcher.py — ZenRows Fetch API wrapper
=======================================================

Single fetcher for all RENTCafe/SecureRC Cloudflare-protected pages.

Why ZenRows
-----------
- **No KYC** — personal users can sign up (BrightData requires business KYC)
- **No robots.txt check** — can access Disallow: / domains (both
  ``thisisourdomain.securerc.co.uk`` and ``southeast-...`` have Disallow: /)
- **5,000 free credits/month**, no credit card
- ``mode=auto`` auto-selects JS rendering + premium proxy when needed

Credit costs (mode=auto, bills only for successful config):
    Standard request:     1 credit
    JS rendering:          5 credits
    Premium proxies:      10 credits
    JS + premium proxies: 25 credits (typical for CF WAF sites)

With floorplan ID caching (12h TTL), budget is manageable within
5,000 credits/month at ~5-min poll intervals.

Env vars
--------
    ZENROWS_API_KEY=xxx                    # required
    ZENROWS_COUNTRY=nl                     # optional, geo-matching
    ZENROWS_MONTHLY_CREDIT_LIMIT=4500      # optional process-local safety cap; disabled by default

The local counter is diagnostic only, not the account balance. By default,
ZenRows itself is authoritative for balance/402 decisions. Prefer the
X-Request-Cost response header when available.

Docs: https://docs.zenrows.com/
Sign up: https://app.zenrows.com/register
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# ── Exceptions (lazy-loaded to avoid circular import) ────────────────
_exc_cache: dict[str, type] = {}


def _exc(name: str) -> type:
    if name not in _exc_cache:
        from scrapers.base import BlockedError, RateLimitError, ScrapeNetworkError
        _exc_cache.update({
            "BlockedError": BlockedError,
            "RateLimitError": RateLimitError,
            "ScrapeNetworkError": ScrapeNetworkError,
        })
    return _exc_cache[name]


# ── Credit tracking (process-level, persists across scrape rounds) ────
# A process-local cap cannot reliably represent monthly account usage because
# Railway restarts reset this in-memory counter. Therefore it is disabled by
# default; ZenRows' own 402 response is authoritative for account exhaustion.
_CREDIT_LIMIT_DEFAULT = 0
_credit_used: float = 0.0
_credit_month: str = ""


def _maybe_reset_credits() -> None:
    """Reset the process-local counter when the calendar month changes."""
    global _credit_used, _credit_month
    month = date.today().strftime("%Y-%m")
    if month != _credit_month:
        logger.info(
            "ZenRows local credit counter reset (%s→%s, previous estimate %.2f)",
            _credit_month or "未初始化", month, _credit_used,
        )
        _credit_used = 0.0
        _credit_month = month


def get_credit_usage() -> float:
    _maybe_reset_credits()
    return _credit_used


def get_credit_limit() -> int:
    try:
        return int(os.environ.get("ZENROWS_MONTHLY_CREDIT_LIMIT", "") or _CREDIT_LIMIT_DEFAULT)
    except ValueError:
        return _CREDIT_LIMIT_DEFAULT


def credits_remaining() -> int:
    """Return local-cap headroom, or -1 when no local cap is configured."""
    limit = get_credit_limit()
    return max(0, limit - int(get_credit_usage())) if limit > 0 else -1


def _consume_credit(cost: float, *, source: str) -> None:
    """Record actual request cost when reported, otherwise a conservative estimate."""
    global _credit_used
    _maybe_reset_credits()
    _credit_used += max(0.0, float(cost))
    limit = get_credit_limit()
    if limit <= 0:
        # No local cutoff: this process-local estimate resets on deploy/restart.
        return
    if _credit_used >= limit:
        logger.error(
            "ZenRows local safety cap reached: %.2f/%.0f (cost source=%s)",
            _credit_used, limit, source,
        )
    elif _credit_used >= limit * 0.8:
        logger.warning(
            "ZenRows local credit estimate at %.2f/%.0f (%.0f%%, source=%s)",
            _credit_used, limit, _credit_used / limit * 100, source,
        )


def _get_request_cost(resp, *, ajax: bool) -> tuple[float, str]:
    """Use ZenRows' per-response cost header; fall back to a conservative estimate."""
    raw_cost = resp.headers.get("X-Request-Cost")
    if raw_cost is not None:
        try:
            cost = float(raw_cost)
            if cost >= 0:
                return cost, "X-Request-Cost"
        except (TypeError, ValueError):
            logger.warning("Unparseable ZenRows X-Request-Cost header: %r", raw_cost)
    # Fallback only: the actual mode=auto charge can be lower than this estimate.
    return (10.0 if ajax else 25.0), "fallback-estimate"


# ── CF challenge detection ───────────────────────────────────────────
# RENTCafe pages served through Cloudflare include challenge-platform
# script references even on REAL pages. We only flag actual CF
# interstitial/challenge pages by checking <title> and body markers.

_CF_TITLE_PATTERNS = ("just a moment", "attention required", "请稍候")
_CF_BODY_MARKERS = ("cf.errors.css",)


def _is_cf_challenge(html: str) -> bool:
    if not html or len(html) < 20:
        return False
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = title_match.group(1).strip().lower() if title_match else ""
    if any(p in title for p in _CF_TITLE_PATTERNS):
        return True
    lower = html.lower()
    if "cf.errors.css" in lower:
        return True
    if "you have been blocked" in lower and "ray id" in lower:
        return True
    if "cf-turnstile" in lower and len(html) < 5000:
        return True
    return False


# ── JS evaluate script for floorplan ID extraction ───────────────────
# Runs in browser context after JS rendering. Searches the DOM for
# floorplan IDs in onclick attrs, href anchors, data attributes, and
# script tags. Injects results as <span data-fp-id="NNN"> inside a
# hidden <div id="zenrows-fp-extract"> so Python regex can find them.
#
# This compensates for JS hydration stripping server-side onclick attrs
# (subPointerId=NNN) — the IDs are still in the JS runtime context but
# not in the static HTML source.
_FP_EXTRACT_JS = (
    "(function(){"
    "var ids=[];"
    # onclick attributes
    "document.querySelectorAll('[onclick]').forEach(function(el){"
    "var oc=el.getAttribute('onclick')||'';"
    "var m=oc.match(/subPointerId=(\\d+)/);if(m)ids.push(m[1]);"
    "m=oc.match(/myFloorPlanId=(\\d+)/);if(m)ids.push(m[1]);"
    "});"
    # href with #FP_Detail_NNNN
    "document.querySelectorAll('a[href*=\"FP_Detail_\"]').forEach(function(el){"
    "var m=el.getAttribute('href').match(/FP_Detail_(\\d+)/);if(m)ids.push(m[1]);"
    "});"
    # data attributes
    "document.querySelectorAll('[data-floorplanid]').forEach(function(el){"
    "var v=el.getAttribute('data-floorplanid');if(v)ids.push(v);"
    "});"
    # script tags
    "document.querySelectorAll('script').forEach(function(s){"
    "var t=s.textContent||'';"
    "var ms=t.matchAll(/subPointerId=(\\d{6,8})/g);for(var m of ms)ids.push(m[1]);"
    "ms=t.matchAll(/myFloorPlanId=(\\d{6,8})/g);for(var m of ms)ids.push(m[1]);"
    "ms=t.matchAll(/\"floorPlanId\"\\s*:\\s*(\\d+)/g);for(var m of ms)ids.push(m[1]);"
    "});"
    # inject
    "ids=[...new Set(ids)];"
    "if(ids.length>0){"
    "var d=document.createElement('div');d.id='zenrows-fp-extract';"
    "d.style.display='none';"
    "ids.forEach(function(id){"
    "var s=document.createElement('span');s.setAttribute('data-fp-id',id);"
    "d.appendChild(s);"
    "});"
    "document.body.appendChild(d);"
    "}"
    "})()"
)


class ZenRowsFetcher:
    """
    ZenRows Fetch API wrapper for RENTCafe HTML scraping.

    Interface (same as old BrightDataFetcher / RentCafeBrowserFetcher):
    - ``ensure_initialized(entry_url)`` — no-op (API is stateless)
    - ``fetch_html(url, referer, ajax)`` — GET HTML via ZenRows API
    - ``close()`` — no-op (no browser to close)
    - ``is_configured`` — True if ZENROWS_API_KEY is set
    - ``is_initialized`` — True after ensure_initialized called
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
        return bool(self._api_key)

    def __enter__(self) -> "ZenRowsFetcher":
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass

    def ensure_initialized(self, entry_url: str) -> None:
        from urllib.parse import urlparse
        parsed = urlparse(entry_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._entry_url = entry_url
        self._initialized = True

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

        Uses ``mode=auto`` which auto-selects JS rendering + premium proxy
        when needed for CF WAF bypass.

        For non-AJAX page fetches (floorplans.aspx), attaches
        ``js_instructions`` to extract floorplan IDs from the rendered DOM
        and inject them as ``<span data-fp-id="NNN">`` elements.

        Parameters
        ----------
        url         : Full URL to fetch
        referer     : Referer header (for AJAX calls)
        ajax        : If True, add X-Requested-With: XMLHttpRequest
        timeout_ms  : API timeout in milliseconds

        Raises
        ------
        BlockedError       API key missing, credits exhausted, or CF challenge
        RateLimitError     API rate limited (429)
        ScrapeNetworkError Network/API errors
        """
        if not self._api_key:
            raise _exc("BlockedError")(
                "ZenRows API key not set. Set ZENROWS_API_KEY. "
                "Free tier: 5000 credits/month, no KYC. "
                "Sign up: https://app.zenrows.com/register"
            )

        _maybe_reset_credits()
        local_limit = get_credit_limit()
        if local_limit > 0 and get_credit_usage() >= local_limit:
            raise _exc("BlockedError")(
                f"ZenRows process-local safety cap reached ({get_credit_usage():.2f}/"
                f"{local_limit}). This is not the account balance."
            )

        if not self._initialized:
            self.ensure_initialized(url)

        # Build params — mode=auto is the only reliable setting for CF WAF
        params: dict[str, str] = {
            "apikey": self._api_key,
            "url": url,
            "mode": "auto",
            "original_status": "true",
        }

        country = os.environ.get("ZENROWS_COUNTRY", "").strip()
        if country:
            params["proxy_country"] = country

        # Non-AJAX: attach js_instructions to extract floorplan IDs from DOM
        if not ajax:
            params["js_instructions"] = json.dumps([
                {"wait_for": "body"},
                {"evaluate": _FP_EXTRACT_JS},
            ])

        # AJAX: pass custom headers via custom_headers=true toggle
        req_headers: dict[str, str] = {}
        if ajax:
            params["custom_headers"] = "true"
            req_headers["X-Requested-With"] = "XMLHttpRequest"
            req_headers["Accept"] = "text/html, */*; q=0.01"
            if referer:
                req_headers["Referer"] = referer

        import requests as _requests

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
            raise _exc("ScrapeNetworkError")(f"ZenRows timeout ({api_timeout}s): {url}")
        except _requests.exceptions.ConnectionError as e:
            raise _exc("ScrapeNetworkError")(f"ZenRows connection error: {e}")

        elapsed = time.monotonic() - start

        # API-level errors
        if resp.status_code == 429:
            raise _exc("RateLimitError")("ZenRows rate limited (429)")

        if resp.status_code == 402:
            try:
                msg = resp.json().get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise _exc("BlockedError")(f"ZenRows 402 (credits exhausted): {msg}")

        if resp.status_code == 401:
            raise _exc("BlockedError")("ZenRows API key invalid (401)")

        if resp.status_code == 422:
            try:
                msg = resp.json().get("detail", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise _exc("ScrapeNetworkError")(f"ZenRows 422 (invalid request): {msg}")

        if resp.status_code >= 400:
            try:
                msg = resp.json().get("detail", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise _exc("ScrapeNetworkError")(f"ZenRows HTTP {resp.status_code}: {msg}")

        html = resp.text

        # ZenRows exposes the actual per-request charge in X-Request-Cost.
        # mode=auto may use fewer credits than the old fixed 25/10 estimates.
        request_cost, cost_source = _get_request_cost(resp, ajax=ajax)
        _consume_credit(request_cost, source=cost_source)

        logger.info(
            "ZenRows fetch: url=%s, status=%d, %.1fs, %d chars, "
            "request_cost=%.2f (%s), process_local_estimate=%.2f, local_cap=%s",
            url, resp.status_code, elapsed, len(html), request_cost, cost_source,
            get_credit_usage(), get_credit_limit() if get_credit_limit() > 0 else "disabled",
        )

        if _is_cf_challenge(html):
            raise _exc("BlockedError")(
                f"ZenRows returned CF challenge for {url} "
                "even with mode=auto"
            )

        if len(html) < 200:
            logger.warning("ZenRows short response (%d chars) for %s: %s", len(html), url, html[:200])

        return html
