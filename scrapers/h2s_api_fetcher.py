"""
scrapers/h2s_api_fetcher.py — Holland2Stay GraphQL via ZenRows / BrightData
==========================================================================

Why
---
H2S moved its GraphQL API to ``www.holland2stay.com/api/graphql`` behind a
Cloudflare managed challenge. CloakBrowser no longer passes it reliably
("Just a moment…" page / GraphQL 403), so the H2S source got stuck in its
circuit breaker.

This module provides a drop-in replacement for ``BrowserFetcher`` (same
``fetch_gql`` / ``ensure_initialized`` / ``close`` interface) that sends the
GraphQL request through an unblocking API instead of a local browser:

1. ZenRows   (``ZENROWS_API_KEY``)     — POST with premium proxy, then GET + mode=auto
2. BrightData (``BRIGHTDATA_API_KEY``) — Web Unlocker POST, then GET

The first strategy that returns valid GraphQL JSON is remembered ("sticky")
so later rounds go straight to it and don't waste credits on failing ones.
A provider that answers 401/402 (bad key / no credits) or a BrightData KYC
error is disabled for the rest of the process.

It is used ONLY by ``scrapers/holland2stay.py``. OurDomain / Xior keep their
own fetchers untouched.

Env vars
--------
    H2S_FETCH_MODE=auto        # auto | zenrows | brightdata | browser
                               # auto = ZenRows → BrightData (whichever keys are set),
                               #        falls back to CloakBrowser if no key is set
    ZENROWS_API_KEY / ZENROWS_COUNTRY          (shared with OurDomain)
    BRIGHTDATA_API_KEY / BRIGHTDATA_ZONE / BRIGHTDATA_COUNTRY (shared)
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
from typing import Callable, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

H2S_GQL_URL = "https://www.holland2stay.com/api/graphql"
_H2S_REFERER = "https://www.holland2stay.com/residences"

# Tried in this order from inside the rendered H2S page
_INPAGE_ENDPOINTS = ["/api/remote", "/api/graphql"]

# Status label → available_to_book ID (same IDs as scrapers/holland2stay._STATUS_MAP)
_STATUS_LABELS: list[tuple[str, int]] = [
    # most specific first (English + Dutch site labels)
    ("to be in lottery", 6204),
    ("binnenkort in loting", 6204),
    ("binnenkort loting", 6204),
    ("available to book", 179),
    ("direct boeken", 179),
    ("direct te boeken", 179),
    ("book directly", 179),
    ("available in lottery", 336),
    ("in loting", 336),
    ("loting", 336),
    ("lottery", 336),
    ("coming soon", 6253),
    ("binnenkort", 6253),
    ("reserved", 6203),
    ("gereserveerd", 6203),
    ("occupied", 180),
    ("verhuurd", 180),
    ("bezet", 180),
]

# After a full GraphQL failure, skip GraphQL attempts for this long and go
# straight to reading the residences page (saves ~40s + credits per round).
_GQL_RETRY_AFTER = 6 * 3600

# Runs in the rendered /residences page: collects each residence card
_DOM_EXTRACT_JS = r"""
(function(){
  var out={cards:[],links:0,title:document.title,
           body:(document.body.innerText||'').replace(/\s+/g,' ').slice(0,400)};
  var seen={};
  document.querySelectorAll('a[href*="/residences/"]').forEach(function(a){
    var href=a.href.split('#')[0].split('?')[0];
    var m=href.match(/\/residences\/([^\/]+?)(?:\.html)?\/?$/);
    if(!m||!m[1])return;
    out.links++;
    if(seen[href])return; seen[href]=1;
    var el=a,card=a;
    for(var i=0;i<8&&el;i++){
      var hs={};el.querySelectorAll('a[href*="/residences/"]').forEach(function(x){
        var k=x.href.split('#')[0].split('?')[0];if(/\/residences\/[^\/]+/.test(k))hs[k]=1;});
      if(Object.keys(hs).length>1)break;
      card=el;
      el=el.parentElement;
    }
    var h=card.querySelector('h1,h2,h3,h4,h5,[class*="title" i],[class*="name" i]');
    out.cards.push({href:href,slug:m[1],
      title:((h&&h.innerText)||a.innerText||'').replace(/\s+/g,' ').trim().slice(0,160),
      text:(card.innerText||'').replace(/\s+/g,' ').trim().slice(0,1200)});
  });
  var maxp=1;
  document.querySelectorAll('a[href*="page="]').forEach(function(a){
    var m=a.href.match(/[?&]page=(\d+)/);if(m)maxp=Math.max(maxp,parseInt(m[1],10));});
  out.max_page=maxp;
  var d=document.createElement('pre');d.id='h2s-dom-result';d.style.display='none';
  d.textContent=JSON.stringify(out);document.body.appendChild(d);
})()
"""

_H2S_GQL_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Store": "default",
    "Content-Currency": "EUR",
    "Origin": "https://www.holland2stay.com",
    "Referer": _H2S_REFERER,
}

# ── Exceptions (lazy-loaded to avoid circular import) ────────────────
_exc_cache: dict[str, type] = {}


def _exc(name: str) -> type:
    if name not in _exc_cache:
        from scrapers.base import (
            BlockedError,
            RateLimitError,
            ScrapeNetworkError,
            UpstreamMaintenanceError,
        )
        _exc_cache.update({
            "BlockedError": BlockedError,
            "RateLimitError": RateLimitError,
            "ScrapeNetworkError": ScrapeNetworkError,
            "UpstreamMaintenanceError": UpstreamMaintenanceError,
        })
    return _exc_cache[name]


# ── Helpers ──────────────────────────────────────────────────────────

_CF_TITLE_PATTERNS = ("just a moment", "attention required", "请稍候")


def _looks_like_cf_challenge(text: str) -> bool:
    if not text:
        return False
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = m.group(1).strip().lower() if m else ""
    if any(p in title for p in _CF_TITLE_PATTERNS):
        return True
    lower = text.lower()
    return ("cf.errors.css" in lower) or ("you have been blocked" in lower and "ray id" in lower)


def _extract_gql_json(text: str) -> Optional[dict]:
    """
    Parse a GraphQL JSON response. When the unblocker rendered the response in
    a browser, the JSON comes wrapped as ``<html><body><pre>{...}</pre>``.
    Returns None if the text is not a GraphQL response.
    """
    if not text:
        return None
    candidate = text.strip()
    if not candidate.startswith("{"):
        m = re.search(r"<pre[^>]*>(.*?)</pre>", candidate, re.S | re.I)
        if m:
            candidate = _html.unescape(m.group(1)).strip()
        else:
            # Last resort: first {...} block that contains "data" or "errors"
            start = candidate.find('{"data"')
            if start < 0:
                start = candidate.find('{"errors"')
            if start < 0:
                return None
            candidate = _html.unescape(candidate[start:candidate.rfind("}") + 1])
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict) and ("data" in obj or "errors" in obj):
        return obj
    return None


from .base import ScrapeNetworkError as _ScrapeNetworkError  # noqa: E402


class H2SPageNotReady(_ScrapeNetworkError):
    """Residences page loaded but listings never rendered (slow load). Temporary."""


class _ProviderDisabled(Exception):
    """Provider can't be used this process (bad key, no credits, KYC)."""


