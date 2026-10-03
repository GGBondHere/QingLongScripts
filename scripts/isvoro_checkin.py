#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
名称：ISVORO 签到
用途：每日签到，查询连签、奖励、积分余额与累计积分；刷新并保存 Cookie。

青龙配置：
- 执行：task QingLongScripts/isvoro_checkin.py now。
- 独立任务：启用、单次类型；日常由 daily_all.py 执行。
- 依赖：requests。
- 通知：在青龙面板配置，合集只发送汇总通知。

环境变量：
- ISVORO_COOKIE：包含 pmt_access、pmt_refresh、pmt_csrf 的完整 Cookie，单账号。
- CLIENT_ID、CLIENT_SECRET：青龙 OpenAPI 应用凭证，应用需授予环境变量权限。

凭证获取：
1. 登录 https://isvoro.com/checkin，F12 → 网络，刷新页面并选择 /api/v1/auth/me 请求。
2. 复制请求头 Cookie 的完整值，填入 ISVORO_COOKIE。

填写示例：
- pmt_refresh=实际值; pmt_access=实际值; pmt_csrf=实际值

使用说明：
- 未配置回写凭证时，新 Cookie 不会保存到面板。
- 停用标识：isvoro；缺凭证或已停用时跳过。详见 docs/isvoro.md。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# 运行配置

def skip_inactive_service(raw: str, disabled_value: str, daily_child: bool) -> bool:
    """未配置凭证或已停用时跳过。"""
    disabled = {item.strip().casefold() for item in re.split(r"[,;|&\n]+", disabled_value)}
    if raw.strip() and not disabled.intersection({"isvoro", "isvoro_checkin"}):
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
    os.getenv("ISVORO_COOKIE", ""), os.getenv("DAILY_ALL_DISABLED", ""),
    os.getenv("DAILY_ALL_CHILD", "").strip() == "1",
):
    raise SystemExit(0)


