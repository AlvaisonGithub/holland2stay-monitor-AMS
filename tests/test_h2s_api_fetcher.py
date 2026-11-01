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


def test_falls_back_to_zenrows_get_with_pre_wrapped_json(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    wrapped = "<html><body><pre>" + json.dumps(GQL_OK).replace('"', "&quot;") + "</pre></body></html>"
    with patch("requests.post", return_value=_resp(text=CF_PAGE)), \
         patch("requests.get", return_value=_resp(text=wrapped)) as get:
        assert mod.H2SApiFetcher().fetch_gql("q", {"a": 1}) == GQL_OK
        assert "query=q" in get.call_args.kwargs["params"]["url"]
    assert mod.H2SApiFetcher._sticky_strategy == "zenrows-get"


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