class _StrategyFailed(Exception):
    """This strategy didn't yield GraphQL JSON; try the next one."""


# ── Fetcher ──────────────────────────────────────────────────────────

class H2SApiFetcher:
    """
    Remote (API-based) replacement for ``browser_fetcher.BrowserFetcher``
    for H2S read-only GraphQL queries.
    """

    ZENROWS_URL = "https://api.zenrows.com/v1/"
    BRIGHTDATA_URL = "https://api.brightdata.com/request"

    # Shared across instances: which strategy worked last, disabled providers
    _sticky_strategy: Optional[str] = None
    _disabled_providers: set[str] = set()
    _inpage_endpoint: Optional[str] = None
    _gql_dead_until: float = 0.0
    # Cheap (browser-only) ZenRows renders work → skip premium until this time.
    # Premium needed → use premium until this time, then re-test cheap.
    _premium_until: float = 0.0

    def __init__(self, headless: bool = True, providers: Optional[list[str]] = None):
        # headless kept for interface compatibility
        self._zr_key = os.environ.get("ZENROWS_API_KEY", "").strip() or None
        self._bd_key = os.environ.get("BRIGHTDATA_API_KEY", "").strip() or None
        self._bd_zone = os.environ.get("BRIGHTDATA_ZONE", "").strip() or "web_unlocker1"
        self._zr_country = os.environ.get("ZENROWS_COUNTRY", "").strip() or "nl"
        self._bd_country = os.environ.get("BRIGHTDATA_COUNTRY", "").strip() or "nl"
        self._providers = providers or ["zenrows", "brightdata"]
        self._initialized = False

    # ── interface compat ─────────────────────────────────────────────
    def __enter__(self) -> "H2SApiFetcher":
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        pass

    def ensure_initialized(self) -> None:
        if not self._strategies():
            raise _exc("BlockedError")(
                "H2S API fetcher: no usable provider (set ZENROWS_API_KEY or "
                "BRIGHTDATA_API_KEY, or check credits/KYC)."
            )
        self._initialized = True

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_configured(self) -> bool:
        return bool(self._strategies())

    @classmethod
    def any_key_configured(cls) -> bool:
        return bool(
            os.environ.get("ZENROWS_API_KEY", "").strip()
            or os.environ.get("BRIGHTDATA_API_KEY", "").strip()
        )

    # ── strategy list ────────────────────────────────────────────────
    def _strategies(self) -> list[tuple[str, Callable[[str, dict], str]]]:
        out: list[tuple[str, Callable[[str, dict], str]]] = []
        for p in self._providers:
            if p in self._disabled_providers:
                continue
            if p == "zenrows" and self._zr_key:
                # GET is not supported by H2S (/api/graphql GET → 404), so only POST paths
                out.append(("zenrows-render", self._zenrows_render))
                out.append(("zenrows-post", self._zenrows_post))
            elif p == "brightdata" and self._bd_key:
                out.append(("brightdata-post", self._brightdata_post))
        sticky = self._sticky_strategy
        if sticky and any(n == sticky for n, _ in out):
            out.sort(key=lambda s: 0 if s[0] == sticky else 1)
        return out

    # ── public API ───────────────────────────────────────────────────
    def fetch_gql(
        self,
        query: str,
        variables: dict | None = None,
        *,
        timeout_ms: int = 60_000,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        variables = variables or {}
        is_aggs = "aggregations" in query
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":"))

        # H2S's rebuilt site has no public GraphQL anymore, so by default go
        # straight to reading the residences page (H2S_TRY_GRAPHQL=true re-enables
        # the GraphQL attempts). Also used after a GraphQL failure.
        skip_gql = os.environ.get("H2S_TRY_GRAPHQL", "").strip().lower() != "true"
        if self._zr_key and "zenrows" not in self._disabled_providers \
                and (skip_gql or time.monotonic() < type(self)._gql_dead_until):
            if is_aggs:
                return {"data": {"products": {"aggregations": []}}}
            return self._dom_products(variables)

        strategies = self._strategies()
        if not strategies:
            raise _exc("BlockedError")(
                "H2S API fetcher: all providers disabled (missing key / credits / KYC)."
            )

        errors: list[str] = []
        saw_rate_limit = False
        for name, fn in strategies:
            provider = name.split("-")[0]
            if provider in self._disabled_providers:
                continue
            start = time.monotonic()
            try:
                text = fn(body, {"query": query, "variables": variables})
            except _ProviderDisabled as e:
                type(self)._disabled_providers.add(provider)
                logger.error("H2S %s disabled for this process: %s", provider, e)
                errors.append(f"{name}: {e}")
                continue
            except _exc("RateLimitError") as e:
                saw_rate_limit = True
                errors.append(f"{name}: {e}")
                continue
            except _StrategyFailed as e:
                logger.warning("H2S strategy %s failed (%.1fs): %s", name, time.monotonic() - start, e)
                errors.append(f"{name}: {e}")
                continue

            data = _extract_gql_json(text)
            if data is None:
                from scrapers.base import is_maintenance_body
                if is_maintenance_body(text[:4000]):
                    raise _exc("UpstreamMaintenanceError")("H2S 平台维护中")
                reason = "CF challenge page" if _looks_like_cf_challenge(text) else f"non-JSON body {text[:150]!r}"
                logger.warning("H2S strategy %s: %s", name, reason)
                errors.append(f"{name}: {reason}")
                continue

            if type(self)._sticky_strategy != name:
                logger.info("H2S GraphQL via %s OK (%.1fs) — using it as preferred strategy", name, time.monotonic() - start)
                type(self)._sticky_strategy = name
            else:
                logger.debug("H2S GraphQL via %s OK (%.1fs)", name, time.monotonic() - start)
            return data

        type(self)._sticky_strategy = None
        summary = " | ".join(errors)[:800]

        # Fallback: read listings straight from the rendered residences page
        if self._zr_key and "zenrows" not in self._disabled_providers:
            logger.warning("H2S GraphQL unreachable (%s) — falling back to reading the residences page", summary[:300])
            type(self)._gql_dead_until = time.monotonic() + _GQL_RETRY_AFTER
            if is_aggs:
                return {"data": {"products": {"aggregations": []}}}
            return self._dom_products(variables)

        if saw_rate_limit and all("429" in e or "rate" in e.lower() for e in errors):
            raise _exc("RateLimitError")(f"H2S API providers rate limited: {summary}")
        raise _exc("BlockedError")(f"H2S GraphQL: all API strategies failed: {summary}")

    # ── ZenRows ──────────────────────────────────────────────────────
    def _zenrows_check(self, resp) -> str:
        code = resp.status_code
        if code in (401, 402):
            raise _ProviderDisabled(f"ZenRows HTTP {code}: {resp.text[:200]}")
        if code == 429:
            raise _exc("RateLimitError")("ZenRows 429")
        if code >= 400:
            raise _StrategyFailed(f"ZenRows HTTP {code}: {resp.text[:200]}")
        cost = resp.headers.get("X-Request-Cost")
        if cost:
            try:
                from scrapers import zenrows_fetcher as _zf
                _zf._consume_credit(float(cost), source="X-Request-Cost(h2s)")
            except Exception:
                pass
        return resp.text

    def _dom_products(self, variables: dict) -> dict:
        """
        Render https://www.holland2stay.com/residences for one city in ZenRows'
        browser, read the residence cards, and return them in the GraphQL
        ``products`` shape that scrapers/holland2stay.py already parses.
        """
        import requests
        from urllib.parse import quote

        filters = (variables or {}).get("filters") or {}
        city_ids = [str(c) for c in ((filters.get("city") or {}).get("in") or [])]
        avail_ids = {int(a) for a in ((filters.get("available_to_book") or {}).get("in") or []) if str(a).isdigit()}
        page_no = int((variables or {}).get("currentPage") or 1)

        # New (Next.js) site format: ?page=1&city[filter]=Rotterdam,25
        names = _city_names_by_id()
        qs = [f"page={page_no}"]
        for c in city_ids:
            qs.append("city%5Bfilter%5D=" + quote(f"{names.get(c, c)},{c}"))
        page_url = _H2S_REFERER + "?" + "&".join(qs)

        start = time.monotonic()
        data: dict = {}
        cards: list = []
        # The listings render a few seconds after the page itself; on a slow
        # load the first read only sees the cookie banner. Retry once with a
        # longer wait before giving up.
        waits = [7000, 15000]
        for attempt, wait_ms in enumerate(waits, 1):
            instructions = [
                {"wait_for": "link[href*='/_next/'], script[src*='/_next/']"},
                {"wait": wait_ms},
                {"scroll_y": 4000},
                {"wait": 2000},
                {"scroll_y": 8000},
                {"wait": 2000},
                {"evaluate": _DOM_EXTRACT_JS},
            ]
            try:
                page = self._render(page_url, instructions, "h2s-dom-result")
            except _ProviderDisabled as e:
                type(self)._disabled_providers.add("zenrows")
                raise _exc("BlockedError")(f"H2S residences page: {e}")
            except _StrategyFailed as e:
                raise _exc("BlockedError")(f"H2S residences page: {e}")

            m = re.search(r'<pre[^>]*id="h2s-dom-result"[^>]*>(.*?)</pre>', page, re.S)
            if not m:
                if _looks_like_cf_challenge(page):
                    raise _exc("BlockedError")("H2S residences page: Cloudflare challenge not passed")
                raise _exc("BlockedError")(f"H2S residences page: extractor output missing ({len(page)} chars)")
            try:
                data = json.loads(_html.unescape(m.group(1)))
            except ValueError as e:
                raise _exc("ScrapeNetworkError")(f"H2S residences page: bad extractor JSON: {e}")

            cards = data.get("cards") or []
            if cards:
                break
            body_txt = (data.get("body") or "").lower()
            if re.search(r"geen (woningen|resultaten)|no (results|residences)|0 (resultaten|results)", body_txt):
                logger.info("H2S residences page: site reports no homes for city=%s", city_ids)
                return {"data": {"products": {"total_count": 0,
                        "page_info": {"current_page": page_no, "total_pages": page_no}, "items": []}}}
            if attempt < len(waits):
                logger.info("H2S residences page: listings not rendered yet (attempt %d), retrying with longer wait", attempt)

        if not cards:
            # Page loaded (CF passed) but listings never rendered: a temporary
            # hiccup, not a block. ScrapeNetworkError marks this round
            # incomplete without tripping the 30-min H2S circuit breaker, and
            # never reports a false "0 homes".
            raise H2SPageNotReady(
                f"H2S residences page: no residence links found after {len(waits)} tries "
                f"(url={page_url}, title={data.get('title')!r}, text={data.get('body', '')[:250]!r})"
            )

        # Make sure the city filter actually applied (otherwise we'd silently
        # report "0 homes" for a city we never looked at).
        wanted = [names[c].lower() for c in city_ids if c in names]
        if wanted:
            mentions_wanted = sum(1 for c in cards if any(w in (c.get("text") or "").lower() for w in wanted))
            if mentions_wanted == 0:
                raise _exc("BlockedError")(
                    f"H2S residences page: city filter not applied for {wanted} (url={page_url}); "
                    f"first card: {(cards[0].get('text') or '')[:150]!r}"
                )

        total_pages = max(page_no, int(data.get("max_page") or 1))

        # If no card's status could be recognised, our reading of the page is
        # off (e.g. new labels) — fail loudly instead of reporting "0 homes".
        if not any(_parse_status(c.get("text") or "") is not None for c in cards):
            raise _exc("BlockedError")(
                f"H2S residences page: no status label recognised on {len(cards)} cards "
                f"(url={page_url}); first card: {(cards[0].get('text') or '')[:200]!r}"
            )

        items = _cards_to_items(cards, city_ids, avail_ids)
        self._enrich_from_detail_pages(items)
        type(self)._sticky_strategy = "zenrows-dom"
        logger.info(
            "H2S residences page %d/%d read via ZenRows (%.1fs): %d cards, %d match city=%s status=%s",
            page_no, total_pages, time.monotonic() - start, len(cards), len(items), city_ids, sorted(avail_ids),
        )
        return {"data": {"products": {
            "total_count": len(items),
            "page_info": {"current_page": page_no, "total_pages": total_pages},
            "items": items,
        }}}

    _PREMIUM_STICKY = 6 * 3600

    def _render(self, url: str, js_instructions: list, marker: str, timeout: int = 150) -> str:
        """
        Load ``url`` in ZenRows' browser and return the HTML.

        Tries the cheap browser-only mode first (no premium proxy). If
        Cloudflare blocks it, retries with the premium residential proxy and
        keeps using premium for 6h before re-testing the cheap mode.
        ``marker`` is the id of the element our JS writes; its absence means
        the page didn't load properly.
        """
        import requests

        cls = type(self)
        modes = ["premium"] if time.monotonic() < cls._premium_until else ["cheap", "premium"]
        last_err = ""
        for mode in modes:
            params = {
                "apikey": self._zr_key,
                "url": url,
                "js_render": "true",
                "block_resources": "image,media,font",
                "js_instructions": json.dumps(js_instructions),
            }
            if mode == "premium":
                params["premium_proxy"] = "true"
                params["proxy_country"] = self._zr_country
            try:
                resp = requests.get(self.ZENROWS_URL, params=params, timeout=timeout)
            except requests.RequestException as e:
                raise _exc("ScrapeNetworkError")(f"ZenRows network error for {url}: {e}")
            try:
                page = self._zenrows_check(resp)
            except _StrategyFailed as e:
                last_err = str(e)
                if mode == "cheap":
                    logger.info("H2S cheap render failed (%s) — retrying with premium proxy", last_err[:150])
                    continue
                raise
            if f'id="{marker}"' not in page:
                last_err = "CF challenge page" if _looks_like_cf_challenge(page) else f"{marker} missing ({len(page)} chars)"
                if mode == "cheap":
                    logger.info("H2S cheap render incomplete (%s) — retrying with premium proxy", last_err)
                    continue
                return page  # caller reports the precise problem
            if mode == "premium" and "cheap" in modes:
                cls._premium_until = time.monotonic() + self._PREMIUM_STICKY
                logger.info("H2S: premium proxy needed — using it for the next %dh", self._PREMIUM_STICKY // 3600)
            elif mode == "cheap" and cls._premium_until:
                logger.info("H2S: cheap render works again — premium proxy no longer used")
                cls._premium_until = 0.0
            return page
        raise _StrategyFailed(last_err or "render failed")

    def _enrich_from_detail_pages(self, items: list[dict]) -> None:
        """Fill missing rent / start date from each residence's own page (cached 24h)."""
        try:
            max_fetch = int(os.environ.get("H2S_DETAIL_MAX_PER_ROUND", "5") or 5)
        except ValueError:
            max_fetch = 5
        now = time.monotonic()
        fetched = 0
        for it in items:
            if it.get("basic_rent") is not None and it.get("available_startdate"):
                continue
            url = it.get("_url")
            if not url:
                continue
            cached = _DETAIL_CACHE.get(url)
            if not cached or now - cached[0] > _DETAIL_TTL:
                if fetched >= max_fetch:
                    continue
                fetched += 1
                try:
                    rent, date = self._fetch_detail(url)
                except Exception as e:  # noqa: BLE001 — enrichment must never break the scan
                    logger.warning("H2S detail page failed for %s: %s", url, e)
                    # remember the failure so we don't pay for it every round
                    _DETAIL_CACHE[url] = (now - _DETAIL_TTL + _DETAIL_FAIL_RETRY, None, None)
                    continue
                cached = (now, rent, date)
                _DETAIL_CACHE[url] = cached
            _, rent, date = cached
            if it.get("basic_rent") is None and rent is not None:
                it["basic_rent"] = rent
            if not it.get("available_startdate") and date:
                it["available_startdate"] = date

    def _fetch_detail(self, url: str) -> tuple[Optional[float], Optional[str]]:
        start = time.monotonic()
        page = self._render(url, [
            {"wait_for": "link[href*='/_next/'], script[src*='/_next/']"},
            {"wait": 4000},
            {"evaluate": _DETAIL_JS},
        ], "h2s-detail-result", timeout=120)
        m = re.search(r'<pre[^>]*id="h2s-detail-result"[^>]*>(.*?)</pre>', page, re.S)
        if not m:
            raise _StrategyFailed("detail extractor output missing")
        text = json.loads(_html.unescape(m.group(1))).get("text") or ""
        rent, date = _parse_detail_rent(text), _parse_detail_date(text)
        logger.info("H2S detail page %s (%.1fs): rent=%s available_from=%s", url, time.monotonic() - start, rent, date)
        if rent is None:
            i = text.lower().find("\u20ac")
            logger.info("H2S detail page without recognised rent, text around first \u20ac: %r",
                        text[max(0, i - 200):i + 200] if i >= 0 else text[:400])
        return rent, date

    def _zenrows_post(self, body: str, _gql: dict) -> str:
        """POST forwarded as-is through a NL residential proxy (no JS render)."""
        import requests
        params = {
            "apikey": self._zr_key,
            "url": H2S_GQL_URL,
            "premium_proxy": "true",
            "proxy_country": self._zr_country,
            "custom_headers": "true",
        }
        try:
            resp = requests.post(self.ZENROWS_URL, params=params, data=body.encode(),
                                 headers=_H2S_GQL_HEADERS, timeout=60)
        except requests.RequestException as e:
            raise _StrategyFailed(f"network: {e}")
        return self._zenrows_check(resp)

    def _zenrows_render(self, body: str, _gql: dict) -> str:
        """
        Same idea as the old CloakBrowser path, but in ZenRows' browser:
        load the H2S site (ZenRows solves the CF challenge), then run the
        GraphQL POST from inside the page with fetch() and write the result
        into a <pre id="h2s-gql-result"> element that we read back.
        """
        import requests
        headers_js = json.dumps({k: v for k, v in _H2S_GQL_HEADERS.items()
                                 if k in ("Accept", "Content-Type", "Store", "Content-Currency")})
        endpoints_js = json.dumps(_INPAGE_ENDPOINTS)
        js = (
            "(function(){var d=document.createElement('pre');d.id='h2s-gql-pending';"
            "d.style.display='none';document.body.appendChild(d);"
            # discover the API calls the H2S page itself made (Next.js rebuild moved the API)
            "var seen={},disc=[],apis=[];"
            "try{performance.getEntriesByType('resource').forEach(function(e){var u=e.name;"
            "if(seen[u]||/\\/_next\\/static|\\.(js|css|png|jpe?g|svg|webp|woff2?|ico)(\\?|$)|google|facebook|hotjar|clarity|cookie/i.test(u))return;seen[u]=1;"
            "if(/graphql/i.test(u))disc.push(u.split('?')[0]);"
            "if(/\\/api\\/|graphql|magento|rest\\//i.test(u))apis.push(e.initiatorType+':'+u.slice(0,160));});}catch(e){}"
            "d.setAttribute('data-discovered',apis.slice(0,25).join(' | '));"
            "var eps=disc.concat(" + endpoints_js + ").filter(function(v,i,a){return a.indexOf(v)==i;}),log=[];"
            "function done(st,t,ep){d.setAttribute('data-status',st);d.setAttribute('data-endpoint',ep);"
            "d.textContent=t;d.id='h2s-gql-result';}"
            "function tryAt(i){if(i>=eps.length){done(0,'FETCH_ERROR '+log.join(' ; '),'');return;}"
            "var ep=eps[i];"
            "fetch(ep,{method:'POST',credentials:(ep.charAt(0)=='/'?'include':'omit'),headers:" + headers_js
            + ",body:" + json.dumps(body) + "})"
            ".then(function(r){return r.text().then(function(t){"
            "var s=t.replace(/^\\s+/,'');"
            "if(s.charAt(0)=='{'&&(s.indexOf('\"data\"')>=0||s.indexOf('\"errors\"')>=0)){done(r.status,t,ep);}"
            "else{log.push(ep+' HTTP '+r.status+' '+t.slice(0,120));tryAt(i+1);}});})"
            ".catch(function(e){log.push(ep+' '+e);tryAt(i+1);});}"
            "tryAt(0);})()"
        )
        params = {
            "apikey": self._zr_key,
            "url": _H2S_REFERER,
            "js_render": "true",
            "premium_proxy": "true",
            "proxy_country": self._zr_country,
            "block_resources": "image,media,font",
            "js_instructions": json.dumps([
                # real H2S (Next.js) page loaded = CF challenge passed
                {"wait_for": "link[href*='/_next/'], script[src*='/_next/']"},
                {"wait": 4000},  # let the page make its own API calls
                {"evaluate": js},
                {"wait_for": "#h2s-gql-result"},
            ]),
        }
        try:
            resp = requests.get(self.ZENROWS_URL, params=params, timeout=120)
        except requests.RequestException as e:
            raise _StrategyFailed(f"network: {e}")
        page = self._zenrows_check(resp)
        m = re.search(r'<pre[^>]*id="h2s-gql-result"[^>]*>(.*?)</pre>', page, re.S)
        if not m:
            reason = "CF challenge page" if _looks_like_cf_challenge(page) else "result element missing"
            raise _StrategyFailed(f"render: {reason} ({len(page)} chars)")
        inner = _html.unescape(m.group(1)).strip()
        st = re.search(r'data-status="(\d+)"', m.group(0))
        disc = re.search(r'data-discovered="([^"]*)"', m.group(0))
        disc_txt = _html.unescape(disc.group(1)) if disc else ""
        logger.info("H2S page API calls seen: %s", disc_txt or "(none)")
        if inner.startswith("FETCH_ERROR"):
            raise _StrategyFailed(f"in-page fetch failed: {inner[:400]} || page API calls: {disc_txt[:800] or 'none'}")
        ep = re.search(r'data-endpoint="([^"]*)"', m.group(0))
        if ep and type(self)._inpage_endpoint != ep.group(1):
            type(self)._inpage_endpoint = ep.group(1)
            logger.info("H2S in-page GraphQL endpoint: %s", ep.group(1))
        if st and st.group(1) == "429":
            raise _exc("RateLimitError")("H2S 429 (in-page fetch via ZenRows)")
        if _extract_gql_json(inner) is None:
            raise _StrategyFailed(f"in-page fetch HTTP {st.group(1) if st else '?'}: {inner[:150]!r}")
        return inner

    def _zenrows_get(self, _body: str, gql: dict) -> str:
        """GraphQL-over-GET with mode=auto (JS render + premium proxy as needed)."""
        import requests
        target = H2S_GQL_URL + "?" + urlencode({
            "query": gql["query"],
            "variables": json.dumps(gql["variables"], separators=(",", ":")),
        })
        params = {
            "apikey": self._zr_key,
            "url": target,
            "mode": "auto",
            "proxy_country": self._zr_country,
        }
        try:
            resp = requests.get(self.ZENROWS_URL, params=params, timeout=90)
        except requests.RequestException as e:
            raise _StrategyFailed(f"network: {e}")
        return self._zenrows_check(resp)

    # ── BrightData ───────────────────────────────────────────────────
    def _brightdata_call(self, payload: dict, custom_headers: Optional[dict] = None) -> str:
        import requests
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._bd_key}",
        }
        if custom_headers:
            headers["x-unblock-custom-headers"] = json.dumps(custom_headers)
        try:
            resp = requests.post(self.BRIGHTDATA_URL, headers=headers, json=payload, timeout=90)
        except requests.RequestException as e:
            raise _StrategyFailed(f"network: {e}")
        try:
            from scrapers import brightdata_fetcher as _bf
            _bf._consume_credit(1)
        except Exception:
            pass

        code = resp.status_code
        text = resp.text or ""
        lower = text.lower()
        if code in (401, 402) or (code >= 400 and ("kyc" in lower or "verification" in lower)):
            raise _ProviderDisabled(f"BrightData HTTP {code}: {text[:200]}")
        if code == 429:
            raise _exc("RateLimitError")("BrightData 429")
        if code >= 400:
            raise _StrategyFailed(f"BrightData HTTP {code}: {text[:200]}")

        # format=json → {"status_code":..,"headers":..,"body":".."}
        try:
            wrapped = resp.json()
        except ValueError:
            return text
        if isinstance(wrapped, dict) and "body" in wrapped:
            target_status = wrapped.get("status_code")
            inner = wrapped.get("body") or ""
            if target_status == 429:
                raise _exc("RateLimitError")("H2S 429 via BrightData")
            if isinstance(target_status, int) and target_status >= 400 and _extract_gql_json(inner) is None:
                raise _StrategyFailed(f"target HTTP {target_status}: {inner[:150]!r}")
            return inner if isinstance(inner, str) else json.dumps(inner)
        return text

    def _brightdata_post(self, body: str, _gql: dict) -> str:
        payload = {
            "zone": self._bd_zone,
            "url": H2S_GQL_URL,
            "format": "json",
            "method": "POST",
            "body": body,
            "country": self._bd_country,
        }
        return self._brightdata_call(payload, custom_headers={
            k: v for k, v in _H2S_GQL_HEADERS.items()
        })

    def _brightdata_get(self, _body: str, gql: dict) -> str:
        target = H2S_GQL_URL + "?" + urlencode({
            "query": gql["query"],
            "variables": json.dumps(gql["variables"], separators=(",", ":")),
        })
        payload = {
            "zone": self._bd_zone,
            "url": target,
            "format": "json",
            "method": "GET",
            "country": self._bd_country,
        }
        return self._brightdata_call(payload)


