#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
名称：NodeSeek 签到
用途：多账号签到，展示鸡腿收益。

青龙配置：
- 执行：task QingLongScripts/nodeseek.py now。
- 独立任务：启用、单次类型；日常由 daily_all.py 执行。
- 依赖：curl_cffi。
- 通知：在青龙面板配置，合集只发送汇总通知。

环境变量：
- NS_COOKIE：完整 Cookie，多账号每行一个，可附 #备注。
- NS_RANDOM（可选）：随机奖励策略，true/false，默认 true。
- NS_IMPERSONATE（可选）：浏览器指纹，默认 chrome110。

凭证获取：
1. 登录 https://www.nodeseek.com，F12 → 网络，刷新页面并选择本站请求。
2. 复制请求头 Cookie 的完整值，填入 NS_COOKIE。

填写示例：
- 完整Cookie1#大号
- 完整Cookie2#小号

使用说明：
- NS_RANDOM 控制奖励策略，不是执行延迟。
- 停用标识：nodeseek；缺凭证或已停用时跳过。详见 docs/nodeseek.md。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping


# 运行配置

def skip_inactive_service(raw: str, disabled_value: str, daily_child: bool) -> bool:
    """未配置凭证或已停用时跳过。"""
    disabled = {item.strip().casefold() for item in re.split(r"[,;|&\n]+", disabled_value)}
    if raw.strip() and not disabled.intersection({"nodeseek"}):
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
    os.getenv("NS_COOKIE", ""), os.getenv("DAILY_ALL_DISABLED", ""),
    os.getenv("DAILY_ALL_CHILD", "").strip() == "1",
):
    raise SystemExit(0)


# 配置与数据模型

ATTENDANCE_URL = "https://www.nodeseek.com/api/attendance"
REQUEST_TIMEOUT_SECONDS = 25
DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="


def configure_utf8_output() -> None:
    """确保 Windows 本地终端可输出中文和状态符号。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, TypeError, ValueError):
            pass


@dataclass(frozen=True)
class AccountConfig:
    cookie: str
    display_name: str


@dataclass(frozen=True)
class AccountResult:
    display_name: str
    ok: bool
    status: str
    message: str


# 环境变量与凭证解析

def parse_accounts(raw: str) -> list[AccountConfig]:
    """按换行拆分账号，Cookie 内的 ``&`` 等字符保持原样。"""
    accounts: list[AccountConfig] = []
    for raw_line in str(raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        cookie, separator, remark = line.partition("#")
        cookie = cookie.strip()
        if not cookie:
            continue

        display_name = remark.strip() if separator and remark.strip() else ""
        accounts.append(
            AccountConfig(
                cookie=cookie,
                display_name=display_name or f"账号{len(accounts) + 1}",
            )
        )
    return accounts


def normalize_random_mode(value: str) -> str:
    """规范接口参数；它不控制脚本等待时间。"""
    normalized = str(value or "").strip().lower() or "true"
    if normalized not in {"true", "false"}:
        raise ValueError("NS_RANDOM 只能填写 true 或 false")
    return normalized


def build_headers(cookie: str) -> dict[str, str]:
    """构造 NodeSeek 签到请求头。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
        ),
        "origin": "https://www.nodeseek.com",
        "referer": "https://www.nodeseek.com/board",
        "Content-Type": "application/json",
        "Cookie": cookie,
    }


