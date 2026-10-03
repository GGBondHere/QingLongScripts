#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
名称：夸克网盘签到
用途：移动端签到，查询空间奖励、连签进度和总空间。

青龙配置：
- 执行：task QingLongScripts/quark.py now。
- 独立任务：启用、单次类型；日常由 daily_all.py 执行。
- 依赖：requests。
- 通知：在青龙面板配置，合集只发送汇总通知。

环境变量：
- QUARK_COOKIE：移动端 kps、sign、vcode 三参数，或包含它们的完整请求 URL。

凭证获取：
1. 用 Reqable 等工具抓取夸克 APP 网盘签到页。
2. 筛选 drive-m.quark.cn，选择包含 kps、sign、vcode 的请求，复制完整 URL 或三个参数。

填写示例：
- kps=实际值; sign=实际值; vcode=实际值#备注
- https://drive-m.quark.cn/实际路径?kps=实际值&sign=实际值&vcode=实际值#备注

使用说明：
- 多账号每行一个，#备注可选；保留参数原始编码。
- 停用标识：quark；缺凭证或已停用时跳过。详见 docs/quark.md。
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
from urllib.parse import parse_qsl, unquote, urlsplit

# 运行配置

def skip_inactive_service(raw: str, disabled_value: str, daily_child: bool) -> bool:
    """未配置凭证或已停用时跳过。"""
    disabled = {item.strip().casefold() for item in re.split(r"[,;|&\n]+", disabled_value)}
    if raw.strip() and not disabled.intersection({"quark", "夸克网盘"}):
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
    os.getenv("QUARK_COOKIE", ""), os.getenv("DAILY_ALL_DISABLED", ""),
    os.getenv("DAILY_ALL_CHILD", "").strip() == "1",
):
    raise SystemExit(0)


import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# 配置与数据模型

ENV_NAME = "QUARK_COOKIE"
DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="
API_ORIGIN = "https://drive-m.quark.cn"
GROWTH_INFO_URL = f"{API_ORIGIN}/1/clouddrive/capacity/growth/info"
GROWTH_SIGN_URL = f"{API_ORIGIN}/1/clouddrive/capacity/growth/sign"
REQUEST_TIMEOUT = (5, 15)
GET_RETRY_COUNT = 3


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
    display_name: str
    kps: str = ""
    sign: str = ""
    vcode: str = ""
    cookie_header: str = ""
    validation_error: str = ""

    @property
    def secret_values(self) -> tuple[str, ...]:
        """返回本账号可能出现在异常文本中的敏感值。"""
        values = [self.cookie_header, self.kps, self.sign, self.vcode]
        unique = {value for value in values if value}
        return tuple(sorted(unique, key=len, reverse=True))


@dataclass(frozen=True)
class GrowthInfo:
    signed: bool
    daily_reward: int
    progress: int
    target: int
    total_capacity: int
    cumulative_reward: int
    is_88vip: bool = False


@dataclass(frozen=True)
class AccountResult:
    display_name: str
    ok: bool
    status: str
    login_status: str = "未验证"
    sign_status: str = "未完成"
    reward: str = ""
    progress: str = ""
    total_space: str = ""
    cumulative_reward: str = ""
    error_stage: str = ""
    error_message: str = ""


class QuarkError(RuntimeError):
    """带失败阶段的安全错误，不包含原始响应或请求凭证。"""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


# 环境变量与凭证解析

def _clean_field(value: Any) -> str:
    return str(value or "").strip()


def _query_values(url: str) -> dict[str, str]:
    try:
        query = urlsplit(url.strip()).query
    except ValueError:
        return {}
    return {key.lower(): value for key, value in parse_qsl(query, keep_blank_values=True)}