def _city_names_by_id() -> dict[str, str]:
    try:
        from config import KNOWN_CITIES
        return {str(c["id"]): c["name"] for c in KNOWN_CITIES}
    except Exception:  # noqa: BLE001
        return {"24": "Amsterdam", "110": "Diemen"}


def _parse_status(text: str) -> Optional[int]:
    low = text.lower()
    for label, sid in _STATUS_LABELS:
        if label in low:
            return sid
    return None


def _parse_price(text: str) -> Optional[float]:
    # Drop the "*servicekosten ... €335,00 per maand" footnote: that's not the rent
    text = re.split(r"\*?\s*(servicekosten|service costs)", text, flags=re.I)[0]
    m = re.search(r"\u20ac\s*([0-9][0-9.,]*)", text)
    if not m:
        return None
    raw = m.group(1).rstrip(".,")
    # "1.395" / "1,395" / "1.395,00" / "1395.50"
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+([.,]\d{1,2})?", raw):
        raw = re.sub(r"[.,](\d{1,2})$", r"", raw)
        raw = re.sub(r"[.,]", "", raw)
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _parse_date(text: str) -> Optional[str]:
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", text)
    if m and m.group(2).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def _parse_area(text: str) -> Optional[str]:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:2|²)", text)
    return m.group(1).replace(",", ".") if m else None


