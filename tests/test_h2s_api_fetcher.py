"""Tests for scrapers/h2s_api_fetcher.py (ZenRows / BrightData path for H2S)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from scrapers import h2s_api_fetcher as mod
from scrapers.base import BlockedError, ScrapeTask

GQL_OK = {"data": {"products": {"items": [], "page_info": {"total_pages": 1}}}}
CF_PAGE = "<html><head><title>Just a moment...</title></head><body>cf</body></html>"


def _resp(status=200, text="", headers=None, json_obj=None):
    r = MagicMock()
    r.status_code = status
    r.text = text if json_obj is None else json.dumps(json_obj)
    r.headers = headers or {}
    if json_obj is not None:
        r.json.return_value = json_obj
    else:
        r.json.side_effect = ValueError("no json")
    return r


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    mod.H2SApiFetcher._sticky_strategy = None
    mod.H2SApiFetcher._disabled_providers = set()
    mod.H2SApiFetcher._gql_dead_until = 0.0
    for k in ("ZENROWS_API_KEY", "BRIGHTDATA_API_KEY", "H2S_FETCH_MODE"):
        monkeypatch.delenv(k, raising=False)
    yield
    mod.H2SApiFetcher._sticky_strategy = None
    mod.H2SApiFetcher._disabled_providers = set()


def test_mode_resolution(monkeypatch):
    assert mod.resolve_h2s_fetch_mode() == "browser"
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    assert mod.resolve_h2s_fetch_mode() == "api"
    monkeypatch.setenv("H2S_FETCH_MODE", "browser")
    assert mod.resolve_h2s_fetch_mode() == "browser"


def test_zenrows_post_success_and_sticky(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    with patch("requests.post", return_value=_resp(text=json.dumps(GQL_OK))) as post:
        f = mod.H2SApiFetcher()
        assert f.fetch_gql("q", {}) == GQL_OK
        assert post.call_args.kwargs["params"]["url"] == mod.H2S_GQL_URL
    assert mod.H2SApiFetcher._sticky_strategy == "zenrows-post"



def test_zenrows_402_disables_provider_then_brightdata(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "b")

    def fake_post(url, **kw):
        if "zenrows" in url:
            return _resp(402, "no credits")
        return _resp(json_obj={"status_code": 200, "headers": {}, "body": json.dumps(GQL_OK)})

    with patch("requests.post", side_effect=fake_post):
        assert mod.H2SApiFetcher().fetch_gql("q") == GQL_OK
    assert "zenrows" in mod.H2SApiFetcher._disabled_providers
    assert mod.H2SApiFetcher._sticky_strategy == "brightdata-post"


def test_all_fail_raises_blocked(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    with patch("requests.post", return_value=_resp(text=CF_PAGE)), \
         patch("requests.get", return_value=_resp(422, "RESP001")):
        with pytest.raises(BlockedError):
            mod.H2SApiFetcher().fetch_gql("q")


def test_no_keys_blocked():
    with pytest.raises(BlockedError):
        mod.H2SApiFetcher().ensure_initialized()


def test_scraper_uses_api_fetcher_when_key_set(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    import scrapers.holland2stay as h2s
    from models import Listing

    item = {"url_key": "unit-1", "sku": "S1", "name": "Unit 1", "available_to_book": 179,
            "basic_rent": 1200, "city": 24}
    page = {"data": {"products": {"items": [item], "page_info": {"total_pages": 1}}}}
    aggs = {"data": {"products": {"aggregations": []}}}

    def fake_post(url, **kw):
        body = kw.get("data", b"").decode()
        return _resp(text=json.dumps(aggs if "aggregations" in body else page))

    h2s._attr_label_cache = {}
    with patch("requests.post", side_effect=fake_post), \
         patch("scrapers.holland2stay.BrowserFetcher") as bf:
        s = h2s.HollandStayScraper()
        with s.batch_session():
            res = s.scrape(ScrapeTask(source="holland2stay", city_key="24", city_display="Amsterdam",
                                      extra={"availability_ids": ["179"]}))
        bf.assert_not_called()
    assert res.complete is True
    assert len(res.listings) == 1
    assert isinstance(res.listings[0], Listing)
    assert res.listings[0].status == "Available to book"


def test_zenrows_render_in_page_fetch(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    import html as _h
    page = ('<html><body><div>H2S</div><pre id="h2s-gql-result" style="display:none" data-status="200">'
            + _h.escape(json.dumps(GQL_OK)) + "</pre></body></html>")

    def fake_get(url, params=None, **kw):
        assert params["js_render"] == "true"
        assert "/api/graphql" in params["js_instructions"]
        return _resp(text=page)

    with patch("requests.post", return_value=_resp(422, '{"code":"RESP001"}')), \
         patch("requests.get", side_effect=fake_get):
        assert mod.H2SApiFetcher().fetch_gql("q", {"x": 1}) == GQL_OK
    assert mod.H2SApiFetcher._sticky_strategy == "zenrows-render"


def _dom_page(cards):
    import html as _h
    payload = json.dumps({"cards": cards, "links": len(cards), "title": "Residences", "body": "x"})
    return '<html><body><pre id="h2s-dom-result" style="display:none">' + _h.escape(payload) + "</pre></body></html>"


_CARDS = [
    {"href": "https://www.holland2stay.com/residences/kastanjelaan-1-108", "slug": "kastanjelaan-1-108",
     "title": "Kastanjelaan 1-108", "text": "Kastanjelaan 1-108\nAmsterdam\n€ 1.395 p/m\nAvailable to book\nAvailable from 01-12-2026"},
    {"href": "https://www.holland2stay.com/residences/foo-2", "slug": "foo-2",
     "title": "Foo 2", "text": "Foo 2\nAmsterdam\n€ 950\nOccupied"},
    {"href": "https://www.holland2stay.com/residences/bar-3", "slug": "bar-3",
     "title": "Bar 3", "text": "Bar 3\nDiemen\n€ 1.100\nAvailable in lottery"},
]


def test_dom_fallback_end_to_end(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    import scrapers.holland2stay as h2s
    mod.H2SApiFetcher._gql_dead_until = 0.0
    calls = {"dom": 0}

    def fake_get(url, params=None, **kw):
        if "h2s-dom-result" in params.get("js_instructions", "") or "?city" in params["url"]:
            calls["dom"] += 1
            assert "city%5Bfilter%5D=Amsterdam%2C24" in params["url"] or "city%5Bfilter%5D=Diemen%2C110" in params["url"]
            assert "page=1" in params["url"]
            if "Diemen" in params["url"]:
                return _resp(text=_dom_page([c for c in _CARDS if "Diemen" in c["text"]]))
            return _resp(text=_dom_page([c for c in _CARDS if "Amsterdam" in c["text"]]))
            return _resp(text=_dom_page(_CARDS))
        # in-page GraphQL attempt: every endpoint fails
        return _resp(text='<html><pre id="h2s-gql-result" data-status="0">FETCH_ERROR /api/remote HTTP 400</pre></html>')

    h2s._attr_label_cache = {}
    with patch("requests.post", return_value=_resp(422, '{"code":"RESP001"}')), \
         patch("requests.get", side_effect=fake_get):
        s = h2s.HollandStayScraper()
        with s.batch_session():
            res = s.scrape(ScrapeTask(source="holland2stay", city_key="24", city_display="Amsterdam",
                                      extra={"availability_ids": ["179", "336"]}))
            # second city goes straight to the page (GraphQL marked dead)
            res2 = s.scrape(ScrapeTask(source="holland2stay", city_key="110", city_display="Diemen",
                                       extra={"availability_ids": ["179", "336"]}))
    assert [l.id for l in res.listings] == ["kastanjelaan-1-108"]
    l = res.listings[0]
    assert l.status == "Available to book" and l.price_raw == "€1395" and l.available_from == "2026-12-01"
    assert l.url == "https://www.holland2stay.com/residences/kastanjelaan-1-108"
    assert res.complete is True
    assert calls["dom"] == 2
    assert [l.id for l in res2.listings] == ["bar-3"]
    assert res2.listings[0].status == "Available in lottery"
    assert mod.H2SApiFetcher._gql_dead_until > 0
    mod.H2SApiFetcher._gql_dead_until = 0.0


def test_dom_zero_links_is_blocked_not_empty(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    with patch("requests.get", return_value=_resp(text=_dom_page([]))):
        with pytest.raises(BlockedError):
            mod.H2SApiFetcher()._dom_products({"filters": {"city": {"in": ["24"]}}})


def test_dom_city_filter_not_applied_raises(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    rotterdam = [{"href": "https://www.holland2stay.com/residences/x", "slug": "x", "title": "X",
                  "text": "G.H. Betzweg 303, Rotterdam Direct boeken"}]
    with patch("requests.get", return_value=_resp(text=_dom_page(rotterdam))):
        with pytest.raises(BlockedError):
            mod.H2SApiFetcher()._dom_products({"filters": {"city": {"in": ["110"]}}, "currentPage": 1})


def test_dutch_status_labels():
    t = "G.H. Betzweg 303, Rotterdam Direct boeken 56,8 m 2 *servicekosten bedragen \u20ac335,00 per maand."
    assert mod._parse_status(t) == 179
    assert mod._parse_price(t) is None
    assert mod._parse_area(t) == "56.8"


def test_dom_unrecognised_statuses_raise(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    cards = [{"href": "https://www.holland2stay.com/residences/x", "slug": "x", "title": "X",
              "text": "Some street 1, Amsterdam  Nieuw label  56 m 2"}]
    with patch("requests.get", return_value=_resp(text=_dom_page(cards))):
        with pytest.raises(BlockedError):
            mod.H2SApiFetcher()._dom_products({"filters": {"city": {"in": ["24"]}}, "currentPage": 1})


def test_detail_page_fills_missing_rent(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    import html as _h
    mod._DETAIL_CACHE.clear()
    detail_txt = "James Wattstraat 77D2\nKale huur\n€ 1.025,00\nServicekosten € 335,00\nBeschikbaar vanaf 15-12-2026"
    detail_page = ('<html><body><pre id="h2s-detail-result">'
                   + _h.escape(json.dumps({"title": "x", "text": detail_txt})) + "</pre></body></html>")
    cards = [{"href": "https://www.holland2stay.com/residences/james-wattstraat-77d2", "slug": "james-wattstraat-77d2",
              "title": "James Wattstraat 77D2", "text": "James Wattstraat 77D2, Amsterdam In loting 26,2 m 2 Studio 3e verdieping Energielabel A"}]
    calls = {"detail": 0}

    def fake_get(url, params=None, **kw):
        if "james-wattstraat" in params["url"]:
            calls["detail"] += 1
            return _resp(text=detail_page)
        return _resp(text=_dom_page(cards))

    with patch("requests.get", side_effect=fake_get):
        f = mod.H2SApiFetcher()
        out = f._dom_products({"filters": {"city": {"in": ["24"]}, "available_to_book": {"in": ["179", "336"]}}, "currentPage": 1})
        out2 = f._dom_products({"filters": {"city": {"in": ["24"]}, "available_to_book": {"in": ["179", "336"]}}, "currentPage": 1})
    it = out["data"]["products"]["items"][0]
    assert it["available_to_book"] == 336
    assert it["basic_rent"] == 1025.0
    assert it["available_startdate"] == "2026-12-15"
    assert it["energy_label"] == "A" and it["floor"] == "3e verdieping" and it["no_of_rooms"] == "Studio"
    assert out2["data"]["products"]["items"][0]["basic_rent"] == 1025.0
    assert calls["detail"] == 1  # cached the second time