def parse_accounts(raw: str) -> list[AccountConfig]:
    """按真实换行拆分账号；不会把 URL 内的 & 当成账号分隔符。"""
    accounts: list[AccountConfig] = []
    for raw_line in str(raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 备注只取最后一个 # 后的文本，先剥离再解析 URL fragment。
        body, marker, remark = line.rpartition("#")
        if marker and remark.strip():
            source = body.strip()
            display_remark = remark.strip()
        else:
            source = line
            display_remark = ""

        values: dict[str, str] = {}

        if source.lower().startswith(("http://", "https://")):
            values.update(_query_values(source))
        else:
            for raw_part in source.split(";"):
                part = raw_part.strip()
                if not part or "=" not in part:
                    continue
                key, value = part.split("=", 1)
                clean_key = key.strip()
                clean_value = value.strip()
                lowered = clean_key.lower()
                if lowered in {"kps", "sign", "vcode"}:
                    values[lowered] = unquote(clean_value)

        kps = _clean_field(values.get("kps"))
        sign = _clean_field(values.get("sign"))
        vcode = _clean_field(values.get("vcode"))
        missing = [
            name
            for name, value in (("kps", kps), ("sign", sign), ("vcode", vcode))
            if not value
        ]
        validation_error = (
            "缺少移动端参数：" + "、".join(missing) if missing else ""
        )

        cookie_header = (
            f"kps={kps}; sign={sign}; vcode={vcode}"
            if not missing
            else ""
        )

        display_name = display_remark or f"账号{len(accounts) + 1}"
        accounts.append(
            AccountConfig(
                display_name=display_name,
                kps=kps,
                sign=sign,
                vcode=vcode,
                cookie_header=cookie_header,
                validation_error=validation_error,
            )
        )
    return accounts


def format_bytes(value: Any) -> str:
    """将接口字节数转换成紧凑的 B/KB/MB/GB/TB 文本。"""
    try:
        amount = max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        amount = 0.0

    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while amount >= 1024 and unit_index < len(units) - 1:
        amount /= 1024
        unit_index += 1
    if amount.is_integer():
        return f"{int(amount)} {units[unit_index]}"
    return f"{amount:.2f} {units[unit_index]}"


_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(kps|sign|vcode|cookie|set-cookie|authorization)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^,;\s]+)"
)