def _parse_card_features(text: str) -> dict:
    """Type / floor / energy / occupancy from Dutch or English card text (shown as-is)."""
    out: dict = {}
    m = re.search(r"\b(studio|\d+\s*(?:slaapkamers?|bedrooms?|kamers?|rooms?))\b", text, re.I)
    if m:
        out["no_of_rooms"] = m.group(1)
    m = re.search(r"\b(\d+(?:e|de|ste|st|nd|rd|th)?\s*(?:verdieping|floor)|begane grond|ground floor)\b", text, re.I)
    if m:
        out["floor"] = m.group(1)
    m = re.search(r"(?:energielabel|energy label)\s*([A-G]\+{0,4})", text, re.I)
    if m:
        out["energy_label"] = m.group(1).upper()
    m = re.search(r"\b(single of koppel|single or couple|single|koppel|couple|\d+\s*(?:personen|persons|people))\b", text, re.I)
    if m:
        out["maximum_number_of_persons"] = m.group(1)
    return out


# Rent / date from a residence's own page, keyed by url → (time, rent, date)
_DETAIL_CACHE: dict[str, tuple[float, Optional[float], Optional[str]]] = {}
_DETAIL_TTL = 72 * 3600          # rent/date rarely change
_DETAIL_FAIL_RETRY = 6 * 3600    # after a failed lookup, wait this long before paying again

