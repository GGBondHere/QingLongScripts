#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
名称：B站每日任务
用途：漫画签到、观看、分享、投币，查询等级、经验与升级进度。

青龙配置：
- 执行：task QingLongScripts/Bilibili.py now。
- 独立任务：启用、单次类型；日常由 daily_all.py 执行。
- 依赖：requests。
- 通知：在青龙面板配置，合集只发送汇总通知。

环境变量：
- BILIBILI_COOKIE：完整 Cookie，包含 SESSDATA 和 bili_jct，单账号。
- BILIBILI_COIN_NUM（可选）：每天投币目标 0~5，默认 1；0 关闭投币。
- BILIBILI_COIN_RESERVE（可选）：投币后最低硬币余额，默认 100。
- BILIBILI_COIN_TYPE（可选）：1=优先关注 UP，2=热门视频，默认 1。
- BILIBILI_SILVER2COIN（可选）：是否兑换银瓜子，默认 false。

凭证获取：
1. 登录 https://www.bilibili.com/，F12 → 网络，刷新页面并选择本站请求。
2. 复制请求头 Cookie 的完整值，填入 BILIBILI_COOKIE。

填写示例：
- SESSDATA=实际值; bili_jct=实际值; DedeUserID=实际值; 其他Cookie字段

使用说明：
- 可选变量不填即使用默认值；投币消耗账号硬币。
- 停用标识：bilibili；缺凭证或已停用时跳过。详见 docs/bilibili.md。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

# 运行配置

