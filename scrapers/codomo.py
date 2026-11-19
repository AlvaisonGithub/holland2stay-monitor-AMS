"""
scrapers/codomo.py — Holland2Stay (now Codomo) listings from codomo.eu
=====================================================================

In November 2026 Holland2Stay moved to **Codomo** (https://codomo.eu). The
listing search page is server-rendered and, unlike the old site, is not
behind a Cloudflare challenge:

    https://codomo.eu/homes/nl/en?city=Amsterdam&page=1     (25 homes/page)

Each home is an ``<article>`` with address + link, details (type, m², persons,
furnished, floor, building), available-from date, rent, service costs and the
booking action:

    "Book directly"   → available to book            (old H2S status 179)
    "Show interest"   → register interest / lottery  (old H2S status 336)

Next.js streams parts of the page: content may sit in hidden
``<div hidden id="S:x">`` blocks with ``<template id="P:x">`` / ``"B:x"``
placeholders where it belongs. The parser finds every ``<article>`` wherever
it is and fills placeholders back in recursively, so both the "flat" and the
"streamed" variants of the page parse the same way.

Transport (first that works is remembered):
  1. direct  — curl_cffi with a browser TLS fingerprint (free)
  2. zenrows — plain HTML fetch, no JS rendering (cheap), then premium proxy
  3. brightdata — Web Unlocker GET

Only standard-library HTML parsing is used.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

CODOMO_ORIGIN = "https://codomo.eu"
CODOMO_SEARCH = CODOMO_ORIGIN + "/homes/nl/en"
PAGE_SIZE = 25

# Booking action label → old H2S available_to_book ID
ACTION_STATUS: dict[str, int] = {
    "book directly": 179,
    "show interest": 336,
    "register interest": 336,
}

# KNOWN_CITIES names that differ from Codomo's city names
_CITY_NAME_OVERRIDES = {
    "The Hague": "Den Haag",
}

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}

_CF_TITLES = ("just a moment", "attention required", "请稍候")


# ── Exceptions (lazy, avoid circular import) ─────────────────────────

def _exc(name: str) -> type:
    from scrapers import base
    return getattr(base, name)


# ── Minimal DOM ──────────────────────────────────────────────────────

class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: Optional["_Node"]):
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = parent

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, _Node):
                yield from c.iter()

    def find_all(self, tag: str) -> list["_Node"]:
        return [n for n in self.iter() if n.tag == tag]

    def text(self, skip: tuple = ()) -> str:
        out: list[str] = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag not in skip:
                out.append(c.text(skip))
        return "".join(out)


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root
        self.by_id: dict[str, _Node] = {}

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        nid = node.attrs.get("id")
        if nid:
            self.by_id.setdefault(nid, node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.cur.tag == tag:
            self.cur = self.cur.parent or self.root

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _parse_html(html: str) -> _TreeBuilder:
    b = _TreeBuilder()
    b.feed(html)
    b.close()
    return b


def _resolve_placeholders(node: _Node, by_id: dict, depth: int = 0) -> None:
    """Replace <template id="P:x"/"B:x"> with the content of <div hidden id="S:x">."""
    if depth > 12:
        return
    new_children: list = []
    for c in node.children:
        if isinstance(c, _Node) and c.tag == "template":
            tid = c.attrs.get("id", "")
            src = by_id.get("S:" + tid[2:]) if tid[:2] in ("P:", "B:") else None
            if src is not None:
                for sc in src.children:
                    if isinstance(sc, _Node):
                        clone = _clone(sc, node)
                        _resolve_placeholders(clone, by_id, depth + 1)
                        new_children.append(clone)
                    else:
                        new_children.append(sc)
                continue
        if isinstance(c, _Node):
            _resolve_placeholders(c, by_id, depth)
        new_children.append(c)
    node.children = new_children


def _clone(n: _Node, parent: _Node) -> _Node:
    c = _Node(n.tag, dict(n.attrs), parent)
    c.children = [(_clone(x, c) if isinstance(x, _Node) else x) for x in n.children]
    return c


# ── Page parsing ─────────────────────────────────────────────────────

def looks_like_cf_challenge(html: str) -> bool:
    if not html:
        return False
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = (m.group(1).strip().lower() if m else "")
    if any(t in title for t in _CF_TITLES):
        return True
    low = html[:20000].lower()
    return "cf.errors.css" in low or ("you have been blocked" in low and "ray id" in low)


def looks_like_search_page(html: str) -> bool:
    """A real Codomo search page (with or without results)."""
    return bool(html) and "codomo" in html.lower() and (
        "<article" in html or re.search(r"\b\d+\s+residences?\b", html) is not None
    )


_DATE_RE = re.compile(r"^[A-Z][a-z]{2,8} \d{1,2}, \d{4}$")
_PRICE_RE = re.compile(r"^€\s?[0-9][0-9.,]*$")


def _parse_date(s: str) -> Optional[str]:
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_eur(s: str) -> Optional[float]:
    raw = s.replace("€", "").strip().rstrip(".,")
    # English site: "2,053" / "1,234.50"; tolerate Dutch "1.234,50"
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", raw):
        raw = raw.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", raw):
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _texts(node: _Node, skip: tuple = ("details",)) -> list[str]:
    out: list[str] = []

    def walk(n: _Node):
        for c in n.children:
            if isinstance(c, str):
                t = " ".join(c.split())
                if t:
                    out.append(t)
            elif c.tag not in skip:
                walk(c)
    walk(node)
    return out


def _parse_article(art: _Node) -> Optional[dict]:
    h3 = next(iter(art.find_all("h3")), None)
    link = next(iter(h3.find_all("a")), None) if h3 else None
    href = link.attrs.get("href", "") if link else ""
    if not href or "/homes/" not in href:
        return None
    path = href.split("?")[0].rstrip("/")
    parts = path.split("/")
    # /homes/nl/en/{city}/{building}/{unit}
    city_slug = parts[4] if len(parts) > 6 else ""
    unit_slug = parts[-1]
    title = " ".join(link.text().split())

    card: dict = {
        "href": href,
        "url": CODOMO_ORIGIN + href if href.startswith("/") else href,
        "slug": unit_slug,
        "city_slug": city_slug,
        "building_slug": parts[5] if len(parts) > 6 else "",
        "title": title,
    }

    # Feature list items (outside the mobile-only <details> duplicate)
    feats: list[str] = []
    for ul in art.find_all("ul"):
        if _inside(ul, "details"):
            continue
        for li in ul.children:
            if isinstance(li, _Node) and li.tag == "li":
                t = " ".join(li.text().split())
                t = re.sub(r"(\d)\s*m\s*2\b", r"\1 m²", t)
                if t:
                    feats.append(t)
    for t in feats:
        low = t.lower()
        if re.fullmatch(r"\d+(?:[.,]\d+)?\s*m²", t):
            card["area"] = t.replace(" m²", "").replace(",", ".")
        elif re.fullmatch(r"\d+\s+persons?", low):
            card["persons"] = t
        elif low in ("furnished", "unfurnished", "semi-furnished", "upholstered", "shell"):
            card["finishing"] = t
        elif re.fullmatch(r"\d+\.?\s*floor|ground floor|basement", low):
            card["floor"] = t
        elif re.fullmatch(r"studio|loft|maisonette|apartment|room|\d+\s+bedrooms?", low):
            card["rooms"] = t
        elif low in ("book a viewing",):
            continue
        else:
            card.setdefault("building", t)

    texts = _texts(art)
    for i, t in enumerate(texts):
        if "date" not in card and _DATE_RE.match(t):
            card["date"] = _parse_date(t)
            if i + 1 < len(texts):
                card["availability"] = texts[i + 1]
        elif "price" not in card and _PRICE_RE.match(t):
            card["price"] = _parse_eur(t)
        elif t.startswith("*Excl.") and "service" not in card:
            card["service"] = t.lstrip("*")
        m = re.match(r"([\d,.]+)\s+people have shown interest", t)
        if m:
            card["interest_count"] = int(re.sub(r"[^\d]", "", m.group(1)) or 0)

    # Booking action = last link in the card that isn't the title link
    action = ""
    for a in art.find_all("a"):
        if a is link:
            continue
        t = " ".join(a.text().split())
        if t:
            action = t
    card["action"] = action
    card["status_id"] = ACTION_STATUS.get(action.lower())
    return card


def _inside(n: _Node, tag: str) -> bool:
    p = n.parent
    while p is not None:
        if p.tag == tag:
            return True
        p = p.parent
    return False


def parse_search_page(html: str) -> dict:
    """
    Returns {"cards": [...], "total": int|None, "max_page": int, "title": str}.
    Cards are de-duplicated by link.
    """
    b = _parse_html(html)
    cards: list[dict] = []
    seen: set[str] = set()
    for art in b.root.find_all("article"):
        work = _clone(art, art.parent or b.root)
        _resolve_placeholders(work, b.by_id)
        card = _parse_article(work)
        if card and card["href"] not in seen:
            seen.add(card["href"])
            cards.append(card)

    total = None
    for n in b.root.iter():  # the result counter: <p role="status">7 residences</p>
        if n.attrs.get("role") == "status":
            m = re.search(r"(\d+)\s+residences?\b", " ".join(n.text().split()))
            if m:
                total = int(m.group(1))
                break
    if total is None:
        joined = " | ".join(_texts(b.root, skip=("script", "style")))
        m = re.search(r"(?:^|\|\s)(\d+)\s+residences?\b", joined)
        total = int(m.group(1)) if m else None
    pages = [int(p) for p in re.findall(r"[?&]page=(\d+)", html)]
    max_page = max(pages) if pages else 1
    if total is not None:
        max_page = max(max_page, math.ceil(total / PAGE_SIZE) if total else 1)
    tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return {"cards": cards, "total": total, "max_page": max_page,
            "title": (tm.group(1).strip() if tm else "")}


# ── City mapping / URLs ──────────────────────────────────────────────

def city_param(city_id: str) -> tuple[str, str]:
    """KNOWN_CITIES id → (Codomo city name for ?city=, URL slug)."""
    try:
        from config import KNOWN_CITIES
        names = {str(c["id"]): c["name"] for c in KNOWN_CITIES}
    except Exception:  # noqa: BLE001
        names = {"24": "Amsterdam", "110": "Diemen"}
    name = names.get(str(city_id), str(city_id))
    name = _CITY_NAME_OVERRIDES.get(name, name)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return name, slug


def search_url(city_name: str, page: int = 1) -> str:
    q = f"?city={quote(city_name)}"
    if page > 1:
        q += f"&page={page}"
    return CODOMO_SEARCH + q


# ── Transport ────────────────────────────────────────────────────────

class CodomoFetcher:
    """Fetch Codomo pages: direct first (free), then ZenRows / Bright Data."""

    ZENROWS_URL = "https://api.zenrows.com/v1/"
    BRIGHTDATA_URL = "https://api.brightdata.com/request"
    _sticky: Optional[str] = None          # transport that last worked
    _direct_blocked_until: float = 0.0     # skip direct for a while after a block
    _DIRECT_RETRY = 30 * 60

    def __init__(self):
        self._zr_key = os.environ.get("ZENROWS_API_KEY", "").strip() or None
        self._bd_key = os.environ.get("BRIGHTDATA_API_KEY", "").strip() or None
        self._bd_zone = os.environ.get("BRIGHTDATA_ZONE", "").strip() or "web_unlocker1"
        self._country = os.environ.get("ZENROWS_COUNTRY", "").strip() or "nl"

    def _transports(self) -> list[str]:
        mode = (os.environ.get("H2S_FETCH_MODE", "auto") or "auto").strip().lower()
        order: list[str] = []
        if mode in ("auto", "direct", "api", "browser") and time.monotonic() >= type(self)._direct_blocked_until:
            order.append("direct")
        if self._zr_key and mode in ("auto", "zenrows", "api"):
            order += ["zenrows", "zenrows-premium"]
        if self._bd_key and mode in ("auto", "brightdata", "api"):
            order.append("brightdata")
        if not order:  # direct was skipped after a block and no keys: try it anyway
            order.append("direct")
        st = type(self)._sticky
        if st in order:
            order.remove(st)
            order.insert(0, st)
        return order

    def get(self, url: str) -> str:
        errors: list[str] = []
        rate_limited = False
        for t in self._transports():
            start = time.monotonic()
            try:
                html = getattr(self, "_get_" + t.replace("-", "_"))(url)
            except _exc("RateLimitError") as e:
                rate_limited = True
                errors.append(f"{t}: {e}")
                continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"{t}: {type(e).__name__}: {str(e)[:150]}")
                continue
            if looks_like_cf_challenge(html) or not looks_like_search_page(html):
                reason = "Cloudflare challenge" if looks_like_cf_challenge(html) else f"unexpected page ({len(html)} chars)"
                errors.append(f"{t}: {reason}")
                if t == "direct":
                    type(self)._direct_blocked_until = time.monotonic() + self._DIRECT_RETRY
                continue
            if type(self)._sticky != t:
                logger.info("Codomo: fetching via %s (%.1fs)", t, time.monotonic() - start)
                type(self)._sticky = t
            return html
        type(self)._sticky = None
        summary = " | ".join(errors)[:600]
        if rate_limited and all("429" in e or "RateLimit" in e for e in errors):
            raise _exc("RateLimitError")(f"Codomo rate limited: {summary}")
        if errors and all("Timeout" in e or "Connection" in e or "network" in e.lower() for e in errors):
            raise _exc("ScrapeNetworkError")(f"Codomo network error: {summary}")
        raise _exc("BlockedError")(f"Codomo page could not be fetched: {summary}")

    # direct ---------------------------------------------------------
    def _get_direct(self, url: str) -> str:
        import curl_cffi.requests as req
        try:
            from config import get_impersonate, get_proxy_url
            impersonate = get_impersonate()
            proxy = get_proxy_url() or ""
        except Exception:  # noqa: BLE001
            impersonate, proxy = "chrome", ""
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = req.get(
            url,
            impersonate=impersonate,
            proxies=proxies,
            timeout=30,
            headers={"Accept-Language": "en-GB,en;q=0.9,nl;q=0.8"},
        )
        if resp.status_code == 429:
            raise _exc("RateLimitError")("Codomo 429")
        if resp.status_code >= 400 and not looks_like_cf_challenge(resp.text):
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.text

    # zenrows --------------------------------------------------------
    def _zenrows(self, url: str, premium: bool) -> str:
        import requests
        params = {"apikey": self._zr_key, "url": url}
        if premium:
            params.update({"premium_proxy": "true", "proxy_country": self._country})
        resp = requests.get(self.ZENROWS_URL, params=params, timeout=90)
        if resp.status_code == 429:
            raise _exc("RateLimitError")("ZenRows 429")
        if resp.status_code >= 400:
            raise RuntimeError(f"ZenRows HTTP {resp.status_code}: {resp.text[:120]}")
        return resp.text

    def _get_zenrows(self, url: str) -> str:
        return self._zenrows(url, premium=False)

    def _get_zenrows_premium(self, url: str) -> str:
        return self._zenrows(url, premium=True)

    # brightdata -----------------------------------------------------
    def _get_brightdata(self, url: str) -> str:
        import requests
        resp = requests.post(
            self.BRIGHTDATA_URL,
            headers={"Authorization": f"Bearer {self._bd_key}", "Content-Type": "application/json"},
            json={"zone": self._bd_zone, "url": url, "format": "raw", "method": "GET"},
            timeout=90,
        )
        if resp.status_code == 429:
            raise _exc("RateLimitError")("BrightData 429")
        if resp.status_code >= 400:
            raise RuntimeError(f"BrightData HTTP {resp.status_code}: {resp.text[:120]}")
        return resp.text


# ── Scrape one city page → GraphQL-shaped result ─────────────────────

def products_for(variables: dict, fetcher: Optional[CodomoFetcher] = None) -> dict:
    """
    Fetch one Codomo search page for the city/page in ``variables`` and return
    it in the GraphQL ``products`` shape that scrapers/holland2stay.py parses.
    """
    fetcher = fetcher or CodomoFetcher()
    filters = (variables or {}).get("filters") or {}
    city_ids = [str(c) for c in ((filters.get("city") or {}).get("in") or [])]
    avail_ids = {int(a) for a in ((filters.get("available_to_book") or {}).get("in") or []) if str(a).isdigit()}
    page_no = int((variables or {}).get("currentPage") or 1)
    if len(city_ids) != 1:
        raise ValueError(f"Codomo search needs exactly one city, got {city_ids}")
    city_name, city_slug = city_param(city_ids[0])
    url = search_url(city_name, page_no)

    start = time.monotonic()
    html = fetcher.get(url)
    data = parse_search_page(html)
    cards = data["cards"]

    if not cards:
        if data["total"] == 0:
            logger.info("Codomo %s: site reports 0 homes", city_name)
            return _shape([], page_no, page_no)
        raise _exc("BlockedError")(
            f"Codomo page has no homes and no result count (url={url}, title={data['title']!r})"
        )

    # Make sure the city filter applied — never report "0 homes" for a city we didn't see
    in_city = [c for c in cards if c["city_slug"] == city_slug]
    if not in_city:
        raise _exc("BlockedError")(
            f"Codomo city filter not applied for {city_name} (url={url}); "
            f"first card: {cards[0]['href']}"
        )
    unknown = [c for c in in_city if c["status_id"] is None]
    if unknown and len(unknown) == len(in_city):
        raise _exc("BlockedError")(
            f"Codomo: no booking action recognised (labels: {sorted({c['action'] for c in unknown})})"
        )
    if unknown:
        logger.warning("Codomo: unrecognised booking actions %s", sorted({c["action"] for c in unknown}))

    items = []
    for c in in_city:
        if avail_ids and c["status_id"] not in avail_ids:
            continue
        items.append(_to_item(c, city_ids[0]))

    total_pages = max(page_no, data["max_page"])
    statuses: dict[str, int] = {}
    for c in in_city:
        statuses[c["action"] or "?"] = statuses.get(c["action"] or "?", 0) + 1
    logger.info(
        "Codomo %s page %d/%d (%.1fs): %d homes, %d match %s — %s",
        city_name, page_no, total_pages, time.monotonic() - start,
        len(in_city), len(items), sorted(avail_ids), statuses,
    )
    return _shape(items, page_no, total_pages)


def _to_item(c: dict, city_id: str) -> dict:
    item: dict = {
        "url_key": c["slug"],
        "sku": "",
        "name": c["title"],
        "available_to_book": c["status_id"],
        "available_startdate": c.get("date"),
        "city": city_id,
        "_url": c["url"],
    }
    if c.get("price") is not None:
        item["basic_rent"] = c["price"]
    for src, dst in (("area", "living_area"), ("rooms", "no_of_rooms"), ("floor", "floor"),
                     ("finishing", "finishing"), ("persons", "maximum_number_of_persons"),
                     ("building", "building_name")):
        if c.get(src):
            item[dst] = c[src]
    extra = []
    if c.get("service"):
        extra.append(c["service"])
    if c.get("interest_count") is not None:
        extra.append(f"{c['interest_count']} people interested")
    if extra:
        item["offer_text_two"] = " · ".join(extra)
    return item


def _shape(items: list, page_no: int, total_pages: int) -> dict:
    return {"data": {"products": {
        "total_count": len(items),
        "page_info": {"current_page": page_no, "total_pages": total_pages},
        "items": items,
    }}}
