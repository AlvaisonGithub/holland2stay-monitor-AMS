"""Holland2Stay → Codomo (codomo.eu) search-page scraper."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers import codomo
from scrapers.base import BlockedError, ScrapeTask

FX = Path(__file__).parent / "fixtures"
AMS = (FX / "codomo_amsterdam_flat.html").read_text(encoding="utf-8")
NIJ = (FX / "codomo_nijmegen_streamed.html").read_text(encoding="utf-8")
CF = "<html><head><title>Just a moment...</title></head><body>cf</body></html>"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ZENROWS_API_KEY", "BRIGHTDATA_API_KEY", "H2S_FETCH_MODE", "H2S_SITE"):
        monkeypatch.delenv(k, raising=False)
    codomo.CodomoFetcher._sticky = None
    codomo.CodomoFetcher._direct_blocked_until = 0.0
    yield
    codomo.CodomoFetcher._sticky = None
    codomo.CodomoFetcher._direct_blocked_until = 0.0


def _resp(status=200, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


# ── parsing ──────────────────────────────────────────────────────────

def test_parse_flat_page():
    d = codomo.parse_search_page(AMS)
    assert d["total"] == 7 and d["max_page"] == 1
    c0, c1 = d["cards"][0], d["cards"][1]
    assert c0["slug"] == "jan-van-zutphenstraat-923" and c0["city_slug"] == "amsterdam"
    assert c0["url"] == "https://codomo.eu/homes/nl/en/amsterdam/twin/jan-van-zutphenstraat-923"
    assert (c0["rooms"], c0["area"], c0["persons"], c0["finishing"], c0["floor"], c0["building"]) == \
        ("4 bedrooms", "109.95", "5 persons", "Unfurnished", "10. floor", "Twin 1")
    assert c0["date"] == "2026-11-24" and c0["price"] == 2053.0
    assert c0["action"] == "Book directly" and c0["status_id"] == 179
    assert c1["action"] == "Show interest" and c1["status_id"] == 336 and c1["interest_count"] == 202
    assert c1["price"] == 932.0 and "service costs" in c1["service"]


def test_parse_streamed_page_fills_nested_placeholders():
    d = codomo.parse_search_page(NIJ)
    assert d["total"] == 2 and len(d["cards"]) == 2
    assert [c["building"] for c in d["cards"]] == ["Canisiushof", "Canisiushof"]
    assert d["cards"][1]["slug"] == "berg-en-dalseweg-79a109"  # slug comes from the link, not the address
    assert d["cards"][1]["date"] == "2026-12-04" and d["cards"][1]["price"] == 1705.0


def test_euro_and_pages():
    assert codomo._parse_eur("€2,053") == 2053.0
    assert codomo._parse_eur("€1,234.50") == 1234.5
    assert codomo._parse_eur("€1.234,50") == 1234.5
    html = '<title>codomo</title><p role="status">131 residences</p><a href="?page=2">2</a><a href="?page=6">6</a>'
    assert codomo.parse_search_page(html)["max_page"] == 6


def test_city_names():
    assert codomo.city_param("24") == ("Amsterdam", "amsterdam")
    assert codomo.city_param("90") == ("Den Haag", "den-haag")
    assert codomo.search_url("Amsterdam", 2) == "https://codomo.eu/homes/nl/en?city=Amsterdam&page=2"


# ── products_for (GraphQL-shaped output) ─────────────────────────────

def _fetcher(html):
    f = MagicMock()
    f.get.return_value = html
    return f


V = {"filters": {"city": {"in": ["24"]}, "available_to_book": {"in": ["179", "336"]}}, "currentPage": 1}


def test_products_for_shape_and_filters():
    out = codomo.products_for(V, _fetcher(AMS))
    items = out["data"]["products"]["items"]
    assert [i["url_key"] for i in items] == ["jan-van-zutphenstraat-923", "james-wattstraat-77f25", "jan-van-zutphenstraat-779"]
    i1 = items[1]
    assert i1["available_to_book"] == 336 and i1["basic_rent"] == 932.0
    assert i1["available_startdate"] == "2026-12-10" and i1["living_area"] == "25.5"
    assert i1["_url"].startswith("https://codomo.eu/homes/nl/en/amsterdam/europahuis/")
    assert "202 people interested" in i1["offer_text_two"]
    only_book = dict(V, filters={"city": {"in": ["24"]}, "available_to_book": {"in": ["179"]}})
    assert len(codomo.products_for(only_book, _fetcher(AMS))["data"]["products"]["items"]) == 2


def test_wrong_city_page_is_blocked_not_empty():
    with pytest.raises(BlockedError):
        codomo.products_for(V, _fetcher(NIJ))  # Nijmegen homes on an Amsterdam request


def test_zero_homes_vs_unreadable_page():
    empty = '<title>Book a room · Codomo</title><p role="status">0 residences</p>'
    assert codomo.products_for(V, _fetcher(empty))["data"]["products"]["items"] == []
    with pytest.raises(BlockedError):
        codomo.products_for(V, _fetcher("<title>Codomo</title><p>something else</p>"))


def test_unknown_action_labels_fail_loudly():
    html = AMS.replace(">Book directly<", ">Join waitlist<").replace(">Show interest<", ">Join waitlist<")
    with pytest.raises(BlockedError):
        codomo.products_for(V, _fetcher(html))


# ── transport ────────────────────────────────────────────────────────

def test_direct_fetch_is_used_first():
    with patch("curl_cffi.requests.get", return_value=_resp(text=AMS)) as g, patch("requests.get") as zr:
        assert codomo.CodomoFetcher().get("https://codomo.eu/x") == AMS
        assert g.call_count == 1 and zr.call_count == 0
    assert codomo.CodomoFetcher._sticky == "direct"


def test_falls_back_to_zenrows_when_direct_blocked(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    with patch("curl_cffi.requests.get", return_value=_resp(403, CF)), \
         patch("requests.get", return_value=_resp(text=AMS)) as zr:
        f = codomo.CodomoFetcher()
        assert f.get("https://codomo.eu/x") == AMS
        params = zr.call_args.kwargs["params"]
        assert "js_render" not in params and "premium_proxy" not in params  # cheapest mode
        # direct is skipped for a while after a block
        f.get("https://codomo.eu/x")
    assert codomo.CodomoFetcher._sticky == "zenrows"


def test_no_keys_and_blocked_raises():
    with patch("curl_cffi.requests.get", return_value=_resp(403, CF)):
        with pytest.raises(BlockedError):
            codomo.CodomoFetcher().get("https://codomo.eu/x")


# ── end to end through the existing H2S scraper ──────────────────────

def test_holland2stay_scraper_uses_codomo():
    import scrapers.holland2stay as h2s
    from scrapers import h2s_api_fetcher as api

    assert api.resolve_h2s_fetch_mode() == "api"  # no API key needed
    h2s._attr_label_cache = {}
    with patch("curl_cffi.requests.get", return_value=_resp(text=AMS)) as g, \
         patch("scrapers.holland2stay.BrowserFetcher") as bf:
        s = h2s.HollandStayScraper()
        with s.batch_session():
            res = s.scrape(ScrapeTask(source="holland2stay", city_key="24", city_display="Amsterdam",
                                      extra={"availability_ids": ["179", "336"]}))
        bf.assert_not_called()
        assert "city=Amsterdam" in g.call_args.args[0]
    assert res.complete is True
    ids = [l.id for l in res.listings]
    assert ids == ["jan-van-zutphenstraat-923", "james-wattstraat-77f25", "jan-van-zutphenstraat-779"]
    l = res.listings[1]
    assert l.status == "Available in lottery" and l.price_raw == "€932" and l.available_from == "2026-12-10"
    assert l.url == "https://codomo.eu/homes/nl/en/amsterdam/europahuis/james-wattstraat-77f25"
    assert any(f.startswith("Type: Studio") for f in l.features)
    assert any(f.startswith("Area: 25.5") for f in l.features)
    assert any(f.startswith("Building: Europahuis") for f in l.features)
    assert res.listings[0].status == "Available to book"


def test_legacy_switch(monkeypatch):
    from scrapers import h2s_api_fetcher as api
    monkeypatch.setenv("H2S_SITE", "legacy")
    assert api.resolve_h2s_fetch_mode() == "browser"  # old behaviour without keys