def skip_inactive_service(raw: str, disabled_value: str, daily_child: bool) -> bool:
    """未配置凭证或已停用时跳过。"""
    disabled = {item.strip().casefold() for item in re.split(r"[,;|&\n]+", disabled_value)}
    if raw.strip() and not disabled.intersection({"bilibili", "b站每日任务"}):
        return False
    reason = "未配置凭证" if not raw.strip() else "已关闭"
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    print(f"跳过：{reason}")
    if daily_child:
        payload = {"version": 1, "status": "skip", "details": [reason]}
        print("__DAILY_ALL_RESULT__=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return True


if __name__ == "__main__" and skip_inactive_service(
    os.getenv("BILIBILI_COOKIE", ""), os.getenv("DAILY_ALL_DISABLED", ""),
    os.getenv("DAILY_ALL_CHILD", "").strip() == "1",
):
    raise SystemExit(0)


import requests


# 配置与数据模型

def configure_utf8_output() -> None:
    """确保 Windows 本地终端可输出中文和状态符号。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, TypeError, ValueError):
                pass

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
REWARD_URL = "https://api.bilibili.com/x/member/web/exp/reward"
EXP_LOG_URL = "https://api.bilibili.com/x/member/web/exp/log"
POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"
RANKING_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
FOLLOWINGS_URL = "https://api.bilibili.com/x/relation/followings"
SPACE_ARC_URL = "https://api.bilibili.com/x/space/arc/search"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
WATCH_URL = "https://api.bilibili.com/x/v2/history/report"
SHARE_URL = "https://api.bilibili.com/x/web-interface/share/add"
COIN_URL = "https://api.bilibili.com/x/web-interface/coin/add"
MANGA_SIGN_URL = "https://manga.bilibili.com/twirp/activity.v1.Activity/ClockIn"
LIVE_STATUS_URL = "https://api.live.bilibili.com/pay/v1/Exchange/getStatus"
SILVER2COIN_URL = "https://api.live.bilibili.com/xlive/revenue/v1/wallet/silver2coin"

REQUEST_TIMEOUT = (5, 15)
GET_ATTEMPTS = 2
GET_RETRY_INTERVAL = 1.0
LOGIN_CHECK_ATTEMPTS = 3
LOGIN_CHECK_INTERVAL = 2.0
POST_MIN_INTERVAL = 1.0
BEIJING_TZ = timezone(timedelta(hours=8))
DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Settings:
    cookie: str
    cookie_fields: dict[str, str]
    csrf: str
    coin_target: int
    coin_reserve: int
    coin_type: int
    silver2coin: bool
    daily_child: bool


@dataclass(frozen=True)
class UserInfo:
    name: str
    uid: int
    is_login: bool
    coin_balance: float
    vip_type: int
    current_exp: int
    current_level: int
    next_exp: Any


@dataclass(frozen=True)
class TaskOutcome:
    state: str
    message: str

    @property
    def ok(self) -> bool:
        return self.state != "failed"


@dataclass(frozen=True)
class CoinPlan:
    count: int
    state: str
    completed_count: int
    message: str


@dataclass(frozen=True)
class UpgradeInfo:
    next_level: str
    remaining_exp: int
    remaining_days: str


class ApiError(RuntimeError):
    """无法获得继续执行所必需的数据。"""


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_message(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)\b(cookie|set-cookie|authorization)\s*[:=]\s*[^\s,]+",
        r"\1=[已隐藏]",
        text,
    )
    return text[:180]


# 环境变量与凭证解析

def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须填写整数") from exc


def _env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = str(env.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} 只能填写 true 或 false")


def parse_cookie(raw: str) -> tuple[dict[str, str], str]:
    """解析 Cookie 值；兼容分号后有无空格，并保留值中的等号。"""
    text = str(raw or "").strip()

    fields: dict[str, str] = {}
    pairs: list[str] = []
    for part in text.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        fields[key] = value
        pairs.append(f"{key}={value}")
    return fields, "; ".join(pairs)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    fields, cookie = parse_cookie(str(env.get("BILIBILI_COOKIE", "") or ""))
    if not cookie:
        raise ValueError("未找到环境变量 BILIBILI_COOKIE")

    missing = [name for name in ("SESSDATA", "bili_jct") if not fields.get(name)]
    if missing:
        raise ValueError(
            "BILIBILI_COOKIE 缺少必要字段：" + "、".join(missing)
        )

    coin_target = _env_int(env, "BILIBILI_COIN_NUM", 1)
    if not 0 <= coin_target <= 5:
        raise ValueError("BILIBILI_COIN_NUM 只能填写 0 到 5")

    coin_reserve = _env_int(env, "BILIBILI_COIN_RESERVE", 100)
    if coin_reserve < 0:
        raise ValueError("BILIBILI_COIN_RESERVE 不能小于 0")

    coin_type = _env_int(env, "BILIBILI_COIN_TYPE", 1)
    if coin_type not in {1, 2}:
        raise ValueError("BILIBILI_COIN_TYPE 只能填写 1 或 2")

    return Settings(
        cookie=cookie,
        cookie_fields=fields,
        csrf=fields["bili_jct"],
        coin_target=coin_target,
        coin_reserve=coin_reserve,
        coin_type=coin_type,
        silver2coin=_env_bool(env, "BILIBILI_SILVER2COIN", False),
        daily_child=str(env.get("DAILY_ALL_CHILD", "") or "").strip() == "1",
    )


# 网络请求与业务逻辑

class BilibiliClient:
    """带超时、只读重试和 POST 固定限速的会话。"""

    def __init__(
        self,
        cookie: str,
        *,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = requests.Session() if session is None else session
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Cookie": cookie,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        self._sleep = sleeper
        self._clock = clock
        self._last_post_at: float | None = None

    @staticmethod
    def _decode_response(response: Any) -> dict[str, Any]:
        status = _safe_int(getattr(response, "status_code", 0), 0)
        try:
            payload = response.json()
        except Exception:
            return {
                "code": -998,
                "message": f"HTTP {status} 未返回有效 JSON",
                "_http_status": status,
            }
        if not isinstance(payload, dict):
            return {
                "code": -998,
                "message": f"HTTP {status} 返回格式异常",
                "_http_status": status,
            }
        payload = dict(payload)
        payload["_http_status"] = status
        if status >= 400 and _safe_int(payload.get("code"), 0) == 0:
            payload["code"] = -status
            payload["message"] = f"HTTP {status}"
        return payload

    def get(self, url: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        last_error = "查询请求失败"
        for attempt in range(GET_ATTEMPTS):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
                payload = self._decode_response(response)
                if payload.get("_http_status", 0) < 500 or attempt + 1 >= GET_ATTEMPTS:
                    return payload
                last_error = _clean_message(payload.get("message")) or last_error
            except requests.RequestException as exc:
                last_error = f"网络请求异常（{type(exc).__name__}）"
                if attempt + 1 >= GET_ATTEMPTS:
                    break
            if attempt + 1 < GET_ATTEMPTS:
                self._sleep(GET_RETRY_INTERVAL)
        return {"code": -999, "message": last_error, "_http_status": 0}

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._last_post_at is not None:
            remaining = POST_MIN_INTERVAL - (self._clock() - self._last_post_at)
            if remaining > 0:
                self._sleep(remaining)
        try:
            response = self.session.post(
                url,
                data=data,
                headers=dict(headers or {}),
                timeout=REQUEST_TIMEOUT,
            )
            return self._decode_response(response)
        except requests.RequestException as exc:
            return {
                "code": -999,
                "message": f"网络请求异常（{type(exc).__name__}）",
                "_http_status": 0,
            }
        finally:
            self._last_post_at = self._clock()

    def close(self) -> None:
        """释放底层连接池。"""
        self.session.close()


def get_user_info(client: BilibiliClient) -> UserInfo:
    payload = client.get(NAV_URL, params={"_": int(time.time() * 1000)})
    if _safe_int(payload.get("code"), -1) != 0:
        raise ApiError(_clean_message(payload.get("message")) or "登录状态查询失败")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ApiError("登录状态接口返回格式异常")
    level = data.get("level_info") or {}
    if not isinstance(level, dict):
        level = {}
    return UserInfo(
        name=str(data.get("uname") or "未知账号"),
        uid=_safe_int(data.get("mid"), 0),
        is_login=bool(data.get("isLogin")),
        coin_balance=_safe_float(data.get("money"), 0.0),
        vip_type=_safe_int(data.get("vipType"), 0),
        current_exp=_safe_int(level.get("current_exp"), 0),
        current_level=_safe_int(level.get("current_level"), 0),
        next_exp=level.get("next_exp"),
    )


def get_verified_user(
    client: BilibiliClient,
    *,
    attempts: int = LOGIN_CHECK_ATTEMPTS,
    interval: float = LOGIN_CHECK_INTERVAL,
    sleeper: Callable[[float], None] = time.sleep,
) -> UserInfo:
    """多次只读确认登录状态，过滤接口偶发返回的未登录结果。"""
    total = max(1, int(attempts))
    last_error = "账号未登录"
    saw_logged_out = False
    for index in range(total):
        try:
            user = get_user_info(client)
            if user.is_login:
                return user
            saw_logged_out = True
            last_error = "账号未登录"
        except ApiError as exc:
            last_error = str(exc)
        if index + 1 < total:
            sleeper(max(0.0, float(interval)))

    if saw_logged_out and last_error == "账号未登录":
        raise ApiError(
            f"连续 {total} 次查询均显示账号未登录，请重新抓取完整 BILIBILI_COOKIE"
        )
    raise ApiError(f"连续 {total} 次登录状态查询均未成功（{last_error}）")


def get_daily_reward(client: BilibiliClient) -> dict[str, Any] | None:
    payload = client.get(REWARD_URL)
    if _safe_int(payload.get("code"), -1) != 0:
        return None
    data = payload.get("data")
    return dict(data) if isinstance(data, dict) else None


def get_today_exp_items(client: BilibiliClient) -> list[dict[str, Any]] | None:
    """读取今日经验明细；接口失败时返回 None，不能伪装成空明细。"""
    payload = client.get(
        EXP_LOG_URL,
        params={"jsonp": "jsonp", "_": int(time.time() * 1000)},
    )
    if _safe_int(payload.get("code"), -1) != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        return None
    items = data["list"]
    today = beijing_now().strftime("%Y-%m-%d")
    return [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("time", "")).split(" ", 1)[0] == today
    ]


def get_today_exp_total(client: BilibiliClient) -> int | None:
    items = get_today_exp_items(client)
    if items is None:
        return None
    return sum(_safe_int(item.get("delta"), 0) for item in items)


def resolve_today_exp(
    *,
    logged_exp: int | None,
    current_before: int,
    current_after: int,
    has_completed_exp_task: bool,
    logged_exp_before: int | None = None,
) -> int | None:
    """合并任务前后经验快照与本次总经验差值，不使用固定每日经验。"""
    same_run_delta = max(0, int(current_after) - int(current_before))
    confirmed = [value for value in (logged_exp, same_run_delta) if value is not None and value > 0]
    if logged_exp_before is not None:
        confirmed.append(max(0, int(logged_exp_before)) + same_run_delta)
    if confirmed:
        return max(confirmed)
    if has_completed_exp_task:
        return None
    return 0 if logged_exp == 0 else None


def get_today_coin_exp_from_log(client: BilibiliClient) -> int | None:
    """返回今日投币经验；查询失败返回 None，不能与确实为 0 混淆。"""
    payload = client.get(EXP_LOG_URL, params={"jsonp": "jsonp"})
    if _safe_int(payload.get("code"), -1) != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        return None
    today = beijing_now().strftime("%Y-%m-%d")
    return sum(
        max(0, _safe_int(item.get("delta"), 0))
        for item in data["list"]
        if isinstance(item, dict)
        and str(item.get("time", "")).split(" ", 1)[0] == today
        and str(item.get("reason") or "") == "视频投币奖励"
    )


def reward_completed(
    reward: Mapping[str, Any] | None,
    key: str,
    *,
    coin_target: int = 1,
) -> bool:
    if not reward:
        return False
    if key == "coins":
        return _safe_int(reward.get("coins"), 0) >= max(0, min(5, coin_target)) * 10
    return bool(reward.get(key))


def classify_task_outcome(
    *,
    completed_before: bool,
    response: Mapping[str, Any] | None,
    completed_after: bool,
    success_message: str,
) -> TaskOutcome:
    """根据前后任务状态判定结果，避免把重复执行误报成失败。"""
    if completed_before:
        return TaskOutcome("already", "今日已完成")
    response_ok = response is not None and _safe_int(response.get("code"), -1) == 0
    if response_ok and completed_after:
        return TaskOutcome("success", success_message)
    if completed_after:
        return TaskOutcome("already", "今日已完成")
    if response_ok:
        return TaskOutcome("failed", "接口返回成功，但官方任务状态未完成")

    message = _clean_message((response or {}).get("message"))
    lowered = message.lower()
    direct_already = any(
        token in lowered
        for token in ("今日已完成", "今日已分享", "已经完成", "already completed")
    )
    if direct_already:
        return TaskOutcome("already", "今日已完成")
    return TaskOutcome("failed", message or "接口请求失败且任务仍未完成")


def calculate_coin_plan(
    *,
    balance: float,
    today_coin_exp: int,
    daily_target: int,
    reserve: int,
) -> CoinPlan:
    target = max(0, min(5, int(daily_target)))
    completed = max(0, min(5, int(today_coin_exp) // 10))
    if target == 0:
        return CoinPlan(0, "disabled", completed, "已关闭投币")
    if completed >= target:
        return CoinPlan(0, "already", completed, f"今日已完成 {completed}/{target} 枚")

    remaining = target - completed
    spendable = max(0, math.floor(float(balance) - int(reserve) + 1e-9))
    if spendable <= 0:
        return CoinPlan(
            0,
            "protected",
            completed,
            f"余额 {balance:g} 枚，已到保护线 {reserve} 枚",
        )
    count = min(remaining, spendable)
    return CoinPlan(count, "ready", completed, f"本次计划投 {count} 枚")


def calculate_upgrade(
    *,
    current_level: int,
    current_exp: int,
    next_exp: Any,
    today_exp: int | None,
) -> UpgradeInfo:
    try:
        target = int(next_exp)
        current = int(current_exp)
    except (TypeError, ValueError):
        return UpgradeInfo("已满级", 0, "已满级")
    if target <= current or target <= 0:
        return UpgradeInfo("已满级", 0, "已满级")

    remaining = target - current
    days = (
        f"{(remaining + int(today_exp) - 1) // int(today_exp)}天"
        if today_exp is not None and int(today_exp) > 0
        else "未知"
    )
    return UpgradeInfo(f"LV{int(current_level) + 1} / {target}", remaining, days)


def get_popular_videos(client: BilibiliClient, count: int = 10) -> list[dict[str, Any]]:
    payload = client.get(POPULAR_URL, params={"pn": 1, "ps": count})
    data = payload.get("data") if _safe_int(payload.get("code"), -1) == 0 else None
    items = data.get("list", []) if isinstance(data, dict) else []
    if not items:
        payload = client.get(RANKING_URL, params={"rid": 1, "type": "all"})
        data = payload.get("data") if _safe_int(payload.get("code"), -1) == 0 else None
        items = data.get("list", []) if isinstance(data, dict) else []

    videos: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("aid"):
            continue
        owner = item.get("owner") or {}
        videos.append(
            {
                "aid": item.get("aid"),
                "bvid": item.get("bvid"),
                "cid": item.get("cid") or 0,
                "title": str(item.get("title") or "未命名视频"),
                "owner": str(owner.get("name") or "") if isinstance(owner, dict) else "",
            }
        )
    return videos


def get_followed_videos(
    client: BilibiliClient,
    uid: int,
    *,
    desired: int = 5,
) -> list[dict[str, Any]]:
    payload = client.get(
        FOLLOWINGS_URL,
        params={"vmid": uid, "pn": 1, "ps": 50, "order": "desc", "order_type": "attention"},
    )
    data = payload.get("data") if _safe_int(payload.get("code"), -1) == 0 else None
    followings = data.get("list", []) if isinstance(data, dict) else []
    videos: list[dict[str, Any]] = []
    for following in followings[:10]:
        if not isinstance(following, dict) or not following.get("mid"):
            continue
        result = client.get(
            SPACE_ARC_URL,
            params={
                "mid": following["mid"],
                "pn": 1,
                "ps": 2,
                "tid": 0,
                "order": "pubdate",
                "keyword": "",
            },
        )
        result_data = result.get("data") if _safe_int(result.get("code"), -1) == 0 else None
        result_list = result_data.get("list", {}) if isinstance(result_data, dict) else {}
        entries = result_list.get("vlist", []) if isinstance(result_list, dict) else []
        for item in entries:
            if isinstance(item, dict) and item.get("aid"):
                videos.append(
                    {
                        "aid": item.get("aid"),
                        "bvid": item.get("bvid"),
                        "cid": item.get("cid") or 0,
                        "title": str(item.get("title") or "未命名视频"),
                        "owner": str(item.get("author") or ""),
                    }
                )
        if len(videos) >= desired:
            break
    return videos


def ensure_video_cid(client: BilibiliClient, video: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(video)
    if result.get("cid"):
        return result
    params = {"bvid": result["bvid"]} if result.get("bvid") else {"aid": result.get("aid")}
    payload = client.get(VIEW_URL, params=params)
    data = payload.get("data") if _safe_int(payload.get("code"), -1) == 0 else None
    if isinstance(data, dict):
        result["cid"] = data.get("cid") or 0
    return result


def recheck_task(
    client: BilibiliClient,
    key: str,
    *,
    attempts: int = 2,
    interval: float = 1.0,
    coin_target: int = 1,
) -> bool:
    for index in range(attempts):
        reward = get_daily_reward(client)
        if reward_completed(reward, key, coin_target=coin_target):
            return True
        if index + 1 < attempts:
            time.sleep(interval)
    return False


def run_manga_sign(client: BilibiliClient) -> TaskOutcome:
    response = client.post(MANGA_SIGN_URL, data={"platform": "android"})
    if _safe_int(response.get("code"), -1) == 0:
        return TaskOutcome("success", "签到成功")
    message = _clean_message(response.get("msg") or response.get("message"))
    lowered = message.lower()
    if any(token in lowered for token in ("duplicate", "已经签到", "已签到", "重复签到")):
        return TaskOutcome("already", "今日已完成")
    return TaskOutcome("failed", message or "漫画签到失败")


def run_watch(
    client: BilibiliClient,
    csrf: str,
    video: Mapping[str, Any] | None,
    reward_before: Mapping[str, Any] | None,
) -> TaskOutcome:
    completed_before = reward_completed(reward_before, "watch")
    if completed_before:
        return classify_task_outcome(
            completed_before=True,
            response=None,
            completed_after=False,
            success_message="观看任务完成",
        )
    if not video:
        return TaskOutcome("failed", "无法获取用于观看任务的视频")
    selected = ensure_video_cid(client, video)
    if not selected.get("cid"):
        return TaskOutcome("failed", "无法获取视频 cid")
    response = client.post(
        WATCH_URL,
        data={
            "aid": selected.get("aid"),
            "cid": selected.get("cid"),
            "progres": 300,
            "csrf": csrf,
        },
        headers={"Referer": f"https://www.bilibili.com/video/{selected.get('bvid') or ''}"},
    )
    completed_after = recheck_task(client, "watch", attempts=3)
    return classify_task_outcome(
        completed_before=False,
        response=response,
        completed_after=completed_after,
        success_message=f"观看《{selected.get('title')}》300秒",
    )


def run_share(
    client: BilibiliClient,
    csrf: str,
    video: Mapping[str, Any] | None,
    reward_before: Mapping[str, Any] | None,
) -> TaskOutcome:
    completed_before = reward_completed(reward_before, "share")
    if completed_before:
        return classify_task_outcome(
            completed_before=True,
            response=None,
            completed_after=False,
            success_message="分享任务完成",
        )
    if not video:
        return TaskOutcome("failed", "无法获取用于分享任务的视频")
    response = client.post(
        SHARE_URL,
        data={
            "aid": video.get("aid"),
            "bvid": video.get("bvid") or "",
            "csrf": csrf,
        },
    )
    completed_after = recheck_task(client, "share", attempts=3)
    return classify_task_outcome(
        completed_before=False,
        response=response,
        completed_after=completed_after,
        success_message=f"分享《{video.get('title')}》成功",
    )


def run_coin(
    client: BilibiliClient,
    settings: Settings,
    user: UserInfo,
    reward_before: Mapping[str, Any] | None,
) -> TaskOutcome:
    if reward_before is not None:
        coin_exp = _safe_int(reward_before.get("coins"), 0)
    else:
        coin_exp = get_today_coin_exp_from_log(client)
        if coin_exp is None:
            return TaskOutcome("failed", "无法确认今日投币数量，已阻止投币")
    plan = calculate_coin_plan(
        balance=user.coin_balance,
        today_coin_exp=coin_exp,
        daily_target=settings.coin_target,
        reserve=settings.coin_reserve,
    )
    if plan.state == "disabled":
        return TaskOutcome("skipped", "已关闭")
    if plan.state == "already":
        return TaskOutcome("already", plan.message)
    if plan.state == "protected":
        return TaskOutcome("skipped", plan.message)

    candidates = (
        get_followed_videos(client, user.uid, desired=max(5, plan.count + 2))
        if settings.coin_type == 1
        else []
    )
    popular = (
        get_popular_videos(client, count=10)
        if len(candidates) < plan.count
        else []
    )
    seen: set[Any] = set()
    merged: list[dict[str, Any]] = []
    for video in candidates + popular:
        aid = video.get("aid")
        if aid and aid not in seen:
            seen.add(aid)
            merged.append(video)
    if not merged:
        return TaskOutcome("failed", "无法获取用于投币的视频")

    success_count = 0
    last_message = "没有可投币的视频"
    for video in merged:
        response = client.post(
            COIN_URL,
            data={
                "aid": video.get("aid"),
                "bvid": video.get("bvid") or "",
                "multiply": 1,
                "select_like": 0,
                "cross_domain": "true",
                "csrf": settings.csrf,
            },
        )
        if _safe_int(response.get("code"), -1) == 0:
            success_count += 1
            if success_count >= plan.count:
                break
            continue
        last_message = _clean_message(response.get("message")) or "投币请求失败"
        if _safe_int(response.get("code"), 0) == 34005:
            continue
        break

    target = settings.coin_target
    if success_count >= plan.count:
        today_count = min(target, plan.completed_count + success_count)
        remaining_balance = user.coin_balance - success_count
        return TaskOutcome(
            "success",
            f"本次投币 {success_count} 枚，今日 {today_count}/{target} 枚，余额约 {remaining_balance:g} 枚",
        )

    completed_after = recheck_task(
        client,
        "coins",
        coin_target=settings.coin_target,
    )
    if completed_after:
        return TaskOutcome("already", f"今日已完成 {target}/{target} 枚")
    return TaskOutcome("failed", f"投币未完成：{last_message}")


def run_silver2coin(client: BilibiliClient, settings: Settings) -> TaskOutcome:
    if not settings.silver2coin:
        return TaskOutcome("skipped", "未开启")
    response = client.post(SILVER2COIN_URL, data={"csrf": settings.csrf})
    if _safe_int(response.get("code"), -1) == 0:
        return TaskOutcome("success", "银瓜子兑换硬币成功")
    message = _clean_message(response.get("message") or response.get("msg"))
    if any(token in message for token in ("已兑换", "已经兑换", "今日兑换")):
        return TaskOutcome("already", "今日已完成")
    if "不足" in message:
        return TaskOutcome("skipped", message)
    return TaskOutcome("failed", message or "银瓜子兑换失败")


def get_live_status(client: BilibiliClient) -> dict[str, Any]:
    payload = client.get(LIVE_STATUS_URL)
    data = payload.get("data") if _safe_int(payload.get("code"), -1) == 0 else None
    return dict(data) if isinstance(data, dict) else {}


def wait_today_exp_refresh(
    client: BilibiliClient,
    *,
    should_wait: bool,
    wait_seconds: int = 10,
    interval: int = 2,
) -> int | None:
    best = get_today_exp_total(client)
    if not should_wait:
        return best
    rounds = max(1, wait_seconds // interval)
    for _ in range(rounds):
        time.sleep(interval)
        current = get_today_exp_total(client)
        if current is not None:
            best = current if best is None else max(best, current)
    return best


# 日志、通知与合集结果

def result_status(outcomes: list[tuple[str, TaskOutcome]]) -> str:
    """汇总真实失败与策略性跳过，供日志、通知和合集共同使用。"""
    states = {outcome.state for _, outcome in outcomes}
    if "failed" in states:
        return "fail"
    if "skipped" in states:
        return "warn"
    return "success"


def _daily_task_text(label: str, outcome: TaskOutcome) -> str:
    """把单项结果压缩成合集通知中的短文本。"""
    if outcome.state in {"success", "already"}:
        if label == "漫画签到":
            return "漫画已签到"
        if label == "观看视频":
            return "观看完成"
        if label == "分享任务":
            return "分享完成"
        if label == "投币任务":
            matched = re.search(r"今日(?:已完成)?\s*(\d+/\d+)\s*枚", outcome.message)
            return f"投币{matched.group(1)}" if matched else f"投币：{outcome.message}"
        if label == "瓜子兑换":
            return "瓜子兑换完成"
    return f"{label}：{outcome.message}"


def build_daily_details(
    outcomes: list[tuple[str, TaskOutcome]],
    *,
    today_exp: int | None,
    user_after: UserInfo,
    upgrade: UpgradeInfo,
) -> list[str]:
    """生成 daily_all.py 使用的稳定摘要，不依赖解析人类日志。"""
    exp_text = f"+{today_exp}" if today_exp is not None else "暂未同步"
    return [
        "｜".join(_daily_task_text(label, outcome) for label, outcome in outcomes),
        f"经验 {exp_text}，当前{user_after.current_exp}，升级还需{upgrade.remaining_days}",
    ]


def emit_daily_result(status: str, details: list[str], daily_child: bool) -> None:
    if not daily_child:
        return
    payload = {"version": 1, "status": status, "details": details}
    print(DAILY_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def format_notification(
    user: UserInfo,
    outcomes: list[tuple[str, TaskOutcome]],
    *,
    today_exp: int | None,
    user_after: UserInfo,
    upgrade: UpgradeInfo,
    live: Mapping[str, Any],
    elapsed: float = 0,
) -> str:
    status = result_status(outcomes)
    icon = {"success": "✅", "warn": "⚠️", "fail": "❌"}[status]
    lines = ["📋 任务结果", "", f"{icon} {user.name}", ""]
    for label, outcome in outcomes:
        lines.append(f"{label}：{outcome.message}")
    lines.extend(
        [
            "",
            "📊 数据统计",
            f"等级：LV{user_after.current_level}",
            f"今日经验：{today_exp if today_exp is not None else '暂未同步'}",
            f"当前经验：{user_after.current_exp}",
            f"下一等级：{upgrade.next_level}",
            f"升级还差：{upgrade.remaining_exp} 经验",
            f"升级还需：{upgrade.remaining_days}",
            f"硬币：{live.get('coin', user_after.coin_balance)}",
            "",
            f"⏱️ 耗时：{max(0, int(round(elapsed)))}秒",
            f"🕒 完成：{beijing_now().strftime('%m-%d %H:%M')}",
        ]
    )
    return "\n".join(lines)


def system_notify(title: str, content: str, daily_child: bool) -> str:
    if daily_child:
        return "由合集统一发送"
    try:
        QLAPI.systemNotify({"title": title, "content": content})  # type: ignore[name-defined]
        return "已发送"
    except NameError:
        return "当前环境没有 QLAPI，已跳过"
    except Exception as exc:
        return f"发送失败（{type(exc).__name__}）"


def _early_failure(message: str, daily_child: bool, started: float) -> int:
    elapsed = time.monotonic() - started
    print(f"❌ 任务失败\n{message}")
    emit_daily_result("fail", [message], daily_child)
    content = "\n".join(
        [
            "📋 任务结果",
            "",
            "❌ 任务失败",
            message,
            "",
            f"⏱️ 耗时：{max(0, int(round(elapsed)))}秒",
        ]
    )
    notify_result = system_notify("B站每日任务 · 有失败", content, daily_child)
    print(f"通知：{notify_result}")
    print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
    return 1


# 程序入口

def main(
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[str], BilibiliClient] = BilibiliClient,
) -> int:
    configure_utf8_output()
    started = time.monotonic()
    env = os.environ if environ is None else environ
    daily_child = str(env.get("DAILY_ALL_CHILD", "") or "").strip() == "1"
    print(f"==== B站每日任务 | {beijing_now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    raw = str(env.get("BILIBILI_COOKIE", "") or "")
    if skip_inactive_service(raw, str(env.get("DAILY_ALL_DISABLED", "") or ""), daily_child):
        return 0
    try:
        settings = load_settings(env)
    except ValueError as exc:
        return _early_failure(str(exc), daily_child, started)

    client = client_factory(settings.cookie)
    try:
        user = get_verified_user(client)

        reward_before = get_daily_reward(client)
        precheck_failed = reward_before is None
        logged_exp_before = get_today_exp_total(client)

        needs_video = reward_before is None or not (
            reward_completed(reward_before, "watch")
            and reward_completed(reward_before, "share")
        )
        popular = get_popular_videos(client, count=10) if needs_video else []
        video = popular[0] if popular else None

        outcomes = [
            ("漫画签到", run_manga_sign(client)),
            ("观看视频", run_watch(client, settings.csrf, video, reward_before)),
            ("分享任务", run_share(client, settings.csrf, video, reward_before)),
            ("投币任务", run_coin(client, settings, user, reward_before)),
        ]
        if settings.silver2coin:
            outcomes.append(("瓜子兑换", run_silver2coin(client, settings)))

        changed_exp = any(
            outcome.state == "success"
            for label, outcome in outcomes
            if label in {"观看视频", "分享任务", "投币任务"}
        )
        logged_exp = wait_today_exp_refresh(
            client,
            should_wait=changed_exp,
        )
        try:
            user_after = get_verified_user(client)
        except ApiError:
            user_after = user
        same_run_delta = max(0, user_after.current_exp - user.current_exp)
        has_completed_exp_task = any(
            label in {"观看视频", "分享任务", "投币任务"}
            and outcome.state in {"success", "already"}
            for label, outcome in outcomes
        ) or reward_completed(reward_before, "login")
        today_exp = resolve_today_exp(
            logged_exp=logged_exp,
            current_before=user.current_exp,
            current_after=user_after.current_exp,
            has_completed_exp_task=has_completed_exp_task,
            logged_exp_before=logged_exp_before,
        )
        upgrade = calculate_upgrade(
            current_level=user_after.current_level,
            current_exp=user_after.current_exp,
            next_exp=user_after.next_exp,
            today_exp=today_exp,
        )
        live = get_live_status(client)

        status = result_status(outcomes)
        failed_count = sum(outcome.state == "failed" for _, outcome in outcomes)
        skipped_count = sum(outcome.state == "skipped" for _, outcome in outcomes)
        status_icon = {"success": "✅", "warn": "⚠️", "fail": "❌"}[status]

        print()
        print(f"{status_icon} 账号：{user.name}")
        print("登录：Cookie 有效")
        if precheck_failed:
            print("状态预检查：查询失败，已根据执行结果和复查状态判断")
        for label, outcome in outcomes:
            print(f"{label}：{outcome.message}")

        print()
        print("📊 数据统计")
        print(f"等级：LV{user_after.current_level}")
        print(f"今日经验：{today_exp if today_exp is not None else '暂未同步'}")
        print(f"当前经验：{user_after.current_exp}")
        print(f"下一等级：{upgrade.next_level}")
        print(f"升级还差：{upgrade.remaining_exp} 经验")
        print(f"升级还需：{upgrade.remaining_days}")
        print(f"硬币：{live.get('coin', user_after.coin_balance)}")

        if status == "fail":
            print(f"结果：真正失败 {failed_count} 项")
        elif status == "warn":
            print(f"结果：完成，按策略跳过 {skipped_count} 项")
        else:
            print("结果：全部成功或已完成")

        elapsed = time.monotonic() - started
        emit_daily_result(
            status,
            build_daily_details(
                outcomes,
                today_exp=today_exp,
                user_after=user_after,
                upgrade=upgrade,
            ),
            settings.daily_child,
        )
        notification = format_notification(
            user,
            outcomes,
            today_exp=today_exp,
            user_after=user_after,
            upgrade=upgrade,
            live=live,
            elapsed=elapsed,
        )
        suffix = {"success": "全部完成", "warn": "有提醒", "fail": "有失败"}[status]
        notify_result = system_notify(
            f"B站每日任务 · {suffix}",
            notification,
            settings.daily_child,
        )
        print(f"通知：{notify_result}")
        print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
        return 1 if status == "fail" else 0
    except ApiError as exc:
        return _early_failure(f"登录检查失败：{exc}", settings.daily_child, started)
    except Exception as exc:
        return _early_failure(
            f"脚本处理异常（{type(exc).__name__}）",
            settings.daily_child,
            started,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
