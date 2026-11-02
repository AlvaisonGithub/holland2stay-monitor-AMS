"""智能轮询间隔计算（纯函数，无共享状态）。"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

_AMS = ZoneInfo("Europe/Amsterdam")


def get_interval(cfg) -> tuple[int, bool]:
    """
    根据荷兰本地时间（Europe/Amsterdam）判断当前是否处于高峰期。

    Returns
    -------
    (interval_seconds, is_peak)
    interval_seconds : 本轮应等待的基准秒数（抖动前）
    is_peak          : True 表示当前处于高峰期
    """
    now = datetime.now(_AMS)
    cur = now.hour * 60 + now.minute

    # 夜间低频模式（每天生效）：省 ZenRows 额度。最后一次等待不超过夜间结束时间，
    # 保证早上第一轮准时开始。
    night = _night_wait_seconds(cfg, now)
    if night is not None:
        return night, False

    if cfg.peak_weekdays_only and now.weekday() >= 5:
        return cfg.check_interval, False

    def _in_window(start: str, end: str) -> bool:
        try:
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
        except ValueError:
            return False
        return sh * 60 + sm <= cur <= eh * 60 + em

    if _in_window(cfg.peak_start, cfg.peak_end) or _in_window(
        cfg.peak_start_2, cfg.peak_end_2
    ):
        return cfg.peak_interval, True
    return cfg.check_interval, False


def _hm(value: str) -> int | None:
    try:
        h, m = map(int, value.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def _night_wait_seconds(cfg, now: datetime) -> int | None:
    """
    夜间窗口内返回本轮等待秒数，否则 None。

    .env: NIGHT_INTERVAL（秒，默认 7200，0 = 关闭）、NIGHT_START / NIGHT_END
    （荷兰时间 HH:MM，默认 00:00–07:00，支持跨午夜如 23:00–07:00）。
    """
    interval = int(getattr(cfg, "night_interval", 0) or 0)
    start = _hm(getattr(cfg, "night_start", "") or "")
    end = _hm(getattr(cfg, "night_end", "") or "")
    if interval <= 0 or start is None or end is None or start == end:
        return None

    cur = now.hour * 60 + now.minute
    in_window = (start <= cur < end) if start < end else (cur >= start or cur < end)
    if not in_window:
        return None

    mins_to_end = (end - cur) % (24 * 60)
    secs_to_end = mins_to_end * 60 - now.second
    # 不超过夜间结束；也不比常规间隔更频繁
    return max(int(getattr(cfg, "check_interval", 300)), min(interval, max(60, secs_to_end)))


def apply_jitter(seconds: int, ratio: float = 0.20) -> int:
    """
    在基准等待时间上叠加随机抖动，避免多实例在同一秒发起请求。

    Parameters
    ----------
    seconds : 基准等待时间（秒）
    ratio   : 抖动比例（0–0.5），来自 cfg.jitter_ratio；
              e.g. 0.20 → 实际时间在 [seconds*0.8, seconds*1.2] 内随机

    Returns
    -------
    实际等待时间（秒），最小 5 秒
    """
    delta = seconds * ratio
    return max(5, int(seconds + random.uniform(-delta, delta)))