_DETAIL_JS = r"""
(function(){
  var d=document.createElement('pre');d.id='h2s-detail-result';d.style.display='none';
  d.textContent=JSON.stringify({title:document.title,
    text:(document.body.innerText||'').replace(/[ \t]+/g,' ').slice(0,12000)});
  document.body.appendChild(d);
})()
"""

_RENT_KEYS = [
    r"kale huur|basishuur|basic rent|base rent",
    r"huurprijs|huur per maand|rent per month|monthly rent",
    r"totale huur|total rent|totaal per maand|total per month",
    r"huur|rent|prijs|price",
]


def _parse_detail_rent(text: str) -> Optional[float]:
    for keys in _RENT_KEYS:
        for m in re.finditer(rf"(?:{keys})[^\u20ac]{{0,60}}\u20ac\s*([0-9][0-9.,]*)", text, re.I):
            # skip service-cost lines
            line_start = text.rfind("\n", 0, m.start()) + 1
            if re.search(r"service", text[line_start:m.end()], re.I):
                continue
            val = _parse_price("\u20ac" + m.group(1))
            if val and val >= 200:
                return val
    return None


def _parse_detail_date(text: str) -> Optional[str]:
    m = re.search(r"(beschikbaar vanaf|beschikbaar per|available from|startdatum|ingangsdatum|start date)", text, re.I)
    if m:
        return _parse_date(text[m.end():m.end() + 60])
    return None


