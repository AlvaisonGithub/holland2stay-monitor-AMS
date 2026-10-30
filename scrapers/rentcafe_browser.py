"""
scrapers/rentcafe_browser.py — CloakBrowser-based RENTCafe HTML fetcher
=======================================================================

SecureRC (securerc.co.uk) hosts RENTCafe endpoints for OurDomain, OurCampus,
and Xior. Cloudflare has progressively tightened protection on these domains:
- Pre-2026: curl_cffi TLS impersonation could pass (cookie-based soft retry)
- 2026+: Cloudflare JS challenge (Turnstile-class) requires real browser JS
  execution — curl_cffi can no longer pass regardless of TLS fingerprint.

This module provides ``RentCafeBrowserFetcher`` — a CloakBrowser wrapper that:
1. Launches a patched Chromium (anti-fingerprint C++ patches)
2. Navigates to the RENTCafe floorplans URL (triggers + solves CF challenge)
3. Provides ``fetch_html(url)`` to GET any same-origin URL via browser fetch()
4. Manages browser lifecycle with cross-scrape-round reuse (like BrowserFetcher)

Thread safety
-------------
Each ``RentCafeBrowserFetcher`` instance binds to a single thread. The
OurDomain scraper creates instances per task within an executor thread.
"""
from __future__ import annotations