def _safe_message(value: Any, cookie: str) -> str:
    """只保留结构化 message，并清除可能误带的凭证。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if cookie:
        text = text.replace(cookie, "[已隐藏]")
    text = re.sub(
        r"(?i)\b(cookie|set-cookie|authorization)\s*[:=]\s*[^\s,]+",
        r"\1=[已隐藏]",
        text,
    )
    return text[:200]


# 网络请求与业务逻辑

def default_post(url: str, **kwargs: Any):
    """按需加载 curl_cffi 请求库。"""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise RuntimeError("缺少 Python 依赖 curl_cffi") from exc
    return curl_requests.post(url, **kwargs)


def checkin_account(
    account: AccountConfig,
    *,
    random_mode: str,
    impersonate: str,
    post_func: Callable[..., Any],
) -> AccountResult:
    """为一个账号执行一次签到；此函数不会重试。"""
    try:
        url = f"{ATTENDANCE_URL}?random={random_mode}"
        response = post_func(
            url,
            headers=build_headers(account.cookie),
            json={},
            timeout=REQUEST_TIMEOUT_SECONDS,
            impersonate=impersonate,
        )
    except Exception as exc:  # 请求库可能抛出多种传输异常
        if isinstance(exc, RuntimeError) and str(exc) == "缺少 Python 依赖 curl_cffi":
            message = str(exc)
        else:
            message = f"网络请求异常（{type(exc).__name__}）"
        return AccountResult(account.display_name, False, "error", message)

    try:
        http_status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        http_status = 0

    json_error = False
    try:
        payload = response.json()
    except Exception:
        payload = None
        json_error = True

    if isinstance(payload, Mapping):
        message = _safe_message(payload.get("message"), account.cookie)
        normalized_status = str(payload.get("status", "")).strip()

        # NodeSeek 对“今日已签到”等业务结果可能返回 HTTP 500；必须先按
        # JSON 业务字段分类，再处理通用 HTTP 错误。
        if "已完成签到" in message or "已经签到" in message or "已签到" in message:
            return AccountResult(account.display_name, True, "already", "今日已完成签到")
        if payload.get("success") is True or "鸡腿" in message:
            return AccountResult(
                account.display_name,
                True,
                "success",
                message or "签到成功",
            )
        if normalized_status == "404":
            return AccountResult(
                account.display_name,
                False,
                "invalid",
                "Cookie 已失效或登录状态无效",
            )
    else:
        message = ""

    if http_status in {401, 403}:
        return AccountResult(
            account.display_name,
            False,
            "invalid",
            "Cookie 已失效或登录状态无效",
        )
    if http_status >= 400:
        return AccountResult(
            account.display_name,
            False,
            "error",
            f"接口请求失败（HTTP {http_status}）",
        )
    if json_error:
        return AccountResult(
            account.display_name,
            False,
            "error",
            f"接口未返回有效 JSON（HTTP {http_status}）",
        )
    if not isinstance(payload, Mapping):
        return AccountResult(
            account.display_name,
            False,
            "error",
            f"接口返回格式异常（HTTP {http_status}）",
        )

    return AccountResult(
        account.display_name,
        False,
        "failed",
        message or "签到失败，接口未返回原因",
    )


# 日志、通知与合集结果

def format_account_log(index: int, result: AccountResult) -> str:
    icon = "✅" if result.ok else "❌"
    return "\n".join([f"{icon} 账号{index}：{result.display_name}", f"签到：{result.message}"])


def format_notification(results: list[AccountResult], elapsed: float = 0) -> str:
    lines = ["📋 任务结果", ""]
    for item in results:
        lines.extend(
            [
                f"{'✅' if item.ok else '❌'} {item.display_name}",
                f"签到：{item.message}",
                "",
            ]
        )
    lines.extend(
        [
            f"⏱️ 耗时：{max(0, int(round(elapsed)))}秒",
            f"🕒 完成：{datetime.now().strftime('%m-%d %H:%M')}",
        ]
    )
    return "\n".join(lines).strip()


def build_daily_details(results: list[AccountResult]) -> list[str]:
    success_count = sum(item.ok for item in results)
    details = [f"{item.display_name}：{item.message}" for item in results]
    if len(results) > 1:
        details.insert(0, f"成功 {success_count}/{len(results)}")
    return details


def emit_daily_result(status: str, details: list[str], daily_child: bool) -> None:
    if not daily_child:
        return
    payload = {"version": 1, "status": status, "details": details}
    print(DAILY_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


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


def _print_early_failure(message: str, daily_child: bool, started_at: float) -> int:
    elapsed = time.monotonic() - started_at
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
    notify_result = system_notify("NodeSeek · 有失败", content, daily_child)
    print(f"通知：{notify_result}")
    print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
    return 1


# 程序入口

def main(
    environ: Mapping[str, str] | None = None,
    post_func: Callable[..., Any] | None = None,
) -> int:
    configure_utf8_output()
    started_at = time.monotonic()
    env = os.environ if environ is None else environ
    daily_child = str(env.get("DAILY_ALL_CHILD", "") or "").strip() == "1"

    print(
        "==== NodeSeek签到 | "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===="
    )

    raw_accounts = str(env.get("NS_COOKIE", "") or "")
    if skip_inactive_service(raw_accounts, str(env.get("DAILY_ALL_DISABLED", "") or ""), daily_child):
        return 0

    accounts = parse_accounts(raw_accounts)
    if not accounts:
        return _print_early_failure(
            "环境变量 NS_COOKIE 中没有有效账号", daily_child, started_at
        )

    try:
        random_mode = normalize_random_mode(str(env.get("NS_RANDOM", "") or ""))
    except ValueError as exc:
        return _print_early_failure(str(exc), daily_child, started_at)

    impersonate = str(env.get("NS_IMPERSONATE", "") or "").strip() or "chrome110"
    request_post = default_post if post_func is None else post_func

    print(f"发现 {len(accounts)} 个账号")
    results: list[AccountResult] = []
    for index, account in enumerate(accounts, start=1):
        try:
            result = checkin_account(
                account,
                random_mode=random_mode,
                impersonate=impersonate,
                post_func=request_post,
            )
        except Exception as exc:
            result = AccountResult(
                account.display_name,
                False,
                "error",
                f"脚本处理异常（{type(exc).__name__}）",
            )
        results.append(result)
        print()
        print(format_account_log(index, result))

    success_count = sum(item.ok for item in results)
    all_ok = success_count == len(results)
    status = "success" if all_ok else "fail"
    elapsed = time.monotonic() - started_at
    print()
    print(f"结果：成功 {success_count}/{len(results)}")
    emit_daily_result(status, build_daily_details(results), daily_child)

    title = "NodeSeek · 全部完成" if all_ok else "NodeSeek · 有失败"
    notify_result = system_notify(
        title,
        format_notification(results, elapsed),
        daily_child,
    )
    print(f"通知：{notify_result}")
    print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