def _cards_to_items(cards: list[dict], city_ids: list[str], avail_ids: set[int]) -> list[dict]:
    """Turn residence cards read from the page into GraphQL-like product items."""
    names = _city_names_by_id()
    wanted = [names[c].lower() for c in city_ids if c in names]
    all_city_names = [n.lower() for n in names.values()]
    any_mentions_city = any(
        any(n in (c.get("text") or "").lower() for n in all_city_names)
        for c in cards
    )

    status_counts: dict[str, int] = {}
    items: list[dict] = []
    for c in cards:
        text = c.get("text") or ""
        low = text.lower()
        if wanted and any_mentions_city and not any(w in low for w in wanted):
            continue
        sid = _parse_status(text)
        status_counts[str(sid)] = status_counts.get(str(sid), 0) + 1
        if avail_ids and sid not in avail_ids:
            continue
        price = _parse_price(text)
        item = {
            "url_key": c.get("slug") or "",
            "sku": "",
            "name": c.get("title") or c.get("slug") or "",
            "available_to_book": sid,
            "available_startdate": _parse_date(text),
            "_url": c.get("href") or "",
        }
        if price is not None:
            item["basic_rent"] = price
        area = _parse_area(text)
        if area:
            item["living_area"] = area
        item.update(_parse_card_features(text))
        if city_ids:
            item["city"] = city_ids[0]
        items.append(item)
    logger.info("H2S residences page statuses seen (status_id→count): %s; city filter by text=%s",
                status_counts, bool(wanted and any_mentions_city))
    if cards and not items:
        logger.info("H2S residences page sample card: %s", (cards[0].get("text") or "")[:300])
    for it in items:
        if it.get("basic_rent") is None:
            logger.info("H2S card without rent (%s): %s", it.get("url_key"),
                        next((c.get("text") for c in cards if c.get("slug") == it.get("url_key")), "")[:400])
            break
    return items


def resolve_h2s_fetch_mode() -> str:
    """Return the effective H2S fetch mode: 'api' or 'browser'."""
    mode = (os.environ.get("H2S_FETCH_MODE", "auto") or "auto").strip().lower()
    if mode == "browser":
        return "browser"
    if mode in ("zenrows", "brightdata", "api"):
        return "api"
    return "api" if H2SApiFetcher.any_key_configured() else "browser"


def make_api_fetcher() -> H2SApiFetcher:
    mode = (os.environ.get("H2S_FETCH_MODE", "auto") or "auto").strip().lower()
    if mode == "zenrows":
        return H2SApiFetcher(providers=["zenrows"])
    if mode == "brightdata":
        return H2SApiFetcher(providers=["brightdata"])
    return H2SApiFetcher()
