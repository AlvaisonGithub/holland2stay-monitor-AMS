"""
scrapers/ourdomain.py — OurDomain / RENTCafe unit-level scraper
================================================================

OurDomain exposes availability through RENTCafe HTML endpoints rather than a
JSON API. The scraper uses a two-step flow:

1. ``floorplans.aspx`` discovers floor plan IDs.
2. ``rcLoadContent.ashx?contentclass=availableunits`` returns concrete units.

Floor-plan level availability is deliberately ignored because it can disagree
with unit-level rows. Listings are keyed by physical unit ID and prefixed with
``od_`` so they cannot collide with Holland2Stay URL slugs.

Cloudflare 403 处理策略（2026-10 修订 v2）
------------------------------------------
SecureRC 的 Cloudflare 保护已从 JS challenge 升级到 WAF firewall 级别。
Railway 等 cloud IP 被 CF WAF firewall rules 直接 403，这是 IP 级封锁——
CloakBrowser（patched Chromium）也无法绕过。

抓取流程改为三阶段：
1. **快速路径**：curl_cffi 尝试（个别 IP / 指纹仍可能通过，保留快速通道）
2. **BrightData API 兜底**：curl_cffi 全指纹失败后，通过 BrightData Web
   Unlocker API 获取 HTML。API 在 BrightData 的住宅代理网络上执行 CF
   challenge solving + JS rendering + CAPTCHA solving，返回干净 HTML。
   免费额度 5,000 请求/月，无需信用卡。
3. **CloakBrowser 最后兜底**：如果 BrightData 未配置 API key，回退到
   CloakBrowser（仅对 JS challenge 有效，对 WAF firewall 无效）。

BrightData 配置
---------------
环境变量::
    BRIGHTDATA_API_KEY=xxx          # 从 brightdata.com 注册获取
    BRIGHTDATA_ZONE=web_unlocker1   # zone 名称
    BRIGHTDATA_MONTHLY_CREDIT_LIMIT=4500  # 月度额度上限（默认 4500，留 500 buffer）

floorplan ID 缓存
-----------------
floorplan IDs 变化频率很低（楼盘户型结构基本不变），缓存后每轮只需
fetch rcLoadContent.ashx（AJAX），不必重复 fetch floorplans.aspx。
缓存有效期默认 12 小时，可通过 FLOORPLAN_CACHE_HOURS 环境变量调整。
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

import curl_cffi.requests as req

from config import CLOAKBROWSER_HEADLESS, get_impersonate, get_proxy_url
from models import Listing

from .base import (
    RATE_LIMIT_BACKOFF,
    AbstractScraper,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    is_cloudflare_body,
)


logger = logging.getLogger(__name__)

# 默认 TLS 指纹池：多浏览器家族 × 多平台，最大化 TLS handshake 差异。
#
# 设计思路
# --------
# Cloudflare 做 fingerprint 跟踪时同家族同平台的差异很小（同样的 JA3/JA4
# hash + 极相似的 h2 settings）。混进 Safari / Firefox / 移动端能显著扩大
# "可用指纹空间"——某家族被烧时还有 3-4 个完全不同的回路可走。
#
# 排除条件
# --------
# - 老版本（chrome99, chrome100, chrome104, chrome110 等）已经被 CF 标
#   "可疑 / 过时浏览器"，不放进默认池
# - tor145 触发 CF 高强度挑战，不当首选
# - safari_beta / chrome_beta 等带 "beta" 后缀的不稳定，不进默认池
#
# 共 8 个，覆盖 4 个家族 (Chrome / Safari / Firefox / Edge) × 桌面/移动
# 不同平台。默认 OURDOMAIN_WAF_RETRIES=4 只取前 4 个；想用全量设为 8。
_DEFAULT_IMPERSONATES: tuple[str, ...] = (
    # ── Chrome 桌面：主力 ──
    "chrome136",          # 最新稳定版（2025 Q2），CF 通常优先放行
    "chrome131",          # 上一稳定版，作为可信备份
    # ── Safari：完全不同的 TLS 栈，Cloudflare 待遇也不同 ──
    "safari18_0",         # macOS Safari 18（2024 秋）
    "safari17_2_ios",     # iOS Safari（移动版 fingerprint 差异巨大）
    # ── Firefox：又一家族，NSS 栈，h2 settings 完全不同 ──
    "firefox135",         # 现代稳定版
    # ── Chrome Android：扩 Chrome 家族但是移动平台 ──
    "chrome131_android",
    # ── 旧但仍现代的 Chrome：fallback ──
    "chrome124",          # 2024 Q2，依然广泛存在
    # ── Edge：Windows 默认浏览器分布的代表 ──
    "edge101",
)


# ────────────────────────────────────────────────────────────────────
# TLS 指纹状态（进程级，跨 scrape() 调用持久化）
# ────────────────────────────────────────────────────────────────────
#
# 痛点
# ----
# SecureRC（OurDomain 用的 RentCafe + Cloudflare）做 per-fingerprint 跟踪：
# 同一指纹短时间内重复打它，会进入"挑战中"状态返 403。原实现每次
# scrape() 都从 chrome131 重新试，等于把"被烧"的指纹反复用——chrome131
# / chrome124 看起来"特别容易 block"其实就是因为它们总是被最先试。
#
# 改进
# ----
# - 成功通过的指纹记录 last_good_at，下次 scrape() 从它开始
# - 403 失败的指纹标记 cooldown_until = now + 30 min，期内不再优先用
# - 排序：[上次 last_good] → [未冷却的] → [冷却中的兜底]
# - 全员冷却时仍可用——冷却中的会被放到队尾，至少给条出路
#
# 这是进程内存，monitor 重启清空（重启后等于"忘掉之前的烧"，从配置
# 顺序重新探）。够用——TLS 指纹热度本身就是分钟级现象。
_FINGERPRINT_COOLDOWN_SEC = 1800  # 30 分钟
_FINGERPRINT_STATE: dict[str, dict[str, float]] = {}
# 例：{"chrome131": {"last_good_at": 1234.5, "cooldown_until": 0.0}}


def _mark_fingerprint_good(impersonate: str) -> None:
    state = _FINGERPRINT_STATE.setdefault(impersonate, {})
    state["last_good_at"] = time.monotonic()
    state["cooldown_until"] = 0.0  # 成功 = 解除冷却


def _mark_fingerprint_blocked(impersonate: str) -> None:
    state = _FINGERPRINT_STATE.setdefault(impersonate, {})
    state["cooldown_until"] = time.monotonic() + _FINGERPRINT_COOLDOWN_SEC


def _is_in_cooldown(impersonate: str) -> bool:
    state = _FINGERPRINT_STATE.get(impersonate, {})
    until = state.get("cooldown_until", 0.0)
    return until > time.monotonic()


class OurDomainScraper(AbstractScraper):
    """Unit-level scraper for OurDomain properties backed by RENTCafe."""

    source = "ourdomain"

    BASE = "https://thisisourdomain.securerc.co.uk/onlineleasing"

    # 每栋楼一份元数据：slug + property_id（用于 RentCafe URL）+ street_address
    # （用于 geocode）+ short_display（listing 名前缀）。
    #
    # street_address 必须是 OurDomain 该楼的**真实街道地址**——unit 名（如
    # "Diemen #6045"）是内部单元号，不可 geocode。每栋楼所有单元共享同一
    # 街道地址（地图上同一个 pin），符合"同栋楼"的物理事实。
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

    # ── 浏览器/BrightData 生命周期管理 ────────────────────────────────
    # CloakBrowser 跨轮复用——首轮创建后后续轮复用同一个实例，避免每轮
    # 重新执行 CF challenge。仅在进程退出或 BlockedError 时关闭重建。
    _BROWSER_MAX_AGE = 7200  # 2 小时

    # ── Floorplan ID 缓存（跨轮复用，减少 API credit 消耗）────────────
    # key: building slug (e.g. "ourdomain-amsterdam-diemen")
    # value: (fp_ids, fp_names_by_id, timestamp)
    _FP_CACHE: dict[str, tuple[list[str], dict[str, str], float]] = {}
    _FP_CACHE_TTL = 43200  # 12 小时（秒）

    def __init__(self) -> None:
        self._browser_fetcher: Optional["RentCafeBrowserFetcher"] = None
        self._browser_origin: Optional[str] = None  # 限制同源复用
        self._browser_created_at: float = 0.0
        # BrightData fetcher (created lazily when needed)
        self._brightdata_fetcher: Optional["BrightDataFetcher"] = None
        # ZenRows fetcher (fallback when BrightData KYC blocks the domain)
        self._zenrows_fetcher: Optional["ZenRowsFetcher"] = None
        # BrightData KYC blacklist — set when BrightData returns KYC error
        # for this session, so _get_fetcher skips BrightData and uses ZenRows
        self._brightdata_kyc_blocked: bool = False

    @staticmethod
    def _fp_cache_key(base: str, slug: str) -> str:
        """Generate cache key for floorplan cache."""
        return f"{slug}"

    @classmethod
    def _get_cached_floorplans(cls, slug: str) -> Optional[tuple[list[str], dict[str, str]]]:
        """Return cached floorplan IDs + names if still valid."""
        entry = cls._FP_CACHE.get(slug)
        if not entry:
            return None
        fp_ids, fp_names, cached_at = entry
        if time.monotonic() - cached_at > cls._FP_CACHE_TTL:
            return None  # expired
        return fp_ids, fp_names

    @classmethod
    def _cache_floorplans(cls, slug: str, fp_ids: list[str], fp_names: dict[str, str]) -> None:
        """Cache floorplan IDs + names."""
        cls._FP_CACHE[slug] = (fp_ids, fp_names, time.monotonic())

    def _get_fetcher(self, entry_url: str):
        """
        Get the appropriate fetcher for CF-protected RENTCafe URLs.

        Priority:
        1. BrightData API (if BRIGHTDATA_API_KEY is set) — handles WAF + JS challenge
        2. ZenRows API (if ZENROWS_API_KEY is set) — no KYC, no robots.txt check
        3. CloakBrowser (fallback) — only handles JS challenge, not WAF

        Returns a fetcher object with fetch_html(url, referer, ajax) interface.
        """
        # Try BrightData first (skip if KYC-blocked for this session)
        if not self._brightdata_kyc_blocked and self._brightdata_fetcher is None:
            try:
                from .brightdata_fetcher import BrightDataFetcher
                bd = BrightDataFetcher(headless=CLOAKBROWSER_HEADLESS)
                if bd.is_configured:
                    self._brightdata_fetcher = bd
                    logger.info("Using BrightData Web Unlocker for CF bypass (zone=%s)", bd._zone)
            except ImportError:
                pass

        if not self._brightdata_kyc_blocked and self._brightdata_fetcher is not None:
            if not self._brightdata_fetcher.is_initialized:
                self._brightdata_fetcher.ensure_initialized(entry_url)
            return self._brightdata_fetcher

        # Try ZenRows (fallback when BrightData not configured or KYC-blocked)
        if self._zenrows_fetcher is None:
            try:
                from .zenrows_fetcher import ZenRowsFetcher
                zr = ZenRowsFetcher(headless=CLOAKBROWSER_HEADLESS)
                if zr.is_configured:
                    self._zenrows_fetcher = zr
                    logger.info("Using ZenRows Fetch API for CF bypass")
            except ImportError:
                pass

        if self._zenrows_fetcher is not None:
            if not self._zenrows_fetcher.is_initialized:
                self._zenrows_fetcher.ensure_initialized(entry_url)
            return self._zenrows_fetcher

        # Fall back to CloakBrowser
        return self._ensure_browser(entry_url)

    def _ensure_browser(self, entry_url: str) -> "RentCafeBrowserFetcher":
        """懒创建或复用 CloakBrowser 实例。BlockedError 后自动重建。"""
        from .rentcafe_browser import RentCafeBrowserFetcher
        from urllib.parse import urlparse

        parsed = urlparse(entry_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        now = time.monotonic()
        if self._browser_fetcher is not None:
            # 超龄 → 主动重建
            if now - self._browser_created_at > self._BROWSER_MAX_AGE:
                logger.info("RentCafe browser 已存活 %.0f 分钟，主动重建",
                            (now - self._browser_created_at) / 60)
                self._close_browser()
            # 不同源 → 不能复用（OurDomain Diemen vs South-East）
            elif self._browser_origin != origin:
                logger.info("RentCafe browser 切换 origin: %s → %s",
                            self._browser_origin, origin)
                self._close_browser()
            else:
                return self._browser_fetcher

        # 新建浏览器
        self._browser_fetcher = RentCafeBrowserFetcher(headless=CLOAKBROWSER_HEADLESS)
        try:
            self._browser_fetcher.ensure_initialized(entry_url)
            self._browser_origin = origin
            self._browser_created_at = time.monotonic()
            logger.info("RentCafe browser 已创建并完成 CF 挑战 (origin=%s)", origin)
            return self._browser_fetcher
        except Exception:
            self._close_browser()
            raise

    def _close_browser(self) -> None:
        """关闭浏览器/BrightData fetcher，释放资源。"""
        if self._browser_fetcher is not None:
            try:
                self._browser_fetcher.close()
            except Exception:
                pass
            self._browser_fetcher = None
            self._browser_origin = None
        # BrightData/ZenRows fetchers are stateless, just drop the reference
        # (no resources to release)
        self._brightdata_fetcher = None
        self._zenrows_fetcher = None
        self._brightdata_kyc_blocked = False

    @contextmanager
    def batch_session(self):
        """
        批次上下文：确保 fetcher 存活（如果已创建），dispatcher 通过此入口
        拿到共享实例。跨轮复用——不再每批次创建/关闭。
        """
        try:
            yield
        except (BlockedError,):
            # CF 会话被标记不可复用：关闭当前 fetcher，下轮重建
            logger.warning("OurDomain fetcher 会话不可复用状态，关闭")
            self._close_browser()
            raise

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        building = self._building_for_task(task)
        slug = building["slug"]
        property_id = building["property_id"]
        base = building.get("base", self.BASE)
        display = task.city_display or building["display"]
        move_in_date = task.extra.get("move_in_date") or _next_month_first()

        proxy = get_proxy_url()
        proxies = {"https": proxy, "http": proxy} if proxy else {}

        floorplans_url = f"{base}/{slug}/floorplans.aspx"

        # ── 阶段 1：curl_cffi 快速路径 ──────────────────────────────
        # 个别 IP / 指纹仍可能通过 CF（CF 策略可能因 IP / 时间段不同而放宽），
        # 保留快速通道避免无谓启动浏览器（~190MB 开销）。
        attempts = _impersonate_attempts()
        tried: list[str] = []
        last_blocked: BlockedError | None = None

        for idx, impersonate in enumerate(attempts, start=1):
            tried.append(impersonate)
            try:
                all_units, complete, fp_names_by_id = self._scrape_once(
                    display=display,
                    base=base,
                    floorplans_url=floorplans_url,
                    property_id=property_id,
                    move_in_date=move_in_date,
                    proxies=proxies,
                    impersonate=impersonate,
                )
                # 成功通过 → 标记这个指纹"好"，下次 scrape() 会优先用它
                _mark_fingerprint_good(impersonate)
                last_blocked = None  # 清除之前的失败状态——成功意味着不需要浏览器兜底
                break
            except BlockedError as e:
                last_blocked = e
                # 标记冷却：30 min 内不再优先选这个指纹（除非全员冷却才兜底）
                _mark_fingerprint_blocked(impersonate)
                if idx < len(attempts):
                    logger.warning(
                        "[%s] OurDomain 403，切换 TLS 指纹重试 %d/%d: %s → %s",
                        display, idx + 1, len(attempts), impersonate, attempts[idx],
                    )
                    continue
                # curl_cffi 全指纹失败 → 进入 CF bypass 路径
                logger.warning(
                    "[%s] OurDomain curl_cffi 全指纹失败 (%s)，切换到 CF bypass 路径 (BrightData → CloakBrowser)",
                    display, ", ".join(tried),
                )
                break
            except RateLimitError:
                raise
            except Exception as e:
                raise ScrapeNetworkError(f"[{display}] OurDomain 抓取失败: {e}") from e
        else:
            # for...else: 循环正常结束（没有 break）= attempts 为空
            all_units, complete, fp_names_by_id = {}, False, {}

        # ── 阶段 2：CF bypass 路径（BrightData API → CloakBrowser 兜底）──
        # curl_cffi 全指纹失败（BlockedError）时，通过 BrightData Web Unlocker
        # API 或 CloakBrowser 获取 HTML。BrightData 优先（能处理 WAF firewall
        # 级封锁），未配置时回退到 CloakBrowser（仅 JS challenge）。
        if last_blocked is not None:
            try:
                all_units, complete, fp_names_by_id = self._scrape_via_browser(
                    display=display,
                    base=base,
                    floorplans_url=floorplans_url,
                    property_id=property_id,
                    move_in_date=move_in_date,
                )
            except BlockedError:
                # 浏览器兜底也失败了 → 上抛最终错误
                raise BlockedError(
                    f"[{display}] OurDomain curl_cffi + CF bypass 均被 403。"
                    f"curl_cffi 已试指纹: {', '.join(tried)}。"
                    "解决方案：1) 设置 BRIGHTDATA_API_KEY（免费 5000 请求/月，"
                    "注册：https://brightdata.com/products/web-unlocker）；"
                    "2) 如果 BrightData 报 KYC 错误，设置 ZENROWS_API_KEY"
                    "（免费 5000 credits/月，无需 KYC，不检查 robots.txt，"
                    "注册：https://app.zenrows.com/register）；"
                    "3) 更换 HTTPS_PROXY 出口 IP。"
                ) from last_blocked

        listings = [
            _to_listing(
                unit,
                base_url=f"{base}/{slug}/floorplans.aspx",
                city_display=display,
                building_label=building.get("short_display") or task.extra.get("building_label"),
                source=self.source,
                default_type=building.get("type"),
                fp_names_by_id=fp_names_by_id,
                street_address=building.get("street_address"),
            )
            for unit in all_units.values()
        ]
        logger.info(
            "[%s] %s 抓取完成: method=%s, units=%d, complete=%s",
            display,
            "OurDomain",
            "brightdata" if last_blocked is not None and self._brightdata_fetcher is not None
            else "zenrows" if last_blocked is not None and self._zenrows_fetcher is not None
            else "browser" if last_blocked is not None
            else f"curl_cffi/{impersonate}" if tried else "none",
            len(listings),
            complete,
        )
        return ScrapeResult(task=task, listings=listings, complete=complete)

    def _scrape_via_browser(
        self,
        *,
        display: str,
        base: str,
        floorplans_url: str,
        property_id: str,
        move_in_date: str,
    ) -> tuple[dict[str, dict], bool, dict[str, str]]:
        """
        CF bypass 抓取：通过 BrightData API 或 CloakBrowser 获取 HTML。

        优先使用 BrightData Web Unlocker API（能处理 WAF firewall 级封锁），
        未配置时回退到 CloakBrowser（仅能处理 JS challenge）。

        如果 BrightData 返回 KYC 错误（某些域名需要住宅代理，免费版无 KYC
        无法使用），自动切换到 ZenRows API（无需 KYC，不检查 robots.txt）。
        如果 ZenRows 也失败，最后回退到 CloakBrowser（仅 JS challenge）。

        Floorplan ID 缓存
        ------------------
        floorplan IDs 变化频率极低（楼盘户型结构不变），缓存 12 小时。
        命中缓存时跳过 floorplans.aspx 请求，节省 1 API credit。
        """
        slug = floorplans_url.split("/")[-2] if "/" in floorplans_url else "default"
        fetcher = self._get_fetcher(floorplans_url)

        all_units: dict[str, dict] = {}
        complete = True

        # Phase 1: floorplans.aspx (with caching)
        cached = self._get_cached_floorplans(slug)
        if cached:
            fp_ids, fp_names_by_id = cached
            logger.debug("[%s] floorplan cache hit: %d IDs", display, len(fp_ids))
        else:
            try:
                fp_html = fetcher.fetch_html(floorplans_url)
            except BlockedError as e:
                # Check if this is a KYC/residential restriction error
                # from BrightData. If so, try ZenRows first (no KYC needed),
                # then CloakBrowser as last resort.
                if "KYC" in str(e) or "residential" in str(e).lower():
                    logger.warning(
                        "[%s] BrightData KYC 限制，切换到 ZenRows: %s",
                        display, e,
                    )
                    # Blacklist BrightData for this session and drop the fetcher
                    self._brightdata_kyc_blocked = True
                    self._brightdata_fetcher = None
                    # Try ZenRows (will be picked up by _get_fetcher)
                    fetcher = self._get_fetcher(floorplans_url)
                    try:
                        fp_html = fetcher.fetch_html(floorplans_url)
                    except BlockedError as e2:
                        # ZenRows also failed — try CloakBrowser as last resort
                        logger.warning(
                            "[%s] ZenRows 也失败，切换到 CloakBrowser: %s",
                            display, e2,
                        )
                        self._zenrows_fetcher = None
                        fetcher = self._ensure_browser(floorplans_url)
                        fp_html = fetcher.fetch_html(floorplans_url)
                else:
                    raise
            fp_ids = _extract_floorplan_ids(fp_html)
            if not fp_ids:
                # Diagnostic logging: show what patterns ARE in the HTML
                # to help identify why the regex didn't match.
                has_subpointer = "subPointerId" in fp_html
                has_myfloorplan = "myFloorPlanId" in fp_html
                has_fp_detail = "FP_Detail_" in fp_html
                has_floorplan_selector = "floor-plan-selector" in fp_html
                has_ffloorplan = "FFloorPlan" in fp_html
                has_data_selenium = "data-selenium-id" in fp_html
                # Check for JS-rendered floorplan data in script tags
                has_fp_js = "floorPlan" in fp_html or "floorplan" in fp_html.lower()
                logger.warning(
                    "[%s] 未发现 floorplan id (html=%d chars). "
                    "Patterns found: subPointerId=%s, myFloorPlanId=%s, "
                    "FP_Detail=%s, floor-plan-selector=%s, FFloorPlan=%s, "
                    "data-selenium-id=%s, floorPlan JS=%s. "
                    "HTML first 1000 chars: %s",
                    display, len(fp_html),
                    has_subpointer, has_myfloorplan,
                    has_fp_detail,
                    has_floorplan_selector, has_ffloorplan,
                    has_data_selenium, has_fp_js,
                    fp_html[:1000],
                )
                return {}, False, {}
            fp_names_by_id = _extract_floorplan_names(fp_html)
            self._cache_floorplans(slug, fp_ids, fp_names_by_id)
            logger.debug("[%s] floorplan cache miss: fetched %d IDs", display, len(fp_ids))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[%s] FP names: %d / %d",
                display, len(fp_names_by_id), len(fp_ids),
            )

        # Phase 2: rcLoadContent.ashx per floorplan
        for fp_id in fp_ids:
            url = (
                f"{base}/rcLoadContent.ashx"
                f"?contentclass=availableunits"
                f"&floorPlans={fp_id}"
                f"&MoveInDate={move_in_date}"
                f"&myolePropertyID={property_id}"
            )
            try:
                unit_html = fetcher.fetch_html(url, referer=floorplans_url, ajax=True)
            except (RateLimitError, BlockedError):
                raise
            except Exception as e:
                complete = False
                logger.error(
                    "[%s] availableunits 失败 fp_id=%s: %s",
                    display, fp_id, e,
                    exc_info=True,
                )
                continue
            for unit in _extract_units(unit_html):
                _merge_unit(all_units, unit, fp_id)
        return all_units, complete, fp_names_by_id

    def _scrape_once(
        self,
        *,
        display: str,
        base: str,
        floorplans_url: str,
        property_id: str,
        move_in_date: str,
        proxies: dict[str, str],
        impersonate: str,
    ) -> tuple[dict[str, dict], bool, dict[str, str]]:
        all_units: dict[str, dict] = {}
        complete = True
        with req.Session(impersonate=impersonate, proxies=proxies) as session:
            fp_html = _get_text(
                session,
                floorplans_url,
                headers=_headers_for(floorplans_url),
            )
            fp_ids = _extract_floorplan_ids(fp_html)
            if not fp_ids:
                logger.warning("[%s] OurDomain 未发现 floorplan id", display)
                return {}, False, {}

            # 顺手抓 FP id→name 映射，只作为 _infer_occupancy 的 sqft 兜底。
            # 主信号是 unit 自己的 sqft，所以这个 dict 拿不到也不影响。
            fp_names_by_id = _extract_floorplan_names(fp_html)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[%s] OurDomain FP names: %d / %d",
                    display, len(fp_names_by_id), len(fp_ids),
                )

            for fp_id in fp_ids:
                url = (
                    f"{base}/rcLoadContent.ashx"
                    f"?contentclass=availableunits"
                    f"&floorPlans={fp_id}"
                    f"&MoveInDate={move_in_date}"
                    f"&myolePropertyID={property_id}"
                )
                try:
                    unit_html = _get_text(
                        session,
                        url,
                        headers=_headers_for(url, referer=floorplans_url, ajax=True),
                    )
                except (RateLimitError, BlockedError):
                    raise
                except Exception as e:
                    complete = False
                    logger.error(
                        "[%s] OurDomain availableunits 失败 fp_id=%s: %s",
                        display, fp_id, e,
                        exc_info=True,
                    )
                    continue
                for unit in _extract_units(unit_html):
                    _merge_unit(all_units, unit, fp_id)
        return all_units, complete, fp_names_by_id

    def _building_for_task(self, task: ScrapeTask) -> dict[str, str]:
        key = (task.city_key or "").strip().lower()
        building = dict(self.BUILDINGS.get(key) or {})
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


def _get_text(session: req.Session, url: str, *, headers: Optional[dict[str, str]] = None) -> str:
    """
    GET text with 429 retry, 403 Cloudflare classification, **+ same-session 403 retry**.

    Same-session 403 retry
    ----------------------
    SecureRC 的 "Just a moment..." 是 Cloudflare JS challenge：第一次访问
    返 403 + 一段 challenge HTML，但**同时也会下发 cf_clearance cookie**。
    curl_cffi 没跑 JS 算不出最终 challenge token，但 cookie 已经攒到
    session 上了——很多时候第二次 GET 同 URL 就直接过了（CF 见到部分
    cookie 会放宽到 light challenge）。

    所以：拿到 403 后**先在同 session 内短退避重试一次**，仍然 403 再
    抛 BlockedError 让上层切指纹。这一步省下大量"换指纹"开销——稳态下
    一个指纹就能稳定服务。

    重试只做 1 次，避免维护期之类的情况下白白多打几次。
    """
    total_wait = 0
    in_session_403_retried = False  # 同 session 内 403 只重试一次

    for attempt, wait in enumerate([0] + list(RATE_LIMIT_BACKOFF)):
        if wait:
            total_wait += wait
            logger.warning(
                "OurDomain 429，第 %d/%d 次退避，等待 %d 秒（累计 %ds）",
                attempt, len(RATE_LIMIT_BACKOFF), wait, total_wait,
            )
            time.sleep(wait)

        resp = session.get(url, headers=headers or {}, timeout=30)
        if resp.status_code == 403:
            body = resp.text[:500]
            is_cf = is_cloudflare_body(body)
            reason = "Cloudflare WAF 屏蔽" if is_cf else "服务拒绝"

            # 同 session 内重试 1 次：CF 可能已经下发了 cf_clearance / 校验 cookie，
            # 第二次 GET 同 URL 就能软通过。只对 Cloudflare 类 403 做（非 CF 的硬
            # 403 重试没意义）。
            if is_cf and not in_session_403_retried:
                in_session_403_retried = True
                logger.info(
                    "OurDomain 首次 403 (Cloudflare)，同 session 内短退避重试一次 url=%s",
                    url,
                )
                time.sleep(2)  # 短等待让 CF cookie 生效
                resp = session.get(url, headers=headers or {}, timeout=30)
                if resp.ok:
                    return resp.text
                # 仍 403 / 其他错 → 落到下方常规处理
                if resp.status_code == 403:
                    body = resp.text[:500]
                    is_cf = is_cloudflare_body(body)
                    reason = "Cloudflare WAF 屏蔽" if is_cf else "服务拒绝"

            if resp.status_code == 403:
                logger.warning(
                    "OurDomain GET HTTP 403 (%s) url=%s body=%r",
                    reason, url, body[:200],
                )
                raise BlockedError(
                    f"OurDomain {reason}（HTTP 403）。等待无法恢复。请尝试："
                    f"1) 更换 HTTPS_PROXY 出口 IP；"
                    f"2) 重启 monitor（重建 curl_cffi session + TLS 指纹）；"
                    f"3) 暂停几小时让 Cloudflare 冷却。"
                )
            # 重试拿到 200 已经 return；走到这里说明拿到了非 403 的其它状态码，
            # 落到下方常规分支处理
        if resp.status_code == 429:
            continue
        if not resp.ok:
            logger.error("OurDomain GET HTTP %d url=%s body=%r", resp.status_code, url, resp.text[:300])
        resp.raise_for_status()
        return resp.text

    raise RateLimitError(
        f"OurDomain 持续返回 429（已退避重试 {len(RATE_LIMIT_BACKOFF)} 次，"
        f"累计等待 {total_wait}s）。请降低轮询频率或配置 HTTPS_PROXY。"
    )


def _impersonate_attempts() -> list[str]:
    """
    生成本轮要尝试的 TLS 指纹列表，**按状态智能排序**。

    顺序逻辑
    --------
    1. 上次成功用过的指纹（last_good_at 最新）排首位——稳态下基本只用 1 个
    2. 未在 cooldown 期的、按配置原顺序
    3. cooldown 中的兜底（万一其它全失败，至少能挣扎一下）

    Returns
    -------
    list[str]，长度受 OURDOMAIN_WAF_RETRIES 限制。
    """
    raw = os.environ.get("OURDOMAIN_IMPERSONATES", "")
    configured = [p.strip() for p in re.split(r"[,|]", raw) if p.strip()]
    candidates = configured or [get_impersonate(), *_DEFAULT_IMPERSONATES]
    unique = list(dict.fromkeys(candidates))
    retries = _env_int("OURDOMAIN_WAF_RETRIES", min(4, len(unique)), min_value=1, max_value=8)

    # ── 按状态分桶 ─────────────────────────────────────────────
    last_good = sorted(
        (imp for imp in unique if _FINGERPRINT_STATE.get(imp, {}).get("last_good_at", 0)),
        key=lambda imp: _FINGERPRINT_STATE[imp].get("last_good_at", 0),
        reverse=True,  # 最近成功的排最前
    )
    cool = [imp for imp in unique if _is_in_cooldown(imp)]
    fresh = [imp for imp in unique if imp not in last_good and imp not in cool]

    # 合并：last_good → fresh → cooldown（去重保序）
    ordered = list(dict.fromkeys([*last_good, *fresh, *cool]))
    return ordered[:retries]


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(min_value, min(value, max_value))


def _headers_for(url: str, *, referer: str = "", ajax: bool = False) -> dict[str, str]:
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    if ajax:
        headers.update({
            "Accept": "text/html, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
    return headers


def _extract_floorplan_ids(html: str) -> list[str]:
    # RENTCafe floorplan IDs can appear in several patterns depending on
    # whether the HTML is server-side rendered (curl_cffi) or JS-rendered
    # (BrightData with render=true, CloakBrowser):
    #
    #   subPointerId=NNNN          — photo gallery onclick (Diemen)
    #   myFloorPlanId=NNNN         — Get Notified / Contact Us links (South-East)
    #   FP_Detail_NNNN             — anchor href in floor-plan list (South-East v2)
    #   data-floorplanid="NNNN"    — JS-hydrated floor-plan-selector elements
    #   floorplanid=NNNN           — query-string in JS variable assignments
    #   "floorPlanId":NNNN         — JSON in script tags (JS-rendered pages)
    #   fpId\s*=\s*['"]?NNNN       — JS variable assignment
    ids = re.findall(r"subPointerId=(\d+)", html)
    if not ids:
        ids = re.findall(r"myFloorPlanId=(\d+)", html)
    if not ids:
        # South-East v2 theme: floor-plan list anchors use #FP_Detail_NNNN
        ids = re.findall(r"FP_Detail_(\d+)", html)
    if not ids:
        # JS-rendered HTML may use data attributes
        ids = re.findall(r'data-floorplanid["\s]*=["\s]*(\d+)', html, re.I)
    if not ids:
        # JSON in script tags
        ids = re.findall(r'"floorPlanId"\s*:\s*(\d+)', html, re.I)
    if not ids:
        # JS variable assignments
        ids = re.findall(r'fpId\s*=\s*["\']?(\d+)', html, re.I)
    if not ids:
        # Generic floorplan id in query strings within JS
        ids = re.findall(r'floorplanid=(\d+)', html, re.I)
    return list(dict.fromkeys(ids))


def _extract_floorplan_names(html: str) -> dict[str, str]:
    """
    从 floorplans.aspx 解析 ``{fp_id: fp_name}``。

    实现踩坑记录
    ------------
    试过两种失败的方案：
    1. ``#FFloorPlan`` dropdown 的 checkbox label —— 那是 JS hydrated 的，
       curl_cffi 看不到（只有 Playwright 等 JS 跑完才能拿到）。
    2. anchor 的 ``title="..."`` 属性 —— Playwright dump 里有 FP 名，但
       production server-side HTML 里 title 全是 ``"1"`` / ``"Max Rent"``
       等 UI 占位文本，不含 FP 名（浏览器里 JS 后期填的 tooltip）。

    **真实可用 source (Diemen 主题)**：每个 FP 的 floor-plan-selector anchor 都带：

        onclick="showDialog('Floor Plan Executive Studio | Furnished | Contract 1-5 years',
                            'photogallery',
                            'imagetype=floorplan&...&subPointerId=1106316&...');"

    第一个参数 ``'Floor Plan {name}'`` 是干净 FP 名，后面同一 onclick 里
    的 ``subPointerId=NNN`` 是对应 FP 的数字 ID。这两者**强耦合在同一 anchor**，
    server-side rendered，跟 ``_extract_floorplan_ids`` 用的 anchor 是一回事。

    **South-East v2 主题**：floor-plan list anchors 使用 ``#FP_Detail_NNNN``
    作为 href，anchor 文本就是 FP 名：

        <a href="...#FP_Detail_1101760">2 Bedroom Superior (monthly income required)</a>

    FP 名字里含 Occupancy 线索（Studio / 1-Bedroom / "1-person max"）——
    SecureRC 单元表本身没有 Beds 字段，只能从这里反推。
    """
    mapping: dict[str, str] = {}
    # Strategy 1 (Diemen theme): showDialog('Floor Plan {name}' ... subPointerId=NNN)
    # 'Floor Plan {name}' ... subPointerId=NNN 之间允许 onclick 内任意其它参数
    # （photogallery / imagetype / galleryId 等）。[^>]*? 限制不跨越 tag 边界，
    # 避免误匹配下一个 anchor 的 subPointerId。
    pattern = re.compile(
        r"showDialog\(\s*'Floor Plan ([^']+)'[^>]*?subPointerId=(\d+)",
        re.IGNORECASE | re.DOTALL,
    )
    for raw_name, fp_id in pattern.findall(html):
        if fp_id in mapping:
            continue
        name = unescape(raw_name).strip()
        if name:
            mapping[fp_id] = name

    # Strategy 2 (South-East v2 theme): <a href="...#FP_Detail_NNNN">{name}</a>
    if not mapping:
        pattern2 = re.compile(
            r'href=["\'][^"\']*#FP_Detail_(\d+)["\'][^>]*>\s*([^<]+?)\s*</a>',
            re.IGNORECASE,
        )
        for fp_id, raw_name in pattern2.findall(html):
            if fp_id in mapping:
                continue
            name = unescape(raw_name).strip()
            if name:
                mapping[fp_id] = name

    return mapping


# ────────────────────────────────────────────────────────────────────
# Occupancy 反推：用 H2S 同样的词汇表，filter 跨 source 才能合并
# ────────────────────────────────────────────────────────────────────
#
# OD SecureRC 单元表没有 Beds / Occupancy 列。线索全在 Floor Plan 名字里：
#
#   "Executive Studio"        → 单人 studio（OD 营销定位 "Students"）
#   "Superior Plus Studio"    → 大 studio（OD 营销定位 "Young Pros & Couples"）
#   "Superior Studio"         → 单人版（"1-person max" 确认）
#   "1-Bedroom Apartment"     → 双人公寓（夫妻 / 情侣）
#   "1-Bedroom Loft"          → 双人 Loft
#   "2-Bedroom" / "3-Bedroom" → 家庭
#
# 优先级：显式 "1-person max" > "Plus Studio" > "Studio" > "1-Bedroom" > "2/3-Bedroom"

def _infer_occupancy(
    sqft: Optional[str] = None,
    fp_names: Optional[list[str]] = None,
) -> Optional[str]:
    """
    OurDomain unit → Occupancy 启发式。

    用 H2S 词汇表（"One" / "Two (only couples)" / "Family (parents with children)"），
    保证 Web `Occupancy` 多选 filter 跨 source 自然合并。

    主信号：unit 自己的物理面积 ``sqft``
    --------------------------------------
    OurDomain 的 RentCafe ``rcLoadContent.ashx?floorPlans=N`` 过滤器**不可靠**
    （实测每个 FP 查询都返回同一组单元，每个单元被关联到全部 8 个 FP，
    无法靠 FP→unit 映射判断单元类型）。

    所以改用 ``sqft`` 作为权威信号——它是 unit 的真实物理属性，从 unit
    table 行里直接抓到：

    - < 30 m²       → "One"（典型单人 studio）
    - 30 – 60 m²    → "Two (only couples)"（大 studio / 1-BR 公寓）
    - >= 60 m²      → "Family (parents with children)"（2+ BR）

    阈值取自荷兰租赁市场常规：单身 studio 18-28 m²，couple 30+ m²，
    家庭 60+ m²（与 H2S Eindhoven Kastanjelaan 59 m² Loft "Two (only couples)"
    一致）。

    兜底：fp_names
    --------------
    当 sqft 拿不到或非数字时，回退到 FP 名字关键词匹配。罕见路径——
    生产 OD 的 rcLoadContent 单元表 sqft 列稳定存在。
    """
    # 主路径：sqft
    if sqft:
        # 复用 models.parse_float（容忍 "22" / "22.5" / "38 - 48 m²" 等格式）
        from models import parse_float
        sqm = parse_float(sqft)
        if sqm is not None:
            if sqm < 30:
                return "One"
            if sqm < 60:
                return "Two (only couples)"
            return "Family (parents with children)"

    # 兜底：FP 名字（sqft 缺失时罕见路径）
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


def _extract_units(html: str) -> list[dict]:
    units: list[dict] = []
    for row in re.finditer(
        r"<tr\b(?=[^>]*\bid=[\"']unitrow_(\d+)[\"'])[^>]*>.*?</tr>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        unit = _extract_unit(row.group(0), row.group(1))
        if unit:
            units.append(unit)
    return units


def _extract_unit(row_html: str, unit_id: str) -> Optional[dict]:
    """
    从单条 unitrow 提取 unit 数据。selenium-id 用 Diemen 主题命名，
    data-label 作为兜底（South-East 等其它 RentCafe 主题）。

    每个字段都传一组 label fallback——data-label 是表格的"用户可见列标题"，
    跨主题稳定（Sq.M. / Apartment / Rent / Deposit / Amenities / Availability）。
    """
    idx_match = re.search(
        r"data-selenium-id=[\"']urow(\d+)[\"']",
        row_html,
        re.IGNORECASE,
    )
    idx = idx_match.group(1) if idx_match else "1"

    apt = _cell_text(row_html, f"Apt{idx}", labels=("Apartment", "Apt", "Unit"))
    if not apt:
        logger.warning("OurDomain 单元缺少 Apt%s: unit_id=%s", idx, unit_id)
        return None

    # Amenity / Floor info：selenium-id Amenity{idx}，fallback 用 data-label
    # "Amenities" / "Floor"（不同主题可能命名其一）
    amenity_html = _cell_html(
        row_html, f"Amenity{idx}", labels=("Amenities", "Floor", "Details"),
    )
    detail_labels = _label_texts(amenity_html)
    detail = ", ".join(detail_labels)
    floor = parse_ourdomain_floor(detail)
    avail_date = _extract_apply_date(row_html)

    sqft = _cell_text(row_html, f"SqFt{idx}", labels=("Sq.M.", "Sq.Ft.", "Size", "Area"))
    rent = _cell_text(row_html, f"Rent{idx}", labels=("Rent",))
    deposit = _cell_text(row_html, f"Deposit{idx}", labels=("Deposit",))
    avail_html = _cell_html(
        row_html, f"AvailDate{idx}", labels=("Availability", "Available", "Apply"),
    )

    # 关键字段全空时打一条 WARNING，方便日后回看哪些主题需要再加 label fallback
    if not sqft and not detail and not rent:
        logger.warning(
            "OurDomain 单元 %s 解析后核心字段全空——可能是新主题 / 新字段名，"
            "row_html 前 300: %s",
            unit_id, row_html[:300],
        )

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

    # 该单元归属的所有 FP 名字——只作 Occupancy 反推的 fallback。
    # 主信号是 unit 自己的 sqft（OurDomain rcLoadContent 的 FP 过滤器不可靠，
    # 每个单元都被关联到全部 8 FP，FP→unit 映射不能信）。
    fp_names: list[str] = []
    if fp_names_by_id:
        for fp_id in unit.get("fp_ids", []):
            name = fp_names_by_id.get(fp_id)
            if name:
                fp_names.append(name)
    occupancy = _infer_occupancy(sqft=sqft, fp_names=fp_names)

    features = [
        f"Unit: {apt}",
        f"Building: {city_display}",
    ]
    if street_address:
        # 真实街道地址（建筑级，所有 unit 共享）。供 geocode pipeline 用
        # `Address:` 优先级最高（unit name "Diemen #6045" 不可 geocode）。
        features.append(f"Address: {street_address}")
    if default_type:
        features.append(f"Type: {default_type}")
    if sqft:
        features.append(f"Area: {sqft} m²")
    if occupancy:
        # 用 H2S 同样的 "Occupancy: ..." 前缀写进 features，Web filter
        # `get_feature_values("Occupancy")` 会自动 distinct 出来
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


def _format_listing_name(
    city_display: str,
    apt: str,
    building_label: Optional[str] = None,
) -> str:
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


def _cell_html(row_html: str, selenium_id: str, *, labels: tuple[str, ...] = ()) -> str:
    """
    抓单元 cell 的 HTML，**多策略容错**：

    1. `data-selenium-id="<selenium_id>"` 精确匹配（Diemen 用这套）
    2. `data-label="<label>"` 任一匹配（South-East 等其它 RentCafe 主题
       可能用 data-label 而 selenium-id 不同；data-label 是用户可见标签，
       跨主题更稳定）

    任一策略命中即返回；都没命中返回空串。

    为什么需要兜底
    --------------
    OurDomain 的两个楼盘用了不同的 RentCafe 主题。Diemen 的 row 是
    `<td data-selenium-id="SqFt1" data-label="Sq.M.">27</td>`，South-East
    实测 SqFt cell 的 selenium-id 跟 Diemen 不同。data-label 在两套主题
    里都稳定存在，作为兜底胜率更高。
    """
    # 策略 1：selenium-id 精确匹配
    pattern = (
        r"<(?P<tag>th|td)\b"
        rf"(?=[^>]*data-selenium-id=[\"']{re.escape(selenium_id)}[\"'])"
        r"[^>]*>(?P<body>.*?)</(?P=tag)>"
    )
    m = re.search(pattern, row_html, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group("body")

    # 策略 2：data-label 兜底（允许任一 label 命中）
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
        avail_html,
        re.IGNORECASE | re.DOTALL,
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
        row_html,
        re.IGNORECASE | re.DOTALL,
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
