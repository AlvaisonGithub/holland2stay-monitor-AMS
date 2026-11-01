"""
scrapers/ourdomain.py — OurDomain / RENTCafe unit-level scraper
================================================================

OurDomain exposes availability through RENTCafe HTML endpoints:
1. ``floorplans.aspx`` discovers floor plan IDs.
2. ``rcLoadContent.ashx?contentclass=availableunits`` returns concrete units.

Both endpoints are behind Cloudflare WAF on ``*.securerc.co.uk``.
Railway/cloud IPs are WAF-blocked (403) at the IP level — curl_cffi,
CloakBrowser, and Playwright all fail. BrightData requires KYC for
this domain (robots.txt Disallow: /).

The only working path is **ZenRows Fetch API** (no KYC, no robots.txt
check, auto JS rendering + premium proxy). This module uses ZenRows
exclusively.

Env vars
--------
    ZENROWS_API_KEY=xxx              # required (free: 5000 credits/month)
    ZENROWS_COUNTRY=nl               # optional
    ZENROWS_MONTHLY_CREDIT_LIMIT=4500  # optional

Architecture
------------
    scrape(task)
      └→ _fetch_floorplans(base, slug)         # Phase 1: floorplans.aspx
           └→ ZenRowsFetcher.fetch_html(url)   #   with js_instructions
           └→ _extract_floorplan_ids(html)     #   parse IDs
           └→ _extract_floorplan_names(html)   #   parse names
         _fetch_units(base, fp_id, ...)        # Phase 2: per floorplan
           └→ ZenRowsFetcher.fetch_html(url, ajax=True)
           └→ _extract_units(html)             #   parse unit rows

Floorplan ID cache (12h TTL) avoids re-fetching floorplans.aspx every
round, saving ~25 ZenRows credits per building per round.

Listings are keyed by physical unit ID, prefixed ``od_`` to avoid
collision with Holland2Stay URL slugs.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from html import unescape
import logging
import os
import re
import time
from typing import Optional

from config import CLOAKBROWSER_HEADLESS
from models import Listing

from .base import (
    AbstractScraper,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
)

logger = logging.getLogger(__name__)


# ── Building metadata ────────────────────────────────────────────────
# Each building has: slug (URL path), property_id (RENTCafe internal),
# display name, street_address (for geocode), type.
#
# street_address must be the real street address — unit names like
# "Diemen #6045" are internal numbers, not geocodable. All units in a
# building share the same address (same physical pin on map).

BUILDINGS: dict[str, dict[str, str]] = {
    "diemen": {
        "slug": "ourdomain-amsterdam-diemen2",
        "display": "Amsterdam Diemen",
        "short_display": "Diemen",
        "property_id": "184283",
        "type": "Studio",
        "street_address": "Wenckebachweg 51, 1096 AN Amsterdam",
    },
    "south-east": {
        "base": "https://southeast-thisisourdomain.securerc.co.uk/onlineleasing",
        "slug": "ourdomain-amsterdam-south-east",
        "display": "Amsterdam South East",
        "short_display": "South East",
        "property_id": "182801",
        "type": "Studio",
        "street_address": "Dalsteindreef 20-40, 1112 XC Diemen",
    },
}

_DEFAULT_BASE = "https://thisisourdomain.securerc.co.uk/onlineleasing"


# ── Floorplan ID cache (class-level, persists across rounds) ─────────
# key: building slug    value: (fp_ids, fp_names_by_id, timestamp)
_FP_CACHE: dict[str, tuple[list[str], dict[str, str], float]] = {}
_FP_CACHE_TTL = 43200  # 12 hours (seconds)


class OurDomainScraper(AbstractScraper):
    """Unit-level scraper for OurDomain properties backed by RENTCafe."""

    source = "ourdomain"

    def __init__(self) -> None:
        self._fetcher: Optional[ZenRowsFetcher] = None

    # ── Fetcher lifecycle ────────────────────────────────────────────

    def _get_fetcher(self) -> ZenRowsFetcher:
        """Get or create the ZenRows fetcher (singleton per scraper)."""
        if self._fetcher is None:
            from .zenrows_fetcher import ZenRowsFetcher
            zr = ZenRowsFetcher(headless=CLOAKBROWSER_HEADLESS)
            if not zr.is_configured:
                raise BlockedError(
                    "ZENROWS_API_KEY not set. OurDomain requires ZenRows for "
                    "Cloudflare WAF bypass (curl_cffi/BrightData/CloakBrowser "
                    "all fail on Railway). Free tier: 5000 credits/month, "
                    "no KYC. Sign up: https://app.zenrows.com/register"
                )
            self._fetcher = zr
            logger.info("OurDomain: using ZenRows Fetch API")
        return self._fetcher

    @contextmanager
    def batch_session(self):
        """No-op context manager for interface compatibility."""
        yield

    # ── Main scrape entry point ──────────────────────────────────────

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        building = self._building_for_task(task)
        slug = building["slug"]
        property_id = building["property_id"]
        base = building.get("base", _DEFAULT_BASE)
        display = task.city_display or building["display"]
        move_in_date = task.extra.get("move_in_date") or _next_month_first()

        floorplans_url = f"{base}/{slug}/floorplans.aspx"

        # Phase 1: Get floorplan IDs (with cache)
        fp_ids, fp_names_by_id = self._get_floorplans(
            slug=slug, url=floorplans_url, display=display,
        )

        if not fp_ids:
            logger.warning("[%s] no floorplan IDs found", display)
            return ScrapeResult(task=task, listings=[], complete=False)

        # Phase 2: Fetch units per floorplan
        all_units: dict[str, dict] = {}
        complete = True

        for fp_id in fp_ids:
            ajax_url = (
                f"{base}/rcLoadContent.ashx"
                f"?contentclass=availableunits"
                f"&floorPlans={fp_id}"
                f"&MoveInDate={move_in_date}"
                f"&myolePropertyID={property_id}"
            )
            try:
                unit_html = self._get_fetcher().fetch_html(
                    ajax_url, referer=floorplans_url, ajax=True,
                )
            except RateLimitError:
                raise
            except BlockedError:
                raise
            except Exception as e:
                complete = False
                logger.error("[%s] AJAX failed fp_id=%s: %s", display, fp_id, e, exc_info=True)
                continue

            units_found = _extract_units(unit_html)
            if units_found:
                logger.info("[%s] fp_id=%s: %d units (%d chars)", display, fp_id, len(units_found), len(unit_html))
            else:
                logger.debug("[%s] fp_id=%s: 0 units (%d chars)", display, fp_id, len(unit_html))

            for unit in units_found:
                _merge_unit(all_units, unit, fp_id)

        # Convert to listings
        listings = [
            _to_listing(
                unit,
                base_url=floorplans_url,
                city_display=display,
                building_label=building.get("short_display") or task.extra.get("building_label"),
                source=self.source,
                default_type=building.get("type"),
                fp_names_by_id=fp_names_by_id,
                street_address=building.get("street_address"),
            )
            for unit in all_units.values()
        ]

        logger.info("[%s] OurDomain done: units=%d, complete=%s", display, len(listings), complete)
        return ScrapeResult(task=task, listings=listings, complete=complete)

    # ── Phase 1: floorplan IDs ───────────────────────────────────────

    def _get_floorplans(
        self, *, slug: str, url: str, display: str,
    ) -> tuple[list[str], dict[str, str]]:
        """Get floorplan IDs + names, from cache or by fetching."""
        # Check cache
        cached = _FP_CACHE.get(slug)
        if cached:
            fp_ids, fp_names, cached_at = cached
            if time.monotonic() - cached_at < _FP_CACHE_TTL:
                logger.debug("[%s] floorplan cache hit: %d IDs", display, len(fp_ids))
                return fp_ids, fp_names

        # Fetch floorplans.aspx
        fp_html = self._get_fetcher().fetch_html(url)
        fp_ids = _extract_floorplan_ids(fp_html)

        if not fp_ids:
            _log_floorplan_diagnostic(display, fp_html)
            return [], {}

        fp_names = _extract_floorplan_names(fp_html)
        _FP_CACHE[slug] = (fp_ids, fp_names, time.monotonic())
        logger.info("[%s] floorplan cache miss: fetched %d IDs", display, len(fp_ids))
        return fp_ids, fp_names

    # ── Task → building metadata ─────────────────────────────────────

    def _building_for_task(self, task: ScrapeTask) -> dict[str, str]:
        key = (task.city_key or "").strip().lower()
        building = dict(BUILDINGS.get(key) or {})
        if not building:
            slug = task.extra.get("slug") or task.city_key
            property_id = task.extra.get("property_id")
            if not slug or not property_id:
                raise ValueError(
                    f"Unknown OurDomain city_key={task.city_key!r}; "
                    "provide extra.slug and extra.property_id"
                )
            building = {
                "slug": str(slug),
                "display": task.city_display,
                "property_id": str(property_id),
            }
        if task.extra.get("type"):
            building["type"] = str(task.extra["type"])
        return building


# ────────────────────────────────────────────────────────────────────
# Floorplan ID extraction
# ────────────────────────────────────────────────────────────────────
# RENTCafe floorplan IDs appear in different patterns depending on
# whether the HTML is server-side rendered or JS-rendered:
#
#   subPointerId=NNNN          — onclick (Diemen, server-side)
#   myFloorPlanId=NNNN         — Get Notified links (South-East)
#   FP_Detail_NNNN             — anchor href (South-East v2)
#   data-fp-id="NNNN"          — injected by ZenRows js_instructions
#   data-floorplanid="NNNN"    — JS-hydrated elements
#   "floorPlanId":NNNN         — JSON in script tags
#
# The data-fp-id pattern is injected by ZenRowsFetcher's js_instructions
# evaluate script after JS rendering.

def _extract_floorplan_ids(html: str) -> list[str]:
    patterns = [
        r"subPointerId=(\d+)",
        r"myFloorPlanId=(\d+)",
        r"FP_Detail_(\d+)",
        r'data-fp-id=["\'](\d+)',
        r'data-floorplanid["\s]*=["\s]*(\d+)',
        r'"floorPlanId"\s*:\s*(\d+)',
        r'fpId\s*=\s*["\']?(\d+)',
        r'floorplanid=(\d+)',
    ]
    for pattern in patterns:
        ids = re.findall(pattern, html, re.I)
        if ids:
            return list(dict.fromkeys(ids))

    # Last resort: 6-8 digit numbers near floorplan keywords
    for m in re.finditer(r'(?:floorPlan|availableunits|Get\s+Notified|showDialog)', html, re.I):
        window = html[m.start():m.start() + 300]
        fp_match = re.findall(r'\b(\d{6,8})\b', window)
        if fp_match:
            return list(dict.fromkeys(fp_match))

    return []


def _log_floorplan_diagnostic(display: str, html: str) -> None:
    """Log which patterns are present when no IDs found."""
    patterns = {
        "subPointerId": "subPointerId" in html,
        "myFloorPlanId": "myFloorPlanId" in html,
        "FP_Detail": "FP_Detail_" in html,
        "data-fp-id": "data-fp-id" in html,
        "zenrows-extract": "zenrows-fp-extract" in html,
        "floor-plan-selector": "floor-plan-selector" in html,
        "FFloorPlan": "FFloorPlan" in html,
        "data-selenium-id": "data-selenium-id" in html,
        "floorPlan JS": "floorPlan" in html or "floorplan" in html.lower(),
    }
    logger.warning(
        "[%s] no floorplan IDs (html=%d chars). Patterns: %s. First 1000: %s",
        display, len(html), patterns, html[:1000],
    )


def _extract_floorplan_names(html: str) -> dict[str, str]:
    """
    Parse {fp_id: fp_name} from floorplans.aspx.

    Diemen theme: showDialog('Floor Plan {name}' ... subPointerId=NNN)
    South-East theme: <a href="#FP_Detail_NNNN">{name}</a>
    """
    mapping: dict[str, str] = {}

    # Strategy 1 (Diemen): showDialog('Floor Plan {name}' ... subPointerId=NNN)
    pattern = re.compile(
        r"showDialog\(\s*'Floor Plan ([^']+)'[^>]*?subPointerId=(\d+)",
        re.IGNORECASE | re.DOTALL,
    )
    for raw_name, fp_id in pattern.findall(html):
        if fp_id not in mapping:
            name = unescape(raw_name).strip()
            if name:
                mapping[fp_id] = name

    # Strategy 2 (South-East): <a href="#FP_Detail_NNNN">{name}</a>
    if not mapping:
        pattern2 = re.compile(
            r'href=["\'][^"\']*#FP_Detail_(\d+)["\'][^>]*>\s*([^<]+?)\s*</a>',
            re.IGNORECASE,
        )
        for fp_id, raw_name in pattern2.findall(html):
            if fp_id not in mapping:
                name = unescape(raw_name).strip()
                if name:
                    mapping[fp_id] = name

    return mapping


# ────────────────────────────────────────────────────────────────────
# Unit extraction
# ────────────────────────────────────────────────────────────────────

def _extract_units(html: str) -> list[dict]:
    units: list[dict] = []
    for row in re.finditer(
        r"<tr\b(?=[^>]*\bid=[\"']unitrow_(\d+)[\"'])[^>]*>.*?</tr>",
        html, re.IGNORECASE | re.DOTALL,
    ):
        unit = _extract_unit(row.group(0), row.group(1))
        if unit:
            units.append(unit)
    return units


def _extract_unit(row_html: str, unit_id: str) -> Optional[dict]:
    """Extract unit data from a single <tr id="unitrow_NNN"> row."""
    idx_match = re.search(r'data-selenium-id=["\']urow(\d+)["\']', row_html, re.I)
    idx = idx_match.group(1) if idx_match else "1"

    apt = _cell_text(row_html, f"Apt{idx}", labels=("Apartment", "Apt", "Unit"))
    if not apt:
        logger.warning("OurDomain unit missing Apt%s: unit_id=%s", idx, unit_id)
        return None

    amenity_html = _cell_html(row_html, f"Amenity{idx}", labels=("Amenities", "Floor", "Details"))
    detail_labels = _label_texts(amenity_html)
    detail = ", ".join(detail_labels)
    floor = parse_ourdomain_floor(detail)
    avail_date = _extract_apply_date(row_html)

    sqft = _cell_text(row_html, f"SqFt{idx}", labels=("Sq.M.", "Sq.Ft.", "Size", "Area"))
    rent = _cell_text(row_html, f"Rent{idx}", labels=("Rent",))
    deposit = _cell_text(row_html, f"Deposit{idx}", labels=("Deposit",))
    avail_html = _cell_html(row_html, f"AvailDate{idx}", labels=("Availability", "Available", "Apply"))

    if not sqft and not detail and not rent:
        logger.warning("OurDomain unit %s all core fields empty — row_html[:300]: %s", unit_id, row_html[:300])

    return {
        "unit_id": unit_id,
        "apt": apt,
        "sqft": sqft,
        "rent": rent,
        "deposit": deposit,
        "detail": detail,
        "floor": floor,
        "status": _extract_status(avail_html),
        "avail_date": avail_date,
        "fp_ids": [],
    }


def _merge_unit(all_units: dict[str, dict], unit: dict, fp_id: str) -> None:
    unit_id = unit["unit_id"]
    if unit_id not in all_units:
        all_units[unit_id] = unit
    if fp_id not in all_units[unit_id]["fp_ids"]:
        all_units[unit_id]["fp_ids"].append(fp_id)


# ────────────────────────────────────────────────────────────────────
# Listing conversion
# ────────────────────────────────────────────────────────────────────

def _to_listing(
    unit: dict,
    *,
    base_url: str,
    city_display: str,
    source: str,
    building_label: Optional[str] = None,
    default_type: Optional[str] = None,
    fp_names_by_id: Optional[dict[str, str]] = None,
    street_address: Optional[str] = None,
) -> Listing:
    apt = unit.get("apt") or f"#{unit['unit_id']}"
    detail = unit.get("detail") or "OurDomain"
    sqft = unit.get("sqft") or ""
    listing_name = _format_listing_name(city_display, apt, building_label)

    fp_names: list[str] = []
    if fp_names_by_id:
        for fp_id in unit.get("fp_ids", []):
            name = fp_names_by_id.get(fp_id)
            if name:
                fp_names.append(name)
    occupancy = _infer_occupancy(sqft=sqft, fp_names=fp_names)

    features = [f"Unit: {apt}", f"Building: {city_display}"]
    if street_address:
        features.append(f"Address: {street_address}")
    if default_type:
        features.append(f"Type: {default_type}")
    if sqft:
        features.append(f"Area: {sqft} m²")
    if occupancy:
        features.append(f"Occupancy: {occupancy}")
    if unit.get("floor") is not None:
        features.append(f"Floor: {unit['floor']}")
    if unit.get("deposit"):
        features.append(f"Deposit: {unit['deposit']}")
    if detail:
        features.append(f"Detail: {detail}")
    if unit.get("fp_ids"):
        features.append(f"Floorplans: {', '.join(unit['fp_ids'])}")

    return Listing(
        id=f"od_{unit['unit_id']}",
        name=listing_name,
        status=unit.get("status") or "Occupied",
        price_raw=unit.get("rent") or None,
        available_from=unit.get("avail_date") or None,
        features=features,
        url=base_url,
        city=city_display,
        source=source,
    )


def _format_listing_name(city_display: str, apt: str, building_label: Optional[str] = None) -> str:
    building = (building_label or _short_building_label(city_display)).strip()
    unit = (apt or "").strip()
    if not unit.startswith("#") and unit:
        unit = f"#{unit}"
    return " ".join(part for part in [building, unit] if part)


def _short_building_label(city_display: str) -> str:
    value = (city_display or "").strip()
    if not value:
        return "OurDomain"
    lower = value.lower()
    if lower == "amsterdam diemen" or lower.endswith(" diemen"):
        return "Diemen"
    return value


# ────────────────────────────────────────────────────────────────────
# Occupancy inference (matches H2S vocabulary for cross-source filter)
# ────────────────────────────────────────────────────────────────────

def _infer_occupancy(
    sqft: Optional[str] = None,
    fp_names: Optional[list[str]] = None,
) -> Optional[str]:
    """
    OD SecureRC unit table has no Beds column. Infer from sqft:
    - < 30 m² → "One" (single studio)
    - 30-60 m² → "Two (only couples)" (large studio / 1-BR)
    - >= 60 m² → "Family (parents with children)" (2+ BR)

    Fallback to FP name keywords when sqft is missing.
    """
    if sqft:
        from models import parse_float
        sqm = parse_float(sqft)
        if sqm is not None:
            if sqm < 30:
                return "One"
            if sqm < 60:
                return "Two (only couples)"
            return "Family (parents with children)"

    if fp_names:
        joined = " | ".join(fp_names).lower()
        if "1-person max" in joined or "1 person max" in joined:
            return "One"
        if re.search(r"\b[2-9]-bedroom", joined):
            return "Family (parents with children)"
        if "1-bedroom" in joined or "1 bedroom" in joined:
            return "Two (only couples)"
        if "studio" in joined:
            if "plus studio" in joined:
                return "Two (only couples)"
            return "One"

    return None


# ────────────────────────────────────────────────────────────────────
# HTML cell parsing helpers
# ────────────────────────────────────────────────────────────────────

def _cell_html(row_html: str, selenium_id: str, *, labels: tuple[str, ...] = ()) -> str:
    """
    Extract cell HTML from a table row.

    Strategy 1: data-selenium-id exact match (Diemen theme)
    Strategy 2: data-label fallback (South-East and other themes)
    """
    # Strategy 1: selenium-id
    pattern = (
        r"<(?P<tag>th|td)\b"
        rf"(?=[^>]*data-selenium-id=[\"']{re.escape(selenium_id)}[\"'])"
        r"[^>]*>(?P<body>.*?)</(?P=tag)>"
    )
    m = re.search(pattern, row_html, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group("body")

    # Strategy 2: data-label
    for label in labels:
        pattern2 = (
            r"<(?P<tag>th|td)\b"
            rf"(?=[^>]*data-label=[\"']?{re.escape(label)}[\"']?)"
            r"[^>]*>(?P<body>.*?)</(?P=tag)>"
        )
        m = re.search(pattern2, row_html, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group("body")

    return ""


def _cell_text(row_html: str, selenium_id: str, *, labels: tuple[str, ...] = ()) -> str:
    return _strip_html(_cell_html(row_html, selenium_id, labels=labels))


def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _label_texts(html: str) -> list[str]:
    labels: list[str] = []
    for m in re.finditer(r"<label\b[^>]*>(.*?)</label>", html, re.IGNORECASE | re.DOTALL):
        text = _strip_html(m.group(1))
        if not text:
            continue
        lower = text.lower()
        if lower == "max-rent" or "prices and special offers" in lower:
            continue
        labels.append(text)
    return labels


def _extract_status(avail_html: str) -> str:
    m = re.search(
        r"<span\b[^>]*class=[\"']([^\"']*)[\"'][^>]*>(.*?)</span>",
        avail_html, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return "Occupied"
    classes = m.group(1).lower()
    text = _strip_html(m.group(2)).lower()
    if "success" in classes or text == "available":
        return "Available to book"
    if "warning" in classes or "wait" in text:
        return "Available in lottery"
    return "Occupied"


def _extract_apply_date(row_html: str) -> Optional[str]:
    m = re.search(
        r"ApplyNowClick\([^)]*?[\"'](\d{1,2}-\d{1,2}-\d{4})[\"']",
        row_html, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    return _normalize_date(m.group(1))


def _normalize_date(value: str) -> Optional[str]:
    value = value.strip()
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", value)
    if m:
        day, month, year = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_ourdomain_floor(detail: str) -> Optional[int]:
    """Extract the lowest floor number from an OurDomain amenity label."""
    if not detail:
        return None
    lower = detail.lower()
    if "ground" in lower:
        return 0
    m = re.search(r"Floor\s*(\d+)", detail, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _next_month_first() -> str:
    today = date.today()
    year = today.year + (today.month // 12)
    month = (today.month % 12) + 1
    return f"{year:04d}-{month:02d}-01"