import logging
import platform
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Delayed exception loading (same pattern as browser_fetcher.py to avoid
# circular imports via scrapers.base ← scrapers.__init__ ← scrapers.ourdomain)
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


class RentCafeBrowserFetcher:
    """
    CloakBrowser wrapper for RENTCafe HTML scraping.

    Usage::

        with RentCafeBrowserFetcher(headless=True) as fetcher:
            html = fetcher.fetch_html("https://...securerc.co.uk/.../floorplans.aspx")
            unit_html = fetcher.fetch_html("https://...securerc.co.uk/.../rcLoadContent.ashx?...")

    The browser is initialized lazily on the first ``fetch_html`` call.
    After CF challenge passes, all subsequent same-origin requests go through
    ``page.evaluate(fetch())`` which carries CF clearance cookies automatically.

    Resource usage: ~190MB idle, ~280MB with active tabs.
    """

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._browser = None
        self._page = None
        self._initialized = False
        self._effective_headless = headless
        self._origin: Optional[str] = None  # e.g. "https://thisisourdomain.securerc.co.uk"
        self._entry_url: Optional[str] = None  # 原始 floorplans URL，用于 CF 重建

    # ── Context manager ──────────────────────────────────────────────
    def __enter__(self) -> "RentCafeBrowserFetcher":
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """Close browser, release resources."""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._page = None
            self._initialized = False
            self._origin = None
            self._entry_url = None

    # ── CF challenge initialization ──────────────────────────────────
    def ensure_initialized(self, entry_url: str) -> None:
        """
        Lazy init: navigate to the RENTCafe floorplans page to solve CF challenge.

        The entry_url must be on the same origin as all subsequent fetch_html
        calls. After CF passes, the browser has cf_clearance cookies that allow
        same-origin fetch() to work.
        """
        if self._initialized:
            return

        from cloakbrowser import launch

        if platform.system() == "Darwin" and self._effective_headless:
            logger.info("macOS CloakBrowser using headed mode to avoid headless SIGABRT")
            self._effective_headless = False

        chromium_args = []
        if platform.system() == "Linux":
            chromium_args = ["--disable-dev-shm-usage", "--disable-gpu"]
        elif platform.system() == "Windows":
            chromium_args = ["--no-sandbox"]

        self._browser = launch(
            headless=self._effective_headless,
            humanize=True,
            args=chromium_args,
        )
        self._page = self._browser.new_page()

        # Extract origin from entry_url for same-origin validation
        from urllib.parse import urlparse
        parsed = urlparse(entry_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._entry_url = entry_url  # 保存原始 URL，403 重建时用

        logger.info("RentCafeBrowserFetcher: navigating to %s (CF challenge)...", entry_url)
        start = time.monotonic()

        try:
            self._page.goto(
                entry_url,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
        except Exception as e:
            logger.error("RentCafe floorplans page load failed: %s", e)
            raise _exc("ScrapeNetworkError")(
                f"RENTCafe page load failed (CF challenge may not have passed): {e}"
            ) from e

        # ── 等 Cloudflare JS challenge 完成 ───────────────────────────
        # domcontentloaded 返回时页面可能还在 CF 挑战/拦截页。CF 有两种：
        #
        # 1. JS challenge ("Just a moment..." / "请稍候…")：CloakBrowser 的
        #    patched Chromium 能自动通过，需要等 JS 跑完（几秒到 30 秒）
        # 2. WAF firewall 403 ("Attention Required! | Cloudflare")：硬拦截，
        #    浏览器也无法通过，需要换 IP 或等 CF 冷却
        #
        # 等标题变成真实 RENTCafe 页面标题，排除所有 CF 相关标题。
        try:
            self._page.wait_for_function(
                "() => {"
                "  const t = document.title;"
                "  return t !== 'Just a moment...'"
                "    && t !== '请稍候…'"
                "    && t !== '请稍候...'"
                "    && !t.includes('moment')"
                "    && !t.includes('稍候')"
                "    && !t.includes('Attention Required')"
                "    && !t.toLowerCase().includes('cloudflare');"
                "}",
                timeout=30_000,
            )
            cf_elapsed = time.monotonic() - start
            logger.info(
                "CF challenge resolved (%.1fs), title: %r",
                cf_elapsed, self._page.title(),
            )
        except Exception:
            # 30 秒后标题仍然是 CF 相关 — 检查页面内容确认
            try:
                html = self._page.content()[:2000]
            except Exception:
                html = ""

            if self._is_cf_challenge(html):
                self._initialized = False
                raise _exc("BlockedError")(
                    "RENTCafe page stuck on Cloudflare challenge/block after 30s wait. "
                    "CloakBrowser may not be able to pass this challenge level."
                )

            # 不是 CF 挑战页但标题也没变 — 可能是特殊主题，继续执行
            logger.warning(
                "CF wait_for_function timed out but page is not CF challenge. "
                "Proceeding. Title: %r, content (first 200): %s",
                self._page.title(), html[:200],
            )

        # ── 等 RENTCafe 页面元素出现 ───────────────────────────────────
        # CF 通过后，RENTCafe 的 JS 还需要几秒 hydrate 页面。等 unit 表格
        # 或 floorplan 选择器出现。
        try:
            self._page.wait_for_selector(
                '[data-selenium-id], .floor-plan-selector, #unitrow_',
                timeout=15_000,
            )
        except Exception:
            # Selector might not match all RENTCafe themes — check page content
            try:
                html = self._page.content()[:2000]
            except Exception:
                html = ""

            if self._is_cf_challenge(html):
                self._initialized = False
                raise _exc("BlockedError")(
                    "RENTCafe page returned to CF challenge after initial pass"
                )

            # If it's not a CF challenge, the page might just use different
            # selectors. Log and proceed — fetch_html will tell us if it works.
            logger.warning(
                "RentCafe page selectors not found, but not CF challenge. "
                "Proceeding with fetch_html. Page content (first 500): %s",
                html[:500],
            )

        elapsed = time.monotonic() - start
        logger.info("RENTCafe page loaded (%.1fs)", elapsed)
        self._initialized = True

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def _is_cf_challenge(self, html: str) -> bool:
        """
        Check if page HTML is a Cloudflare challenge/interstitial/block page.

        CF 有两种拦截页面：
        1. JS challenge: 标题 "Just a moment..." / "请稍候…"，含
           challenge-platform script + turnstile widget
        2. WAF 403 block: 标题 "Attention Required! | Cloudflare"，含
           cf.errors.css 引用 + "Sorry, you have been blocked"

        Normal RENTCafe pages also contain <!--[if lt IE 7]> conditional
        comments, so "no-js ie6 oldie" alone is NOT a reliable CF indicator.
        """
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

    def _check_same_origin(self, url: str) -> None:
        """Ensure URL is same-origin as the initialized entry point."""
        if not self._origin:
            return
        from urllib.parse import urlparse
        parsed = urlparse(url)
        url_origin = f"{parsed.scheme}://{parsed.netloc}"
        if url_origin != self._origin:
            raise _exc("ScrapeNetworkError")(
                f"Cross-origin request rejected: {url} (expected origin {self._origin})"
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
        GET HTML from a same-origin RENTCafe URL via browser fetch().

        Parameters
        ----------
        url         : Full URL to fetch (must be same-origin as entry_url)
        referer     : Referer header to set (optional)
        ajax        : If True, add X-Requested-With: XMLHttpRequest
        timeout_ms  : fetch timeout in milliseconds

        Returns
        -------
        Response body as text (HTML string)

        Raises
        ------
        BlockedError       HTTP 403 (CF blocking)
        RateLimitError     HTTP 429
        ScrapeNetworkError Network/timeout errors or non-OK responses
        """
        if not self._initialized:
            # 如果 url 是 AJAX endpoint（不是 floorplans.aspx），用已保存的
            # entry_url 初始化；否则用 url 本身。
            init_url = self._entry_url or url
            if not init_url.endswith(".aspx") and ".ashx?" in init_url:
                # entry_url 也可能是 AJAX URL（罕见），无法更好选择就直接用 url
                init_url = url
            self.ensure_initialized(init_url)

        self._check_same_origin(url)

        headers: dict[str, str] = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers.update({
                "Accept": "text/html, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            })

        from json import dumps as _json_dumps
        headers_json = _json_dumps(headers)

        js_code = f"""
            async () => {{
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), {timeout_ms});
                try {{
                    const resp = await fetch({repr(url)}, {{
                        method: 'GET',
                        credentials: 'include',
                        mode: 'same-origin',
                        cache: 'no-store',
                        redirect: 'follow',
                        referrer: window.location.href,
                        referrerPolicy: 'strict-origin-when-cross-origin',
                        headers: {headers_json},
                        signal: controller.signal,
                    }});
                    clearTimeout(timeout);
                    const text = await resp.text();
                    return {{ status: resp.status, ok: resp.ok, text: text }};
                }} catch (err) {{
                    clearTimeout(timeout);
                    return {{ error: err.message || String(err) }};
                }}
            }}
        """

        result = self._page.evaluate(js_code)

        if "error" in result:
            raise _exc("ScrapeNetworkError")(f"Browser fetch failed: {result['error']}")

        status = result["status"]
        text = result.get("text", "")

        if status == 403:
            logger.warning(
                "RENTCafe fetch returned 403, attempting CF session rebuild... url=%s body=%s",
                url, text[:300],
            )
            self._initialized = False
            try:
                # 重建时导航到原始 floorplans URL（不是 AJAX endpoint），
                # 这样才能正确触发并解决 CF challenge。
                rebuild_url = self._entry_url or url
                self.ensure_initialized(rebuild_url)
            except Exception:
                raise _exc("BlockedError")(
                    "RENTCafe returned 403, CF session rebuild failed."
                )
            # Retry after rebuild
            result = self._page.evaluate(js_code)
            if "error" in result:
                raise _exc("ScrapeNetworkError")(f"Retry after rebuild failed: {result['error']}")
            status = result["status"]
            text = result.get("text", "")
            if status == 403:
                raise _exc("BlockedError")(
                    "RENTCafe persistent 403 after CF session rebuild."
                )

        if status == 429:
            raise _exc("RateLimitError")("RENTCafe returned 429 Too Many Requests")

        if status >= 400:
            raise _exc("ScrapeNetworkError")(
                f"RENTCafe HTTP {status}: {text[:300]}"
            )

        return text
