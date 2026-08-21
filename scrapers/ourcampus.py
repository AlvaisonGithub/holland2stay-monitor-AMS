"""
scrapers/ourcampus.py — OurCampus Amsterdam Diemen RENTCafe scraper

OurCampus 使用与 OurDomain 相同的 SecureRC / RENTCafe 后端。
因此直接继承 OurDomainScraper，复用：
- floorplans.aspx floorplan discovery
- rcLoadContent.ashx availableunits
- unit HTML parsing
- status / rent / area parsing
- Cloudflare / TLS fingerprint handling
"""

from .ourdomain import OurDomainScraper


class OurCampusScraper(OurDomainScraper):
    """OurCampus Amsterdam Diemen unit-level RENTCafe scraper."""

    source = "ourcampus"

    BASE = (
        "https://new-ourcampus-amsterdam-diemen-rentcafewebsiteuk."
        "securerc.co.uk/onlineleasing"
    )

    BUILDINGS = {
        "diemen": {
            "base": BASE,
            "slug": "new-ourcampus-amsterdam-diemen",
            "display": "OurCampus Amsterdam Diemen",
            "short_display": "OurCampus",
            "property_id": "186609",
            "type": "Studio",
            "street_address": "Dalsteindreef, Diemen",
        },
    }
