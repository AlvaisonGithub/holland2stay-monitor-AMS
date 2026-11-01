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
_INPAGE_ENDPOINTS = ["/api/graphql", "https://api.holland2stay.com/graphql"]

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
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":"))
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
            "var eps=" + endpoints_js + ",log=[];"
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
        if inner.startswith("FETCH_ERROR"):
            raise _StrategyFailed(f"in-page fetch failed: {inner[:400]}")
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