import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def configure_utf8_output() -> None:
    """确保 Windows 本地终端可输出中文和状态符号。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, TypeError, ValueError):
                pass


# 配置与数据模型

BASE = "https://isvoro.com/api/v1"
QL_OPEN_BASE = "http://127.0.0.1:5700/open"
COOKIE_ENV_NAME = "ISVORO_COOKIE"
DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="
TIMEOUT = 20
LOCAL_REQUEST_TIMEOUT = 10
GET_RETRY_COUNT = 2


# 环境变量与凭证解析

@dataclass
class CookieState:
    original_raw: str
    values: dict[str, str]
    order: list[str]

    @classmethod
    def from_header(cls, raw: str) -> "CookieState":
        original = (raw or "").strip()

        values: dict[str, str] = {}
        order: list[str] = []
        for part in original.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if not name:
                continue
            if name not in values:
                order.append(name)
            values[name] = value.strip()
        return cls(original, values, order)

    def get(self, name: str) -> str:
        return self.values.get(name, "")

    def as_header(self) -> str:
        return "; ".join(
            f"{name}={self.values[name]}"
            for name in self.order
            if name in self.values
        )

    def apply_response(self, response: Any) -> set[str]:
        changed: set[str] = set()
        for cookie in getattr(response, "cookies", ()):
            name = str(getattr(cookie, "name", "") or "").strip()
            if not name:
                continue
            value = str(getattr(cookie, "value", "") or "")
            if name not in self.values:
                self.order.append(name)
            if self.values.get(name) != value:
                changed.add(name)
            self.values[name] = value
        return changed


@dataclass
class WritebackResult:
    enabled: bool
    attempted: bool
    ok: bool
    message: str


def _safe_json(response):
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def create_http_session():
    """创建只会重试查询请求的会话，写请求不会自动重放。"""
    session = requests.Session()
    retry = Retry(
        total=GET_RETRY_COUNT,
        connect=GET_RETRY_COUNT,
        read=GET_RETRY_COUNT,
        status=GET_RETRY_COUNT,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _ql_response_ok(response, data):
    status_code = int(getattr(response, "status_code", 0) or 0)
    api_code = data.get("code")
    return (
        200 <= status_code < 300
        and (api_code is None or str(api_code) in {"0", "200"})
    )


def _ql_message(data, fallback):
    message = data.get("message") if isinstance(data, dict) else None
    return str(message or fallback)


class QinglongOpenAPI:
    """保存青龙中的 ISVORO_COOKIE。"""

    def __init__(self, client_id, client_secret, session=None):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.session = session or requests.Session()
        if session is None and hasattr(self.session, "trust_env"):
            self.session.trust_env = False

    def _get_token(self):
        try:
            response = self.session.get(
                f"{QL_OPEN_BASE}/auth/token",
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=LOCAL_REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return False, f"青龙认证请求异常（{type(error).__name__}）"

        token = (
            (data.get("data") or {}).get("token")
            if isinstance(data.get("data"), dict)
            else ""
        )
        if not _ql_response_ok(response, data) or not token:
            return False, f"青龙认证失败：{_ql_message(data, '未返回访问令牌')}"
        return True, str(token)

    @staticmethod
    def _extract_env_records(data):
        records = data.get("data") if isinstance(data, dict) else None
        if isinstance(records, dict):
            for key in ("data", "items", "rows", "list"):
                if isinstance(records.get(key), list):
                    records = records[key]
                    break
        if not isinstance(records, list):
            return []
        return [item for item in records if isinstance(item, dict)]

    def update_environment(self, original_value, new_value):
        token_ok, token_or_error = self._get_token()
        if not token_ok:
            return WritebackResult(True, True, False, token_or_error)

        headers = {
            "Authorization": f"Bearer {token_or_error}",
            "Content-Type": "application/json",
        }
        try:
            response = self.session.get(
                f"{QL_OPEN_BASE}/envs",
                params={"searchValue": COOKIE_ENV_NAME},
                headers=headers,
                timeout=LOCAL_REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return WritebackResult(
                True,
                True,
                False,
                f"查询青龙环境变量请求异常（{type(error).__name__}）",
            )

        if not _ql_response_ok(response, data):
            return WritebackResult(
                True,
                True,
                False,
                "查询青龙环境变量失败："
                + _ql_message(data, "接口返回异常"),
            )

        matches = [
            item
            for item in self._extract_env_records(data)
            if item.get("name") == COOKIE_ENV_NAME
            and item.get("value") == original_value
        ]
        if len(matches) != 1:
            return WritebackResult(
                True,
                True,
                False,
                "未能唯一确定 ISVORO_COOKIE，已拒绝回写",
            )

        target = matches[0]
        payload = {
            "name": COOKIE_ENV_NAME,
            "value": new_value,
        }
        if "id" in target:
            payload["id"] = target["id"]
        elif "_id" in target:
            payload["_id"] = target["_id"]
        else:
            return WritebackResult(
                True,
                True,
                False,
                "目标环境变量缺少 ID，已拒绝回写",
            )
        for key in ("remarks", "labels"):
            if key in target and target[key] is not None:
                payload[key] = target[key]

        try:
            response = self.session.put(
                f"{QL_OPEN_BASE}/envs",
                json=payload,
                headers=headers,
                timeout=LOCAL_REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return WritebackResult(
                True,
                True,
                False,
                f"ISVORO_COOKIE 回写请求异常（{type(error).__name__}）",
            )

        if not _ql_response_ok(response, data):
            return WritebackResult(
                True,
                True,
                False,
                "ISVORO_COOKIE 回写失败："
                + _ql_message(data, "接口返回异常"),
            )
        return WritebackResult(
            True,
            True,
            True,
            "ISVORO_COOKIE 已自动回写",
        )


def write_back_cookie(
    original_value,
    new_value,
    client_id="",
    client_secret="",
    session=None,
):
    """刷新被业务请求验证后，按原值精确回写青龙环境变量。"""
    clean_id = (client_id or "").strip()
    clean_secret = (client_secret or "").strip()

    if original_value == new_value:
        return WritebackResult(
            bool(clean_id or clean_secret),
            False,
            True,
            "ISVORO_COOKIE 无变化",
        )
    if not clean_id and not clean_secret:
        return WritebackResult(
            False,
            False,
            False,
            "登录态已刷新，但未配置 CLIENT_ID/CLIENT_SECRET",
        )
    if not clean_id or not clean_secret:
        return WritebackResult(
            True,
            False,
            False,
            "CLIENT_ID 与 CLIENT_SECRET 必须同时配置",
        )

    api = QinglongOpenAPI(clean_id, clean_secret, session=session)
    return api.update_environment(original_value, new_value)


class IsvoroClient:
    SAFE_RETRY_METHODS = {"GET", "HEAD"}
    WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(
        self,
        cookie_header,
        session=None,
        writeback_func=None,
        client_id="",
        client_secret="",
    ):
        self.cookies = CookieState.from_header(cookie_header)
        self.session = session or create_http_session()
        self.writeback_func = writeback_func
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.refresh_attempted = False
        self.refreshed = False
        self.writeback_result = None

        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Origin": "https://isvoro.com",
            "Referer": "https://isvoro.com/checkin",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Cache-Control": "no-cache",
        })

    def _request_once(self, method, path, allow_empty_json=False):
        method = method.upper()
        headers = {"Cookie": self.cookies.as_header()}

        if method in self.WRITE_METHODS:
            csrf = self.cookies.get("pmt_csrf")
            if not csrf:
                raise RuntimeError(
                    "缺少 pmt_csrf Cookie，为避免无效提交，本次没有发送请求"
                )
            headers["X-CSRF-Token"] = csrf

        response = self.session.request(
            method,
            BASE + path,
            headers=headers,
            timeout=TIMEOUT,
        )

        try:
            body = response.json()
        except Exception:
            if allow_empty_json and not (getattr(response, "text", "") or "").strip():
                body = {}
            else:
                raise RuntimeError(
                    f"{method} {path} 返回非 JSON："
                    f"HTTP {response.status_code}"
                )

        if not isinstance(body, dict):
            raise RuntimeError(f"{method} {path} 返回格式异常")
        return response, body

    def _refresh_once(self):
        if self.refresh_attempted:
            raise RuntimeError("本进程已经尝试刷新登录凭证，不再重复刷新")
        self.refresh_attempted = True

        if not self.cookies.get("pmt_refresh"):
            raise RuntimeError("缺少 pmt_refresh Cookie，无法自动刷新登录凭证")
        if not self.cookies.get("pmt_csrf"):
            raise RuntimeError("缺少 pmt_csrf Cookie，无法自动刷新登录凭证")

        response, body = self._request_once(
            "POST",
            "/auth/refresh",
            allow_empty_json=True,
        )

        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"登录凭证刷新失败：HTTP {response.status_code}，"
                f"message={body.get('message')}"
            )
        if body.get("code") not in (0, None):
            raise RuntimeError(
                f"登录凭证刷新失败：code={body.get('code')}，"
                f"message={body.get('message')}"
            )

        candidate = CookieState(
            original_raw=self.cookies.original_raw,
            values=dict(self.cookies.values),
            order=list(self.cookies.order),
        )
        changed = candidate.apply_response(response)
        if "pmt_access" not in changed or not candidate.get("pmt_access"):
            raise RuntimeError("登录凭证刷新响应未下发新的 pmt_access")

        self.cookies = candidate
        self.refreshed = True

    def _persist_verified_cookie(self):
        if self.writeback_result is not None:
            return self.writeback_result

        if self.writeback_func is None:
            result = WritebackResult(
                enabled=False,
                attempted=False,
                ok=False,
                message="登录态已刷新，但没有可用的青龙回写函数",
            )
        else:
            try:
                result = self.writeback_func(
                    self.cookies.original_raw,
                    self.cookies.as_header(),
                    self.client_id,
                    self.client_secret,
                )
                if not isinstance(result, WritebackResult):
                    result = WritebackResult(
                        enabled=True,
                        attempted=True,
                        ok=False,
                        message="登录态已刷新，但青龙回写结果格式异常",
                    )
            except Exception as error:
                result = WritebackResult(
                    enabled=True,
                    attempted=True,
                    ok=False,
                    message=(
                        "登录态已刷新，但青龙回写异常"
                        f"（{type(error).__name__}）"
                    ),
                )

        self.writeback_result = result
        return result

    def call(self, method, path):
        method = method.upper()
        response, body = self._request_once(method, path)

        if response.status_code == 401 and method in self.SAFE_RETRY_METHODS:
            self._refresh_once()
            response, body = self._request_once(method, path)
            if 200 <= response.status_code < 300:
                self._persist_verified_cookie()

        return response.status_code, body


@dataclass
class CheckinResult:
    status: str
    sign_status: str
    streak: Any = "?"
    earned_now: Any = "?"
    balance: Any = "?"
    total_earned: Any = "?"
    credential: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""


# 网络请求与业务逻辑
def get_data(body, name):
    if not isinstance(body, dict):
        raise RuntimeError(f"{name}返回格式异常")
    code = body.get("code")
    if code not in (0, None):
        raise RuntimeError(f"{name}失败：{body.get('message') or f'code={code}'}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"{name}缺少 data")
    return data


def get_status(client):
    http, body = client.call("GET", "/points/checkin/status")
    if http in (401, 403):
        raise RuntimeError(f"登录态无效（HTTP {http}）")
    if not 200 <= http < 300:
        raise RuntimeError(f"查询签到状态失败（HTTP {http}）")
    return get_data(body, "查询签到状态")


def get_points(client):
    http, body = client.call("GET", "/points")
    if not 200 <= http < 300:
        raise RuntimeError(f"查询积分失败（HTTP {http}）")
    return get_data(body, "查询积分")


def safe_get_points(client):
    try:
        return get_points(client)
    except Exception:
        return None


def friendly_error(error):
    text = str(error)
    lowered = text.lower()
    if "登录态无效" in text or "401" in lowered:
        return "登录状态已过期，请重新登录网站并更新 ISVORO_COOKIE"
    if "csrf" in lowered or "pmt_csrf" in lowered:
        return "登录校验信息不完整，请重新抓取完整 ISVORO_COOKIE"
    if "can_checkin" in lowered:
        return "服务端当前不允许签到，请稍后再运行"
    if any(word in lowered for word in ("timeout", "timed out", "connection", "network")):
        return "网络请求异常，请稍后再运行"
    return text or "签到没有确认成功"


def verify_checkin(client, points_before=None):
    time.sleep(1)
    status = get_status(client)
    if status.get("checked_in") is not True:
        raise RuntimeError("复查仍未签到；为避免重复提交，不进行第二次 POST")

    points_after = safe_get_points(client)
    result = CheckinResult(status="success", sign_status="签到成功", streak=status.get("streak", "?"))
    if points_after:
        result.balance = points_after.get("balance", "?")
        result.total_earned = points_after.get("total_earned", "?")
        if points_before:
            before_balance = points_before.get("balance")
            after_balance = points_after.get("balance")
            if isinstance(before_balance, (int, float)) and isinstance(after_balance, (int, float)):
                result.earned_now = after_balance - before_balance
    else:
        result.warnings.append("签到已确认，但积分信息查询失败")
    return result


# 日志、通知与合集结果

def _value(value: Any) -> str:
    return "未返回" if value in (None, "?") else str(value)


def _has_earned_value(result: CheckinResult) -> bool:
    return result.earned_now not in (None, "?")


def _apply_credential_result(result: CheckinResult, client: IsvoroClient) -> None:
    if not client.refreshed:
        return
    writeback = client.writeback_result
    if writeback is None:
        result.credential = "登录凭证已刷新，但未获得回写结果"
        result.warnings.append(result.credential)
    elif writeback.ok:
        result.credential = f"登录凭证刷新成功；{writeback.message}"
    else:
        result.credential = writeback.message
        result.warnings.append(writeback.message)


def _notification_warnings(result: CheckinResult) -> list[str]:
    """筛选需用户处理的提醒。"""
    return list(
        dict.fromkeys(
            warning
            for warning in result.warnings
            if warning and warning != result.credential
        )
    )


def _normalized_status(result: CheckinResult) -> str:
    if result.status == "fail":
        return "fail"
    return "warn" if _notification_warnings(result) else "success"


def _log_status(result: CheckinResult) -> str:
    if result.status == "fail":
        return "fail"
    return "warn" if result.warnings else "success"


def format_result_log(result: CheckinResult) -> str:
    status = _log_status(result)
    icon = {"success": "✅", "warn": "⚠️", "fail": "❌"}[status]
    lines = [f"{icon} 账号：ISVORO", f"签到：{result.sign_status}"]
    if result.status != "fail":
        lines.append(f"连续：{_value(result.streak)}天")
        if _has_earned_value(result):
            lines.append(f"本次：+{_value(result.earned_now)}积分")
        lines.extend(
            [
                f"余额：{_value(result.balance)}",
                f"累计：{_value(result.total_earned)}",
            ]
        )
    else:
        lines.append(f"原因：{result.error}")
    if result.credential:
        lines.append(f"凭证：{result.credential}")
    if result.warnings:
        lines.append("提醒：" + "；".join(dict.fromkeys(result.warnings)))
    return "\n".join(lines)


def format_notification(result: CheckinResult, elapsed: float) -> str:
    status = _normalized_status(result)
    warnings = _notification_warnings(result)
    icon = {"success": "✅", "warn": "⚠️", "fail": "❌"}[status]
    lines = ["📋 任务结果", "", f"{icon} ISVORO", f"签到：{result.sign_status}"]
    if result.status != "fail":
        lines.append(f"连续：{_value(result.streak)}天")
        if _has_earned_value(result):
            lines.append(f"本次：+{_value(result.earned_now)}积分")
        lines.append(
            f"余额：{_value(result.balance)}｜累计：{_value(result.total_earned)}"
        )
    else:
        lines.append(f"原因：{result.error}")
    if warnings:
        lines.extend(["", "⚠️ 提醒", *warnings])
    lines.extend(
        [
            "",
            f"⏱️ 耗时：{max(0, int(round(elapsed)))}秒",
            f"🕒 完成：{time.strftime('%m-%d %H:%M')}",
        ]
    )
    return "\n".join(lines)


def build_daily_details(result: CheckinResult) -> list[str]:
    if result.status == "fail":
        return [result.error or "签到失败"]
    details = [f"{result.sign_status}｜连续{_value(result.streak)}天"]
    points = []
    if _has_earned_value(result):
        points.append(f"本次 +{_value(result.earned_now)}")
    points.extend(
        [
            f"余额 {_value(result.balance)}",
            f"累计 {_value(result.total_earned)}",
        ]
    )
    details.append("｜".join(points))
    warnings = _notification_warnings(result)
    if warnings:
        details.append("提醒：" + "；".join(warnings))
    return details


def emit_daily_result(result: CheckinResult, daily_child: bool) -> None:
    if not daily_child:
        return
    payload = {
        "version": 1,
        "status": _normalized_status(result),
        "details": build_daily_details(result),
    }
    print(DAILY_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def system_notify(title: str, content: str, daily_child: bool | None = None) -> str:
    """使用青龙系统通知；合集子进程只返回结果，不重复推送。"""
    child = (
        os.environ.get("DAILY_ALL_CHILD") == "1"
        if daily_child is None
        else daily_child
    )
    if child:
        return "由合集统一发送"
    try:
        QLAPI.systemNotify(  # type: ignore[name-defined]
            {"title": title, "content": content}
        )
        return "已发送"
    except NameError:
        return "当前环境没有 QLAPI，已跳过"
    except Exception as error:
        return f"发送失败（{type(error).__name__}）"


def _finish(result: CheckinResult, started_at: float, daily_child: bool) -> int:
    elapsed = time.monotonic() - started_at
    print(format_result_log(result))
    emit_daily_result(result, daily_child)
    status = _normalized_status(result)
    suffix = {"success": "全部完成", "warn": "有提醒", "fail": "有失败"}[status]
    notify_result = system_notify(
        f"ISVORO · {suffix}",
        format_notification(result, elapsed),
        daily_child,
    )
    print(f"通知：{notify_result}")
    print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
    return 1 if status == "fail" else 0


# 程序入口

def main(
    raw_cookie: str | None = None,
    session: Any = None,
    writeback_func: Any = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> int:
    configure_utf8_output()
    started_at = time.monotonic()
    daily_child = os.environ.get("DAILY_ALL_CHILD") == "1"
    print(f"==== ISVORO 签到 | {time.strftime('%Y-%m-%d %H:%M:%S')} ====")

    raw = os.getenv(COOKIE_ENV_NAME, "").strip() if raw_cookie is None else str(raw_cookie).strip()
    if skip_inactive_service(raw, os.getenv("DAILY_ALL_DISABLED", ""), daily_child):
        return 0

    initial = CookieState.from_header(raw)
    missing = [
        name
        for name in ("pmt_access", "pmt_refresh", "pmt_csrf")
        if not initial.get(name)
    ]
    if missing:
        return _finish(
            CheckinResult(
                "fail",
                "未完成",
                error="ISVORO_COOKIE 缺少必要字段：" + "、".join(missing),
            ),
            started_at,
            daily_child,
        )

    resolved_client_id = (
        os.getenv("CLIENT_ID", "").strip()
        if client_id is None
        else str(client_id).strip()
    )
    resolved_client_secret = (
        os.getenv("CLIENT_SECRET", "").strip()
        if client_secret is None
        else str(client_secret).strip()
    )
    client = IsvoroClient(
        raw,
        session=session,
        writeback_func=writeback_func or write_back_cookie,
        client_id=resolved_client_id,
        client_secret=resolved_client_secret,
    )

    try:
        status_data = get_status(client)
        checked = status_data.get("checked_in")
        can_checkin = status_data.get("can_checkin")
        streak = status_data.get("streak", 0)

        if checked is True:
            points = safe_get_points(client)
            result = CheckinResult(
                "success",
                "今日已签到",
                streak=streak,
                balance=points.get("balance", "?") if points else "?",
                total_earned=points.get("total_earned", "?") if points else "?",
            )
            if points is None:
                result.warnings.append("签到已确认，但积分信息查询失败")
        else:
            if can_checkin is not True:
                raise RuntimeError("服务端当前 can_checkin != true，未发送签到请求")
            points_before = safe_get_points(client)
            post_error = None
            try:
                http, body = client.call("POST", "/points/checkin")
                if not 200 <= http < 300:
                    raise RuntimeError(f"签到接口异常（HTTP {http}）")
                if body.get("code") not in (0, None):
                    raise RuntimeError(f"签到接口失败：{body.get('message') or body.get('code')}")
            except Exception as error:
                post_error = error

            try:
                result = verify_checkin(client, points_before)
            except Exception:
                if post_error:
                    raise RuntimeError(f"{post_error}；复查未确认签到成功")
                raise
        _apply_credential_result(result, client)
    except Exception as error:
        result = CheckinResult("fail", "未完成", error=friendly_error(error))
        _apply_credential_result(result, client)

    return _finish(result, started_at, daily_child)


if __name__ == "__main__":
    raise SystemExit(main())