def redact_text(value: Any, exact_secrets: tuple[str, ...] = ()) -> str:
    """清理服务端或异常文本，避免凭证进入青龙日志和通知。"""
    text = str(value or "")
    for secret in sorted({item for item in exact_secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    text = _NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


# 网络请求与业务逻辑

def create_http_session() -> requests.Session:
    """创建只允许安全 GET 自动重试的 Session。"""
    retry = Retry(
        total=GET_RETRY_COUNT,
        connect=GET_RETRY_COUNT,
        read=GET_RETRY_COUNT,
        status=GET_RETRY_COUNT,
        other=0,
        redirect=0,
        allowed_methods=frozenset({"GET"}),
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=0.5,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount(f"{API_ORIGIN}/", adapter)
    # urllib3 对“尚未建立连接”的失败可能不受 allowed_methods 限制。
    # 给签到 URL 使用更长的独立前缀并彻底关闭重试，确保 POST 最多一次。
    session.mount(GROWTH_SIGN_URL, HTTPAdapter(max_retries=0))
    return session


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes"}


class QuarkClient:
    """单账号客户端；每个账号使用独立 Session。"""

    def __init__(self, account: AccountConfig, session: Any | None = None):
        self.account = account
        self.session = session if session is not None else create_http_session()

    def _params(self) -> dict[str, str]:
        return {
            "pr": "ucpro",
            "fr": "android",
            "uc_param_str": "",
            "kps": self.account.kps,
            "sign": self.account.sign,
            "vcode": self.account.vcode,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Mobile) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Mobile Safari/537.36"
            ),
            "Cookie": self.account.cookie_header,
        }

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        json_body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {
            "params": self._params(),
            "headers": self._headers(),
            "timeout": REQUEST_TIMEOUT,
        }
        if json_body is not None:
            kwargs["json"] = dict(json_body)

        try:
            response = self.session.request(method.upper(), url, **kwargs)
        except requests.RequestException as exc:
            message = redact_text(
                f"网络请求异常（{type(exc).__name__}）",
                self.account.secret_values,
            )
            raise QuarkError(stage, message) from exc
        except Exception as exc:
            message = redact_text(
                f"请求执行异常（{type(exc).__name__}）",
                self.account.secret_values,
            )
            raise QuarkError(stage, message) from exc

        try:
            http_status = int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError):
            http_status = 0

        if http_status in {401, 403}:
            raise QuarkError("登录", f"移动端凭证无效或已过期（HTTP {http_status}）")
        if http_status < 200 or http_status >= 300:
            raise QuarkError(stage, f"接口请求失败（HTTP {http_status or '未知'}）")

        try:
            payload = response.json()
        except Exception as exc:
            raise QuarkError(stage, f"接口未返回有效 JSON（HTTP {http_status}）") from exc
        if not isinstance(payload, Mapping):
            raise QuarkError(stage, "接口返回的 JSON 格式异常")
        return payload

    def get_growth_info(self) -> GrowthInfo:
        payload = self._request_json("GET", GROWTH_INFO_URL, stage="查询签到状态")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            message = redact_text(payload.get("message"), self.account.secret_values)
            suffix = f"：{message}" if message else ""
            raise QuarkError("查询签到状态", f"接口返回缺少 data{suffix}")

        cap_sign = data.get("cap_sign")
        if not isinstance(cap_sign, Mapping):
            raise QuarkError("查询签到状态", "接口返回缺少 cap_sign")
        composition = data.get("cap_composition")
        if not isinstance(composition, Mapping):
            composition = {}

        return GrowthInfo(
            signed=_as_bool(cap_sign.get("sign_daily")),
            daily_reward=_as_int(cap_sign.get("sign_daily_reward")),
            progress=_as_int(cap_sign.get("sign_progress")),
            target=_as_int(cap_sign.get("sign_target")),
            total_capacity=_as_int(data.get("total_capacity")),
            cumulative_reward=_as_int(composition.get("sign_reward")),
            is_88vip=_as_bool(data.get("88VIP")),
        )

    def sign_once(self) -> int:
        payload = self._request_json(
            "POST",
            GROWTH_SIGN_URL,
            stage="签到",
            json_body={"sign_cyclic": True},
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            message = redact_text(payload.get("message"), self.account.secret_values)
            raise QuarkError("签到", message or "签到接口返回缺少 data")
        return _as_int(data.get("sign_daily_reward"))

    @staticmethod
    def _success_result(
        account: AccountConfig,
        info: GrowthInfo,
        *,
        status: str,
        sign_status: str,
        fallback_reward: int = 0,
    ) -> AccountResult:
        reward = info.daily_reward or fallback_reward
        progress = (
            f"{info.progress}/{info.target}天"
            if info.target
            else f"{info.progress}天"
        )
        return AccountResult(
            display_name=account.display_name,
            ok=True,
            status=status,
            login_status="移动端凭证有效",
            sign_status=sign_status,
            reward=format_bytes(reward) if reward else "未返回奖励信息",
            progress=progress,
            total_space=format_bytes(info.total_capacity),
            cumulative_reward=format_bytes(info.cumulative_reward),
        )

    def run(self) -> AccountResult:
        """执行一次签到；POST 一旦尝试，无论结果如何都只做 GET 复查。"""
        if self.account.validation_error:
            return AccountResult(
                display_name=self.account.display_name,
                ok=False,
                status="invalid_config",
                error_stage="配置",
                error_message=self.account.validation_error,
            )

        try:
            before = self.get_growth_info()
        except QuarkError as exc:
            return AccountResult(
                display_name=self.account.display_name,
                ok=False,
                status="error",
                error_stage=exc.stage,
                error_message=redact_text(exc.message, self.account.secret_values),
            )

        if before.signed:
            return self._success_result(
                self.account,
                before,
                status="already",
                sign_status="今日已签到",
            )

        sign_reward = 0
        sign_error: QuarkError | None = None
        try:
            sign_reward = self.sign_once()
        except QuarkError as exc:
            sign_error = exc

        try:
            after = self.get_growth_info()
        except QuarkError as exc:
            if sign_error is not None:
                message = (
                    f"{sign_error.message}；签到结果复查失败（{exc.message}）"
                )
            else:
                message = f"签到结果复查失败（{exc.message}）"
            return AccountResult(
                display_name=self.account.display_name,
                ok=False,
                status="unconfirmed",
                login_status="移动端凭证有效",
                error_stage="签到结果确认",
                error_message=redact_text(message, self.account.secret_values),
            )

        if after.signed:
            if sign_error is not None:
                return self._success_result(
                    self.account,
                    after,
                    status="confirmed",
                    sign_status="今日已签到（复查确认）",
                    fallback_reward=sign_reward,
                )
            return self._success_result(
                self.account,
                after,
                status="success",
                sign_status="签到成功",
                fallback_reward=sign_reward,
            )

        if sign_error is not None:
            failure_message = sign_error.message
        else:
            failure_message = "签到接口返回成功，但复查仍未签到"
        return AccountResult(
            display_name=self.account.display_name,
            ok=False,
            status="failed",
            login_status="移动端凭证有效",
            error_stage="签到",
            error_message=redact_text(failure_message, self.account.secret_values),
        )


# 日志、通知与合集结果

def result_severity(result: AccountResult) -> str:
    """将账号结果归一为 success、warning 或 fail。"""
    if not result.ok:
        return "fail"
    if not result.reward or not result.progress or not result.total_space:
        return "warning"
    return "success"


def merge_status(results: list[AccountResult]) -> str:
    severities = {result_severity(item) for item in results}
    if "fail" in severities:
        return "fail"
    if "warning" in severities:
        return "warn"
    return "success"


def format_duration(seconds: float) -> str:
    return f"{max(0, int(round(seconds)))}秒"


def format_account_log(index: int, result: AccountResult) -> str:
    severity = result_severity(result)
    icon = {"success": "✅", "warning": "⚠️", "fail": "❌"}[severity]
    lines = [f"{icon} 账号{index}：{result.display_name}"]
    if result.ok:
        lines.extend(
            [
                f"登录：{result.login_status}",
                f"签到：{result.sign_status}",
                f"奖励：{result.reward or '未返回奖励信息'}",
                f"连签：{result.progress or '未返回'}",
                (
                    f"空间：{result.total_space or '未返回'}"
                    f"（签到累计 {result.cumulative_reward or '未返回'}）"
                ),
            ]
        )
    else:
        lines.extend(
            [
                f"登录：{result.login_status}",
                "签到：未完成",
                f"{result.error_stage or '执行'}：{result.error_message}",
            ]
        )
    return "\n".join(lines)


def format_notification(results: list[AccountResult], elapsed: float = 0) -> str:
    lines = ["📋 任务结果", ""]
    for result in results:
        severity = result_severity(result)
        icon = {"success": "✅", "warning": "⚠️", "fail": "❌"}[severity]
        lines.append(f"{icon} {result.display_name}")
        if result.ok:
            lines.extend(
                [
                    f"签到：{result.sign_status}",
                    f"奖励：{result.reward or '未返回奖励信息'}",
                    f"连签：{result.progress or '未返回'}",
                    f"空间：{result.total_space or '未返回'}",
                ]
            )
        else:
            lines.append(f"{result.error_stage or '执行'}：{result.error_message}")
        lines.append("")
    lines.extend(
        [
            f"⏱️ 耗时：{format_duration(elapsed)}",
            f"🕒 完成：{datetime.now().strftime('%m-%d %H:%M')}",
        ]
    )
    return "\n".join(lines).strip()


def build_daily_details(results: list[AccountResult]) -> list[str]:
    success_count = sum(item.ok for item in results)
    details = [] if len(results) == 1 else [f"成功 {success_count}/{len(results)}"]
    for result in results:
        prefix = f"{result.display_name}：" if len(results) > 1 else ""
        if result.ok:
            details.append(
                f"{prefix}{result.sign_status}"
                f"｜奖励 {result.reward or '未返回'}"
                f"｜连签 {result.progress or '未返回'}"
                f"｜空间 {result.total_space or '未返回'}"
            )
        else:
            details.append(
                f"{prefix}{result.error_stage or '执行'}失败"
                f"（{result.error_message}）"
            )
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


def _early_failure(message: str, daily_child: bool, started_at: float) -> int:
    elapsed = time.monotonic() - started_at
    print(f"❌ 任务失败\n{message}")
    emit_daily_result("fail", [message], daily_child)
    notify_result = system_notify(
        "夸克网盘 · 有失败",
        "\n".join(["📋 任务结果", "", "❌ 任务失败", message, "", f"⏱️ 耗时：{format_duration(elapsed)}"]),
        daily_child,
    )
    print(f"通知：{notify_result}")
    print(f"==== 完成 | 耗时 {format_duration(elapsed)} ====")
    return 1


# 程序入口

def main(
    environ: Mapping[str, str] | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    configure_utf8_output()
    started_at = time.monotonic()
    env = os.environ if environ is None else environ
    daily_child = _clean_field(env.get("DAILY_ALL_CHILD")) == "1"
    print(
        "==== 夸克网盘签到 | "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===="
    )

    raw_accounts = str(env.get(ENV_NAME, "") or "")
    if skip_inactive_service(raw_accounts, str(env.get("DAILY_ALL_DISABLED", "") or ""), daily_child):
        return 0
    accounts = parse_accounts(raw_accounts)
    if not accounts:
        return _early_failure(
            f"环境变量 {ENV_NAME} 中没有有效账号", daily_child, started_at
        )

    print(f"发现 {len(accounts)} 个账号")
    factory = create_http_session if session_factory is None else session_factory
    results: list[AccountResult] = []

    for index, account in enumerate(accounts, start=1):
        session = None
        if account.validation_error:
            result = AccountResult(
                display_name=account.display_name,
                ok=False,
                status="invalid_config",
                error_stage="配置",
                error_message=account.validation_error,
            )
        else:
            try:
                session = factory()
                result = QuarkClient(account, session=session).run()
            except Exception as exc:
                result = AccountResult(
                    display_name=account.display_name,
                    ok=False,
                    status="error",
                    error_stage="脚本处理",
                    error_message=redact_text(
                        f"执行异常（{type(exc).__name__}）",
                        account.secret_values,
                    ),
                )
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
        results.append(result)
        print()
        print(format_account_log(index, result))

    success_count = sum(item.ok for item in results)
    status = merge_status(results)
    elapsed = time.monotonic() - started_at
    print()
    print(f"结果：成功 {success_count}/{len(results)}")
    emit_daily_result(status, build_daily_details(results), daily_child)
    title_suffix = {"success": "全部完成", "warn": "有提醒", "fail": "有失败"}[status]
    notify_result = system_notify(
        f"夸克网盘 · {title_suffix}",
        format_notification(results, elapsed),
        daily_child,
    )
    print(f"通知：{notify_result}")
    print(f"==== 完成 | 耗时 {format_duration(elapsed)} ====")
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
