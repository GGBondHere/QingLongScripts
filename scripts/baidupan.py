#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
名称：百度网盘每日任务
用途：网页/APP 签到、每日答题，查询会员、成长值与升级进度。

青龙配置：
- 执行：task QingLongScripts/baidupan.py now。
- 独立任务：启用、单次类型；日常由 daily_all.py 执行。
- 依赖：requests。
- 通知：在青龙面板配置，合集只发送汇总通知。

环境变量：
- BAIDU_COOKIE：完整网页 Cookie，多账号每行一个，不加 #备注。
- BAIDU_DEVICE_ID（可选）：APP 的 cuid 或 devuid，按 Cookie 账号顺序每行一个；留空只运行网页任务。

凭证获取：
1. 登录 https://pan.baidu.com/，F12 → 网络，刷新页面并复制本站请求头 Cookie 的完整值。
2. 需要 APP 签到时，抓取 APP“成长值任务”页的 /coins/taskcenter/signinlist 请求，将 cuid 或 devuid 填入 BAIDU_DEVICE_ID。

填写示例：
- BAIDU_COOKIE：完整Cookie1（多账号换行）
- BAIDU_DEVICE_ID：设备值1（按账号顺序填写）

使用说明：
- APP 及答题奖励需在手机成长值页手动领取；广告和连续签到奖励自行处理。
- 停用标识：baidupan；缺凭证或已停用时跳过。详见 docs/baidupan.md。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Any

# 运行配置

def skip_inactive_service(raw: str, disabled_value: str, daily_child: bool) -> bool:
    """未配置凭证或已停用时跳过。"""
    disabled = {item.strip().casefold() for item in re.split(r"[,;|&\n]+", disabled_value)}
    if raw.strip() and not disabled.intersection({"baidupan", "百度网盘"}):
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
    os.getenv("BAIDU_COOKIE", ""), os.getenv("DAILY_ALL_DISABLED", ""),
    os.getenv("DAILY_ALL_CHILD", "").strip() == "1",
):
    raise SystemExit(0)


import requests


# 配置与数据模型

SCRIPT_NAME = "百度网盘"
COOKIE_ENV_NAME = "BAIDU_COOKIE"
DEVICE_ENV_NAME = "BAIDU_DEVICE_ID"
DAILY_CHILD_ENV_NAME = "DAILY_ALL_CHILD"
DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="
REQUEST_TIMEOUT = (5, 15)
CHINA_TIMEZONE = timezone(timedelta(hours=8))
GROWTH_HISTORY_PAGE_SIZE = 15
GROWTH_HISTORY_MAX_PAGES = 20

MEMBERSHIP_LEVEL_URL = "https://pan.baidu.com/rest/2.0/membership/level"

SIGNIN_URL = (
    f"{MEMBERSHIP_LEVEL_URL}?app_id=250528&web=5&method=signin"
)
QUESTION_URL = (
    "https://pan.baidu.com/act/v2/membergrowv2/getdailyquestion"
    "?app_id=250528&web=5"
)
ANSWER_URL = "https://pan.baidu.com/act/v2/membergrowv2/answerquestion"
MEMBER_URL = (
    "https://pan.baidu.com/rest/2.0/membership/user"
    "?app_id=250528&web=5&method=query"
)
APP_TASK_LIST_URL = "https://pan.baidu.com/coins/taskcenter/tasklist"
APP_SIGN_URL = "https://pan.baidu.com/coins/taskcenter/signin"
APP_SIGN_LIST_URL = "https://pan.baidu.com/coins/taskcenter/signinlist"
GROWTH_TASK_LIST_URL = "https://pan.baidu.com/api/taskscore/tasklist"

APP_SIGN_TASK_TYPE = 166
ANSWER_TASK_TYPE = 169
# 网页签到的成长明细事件类型；奖励数值读取 value。
WEB_SIGN_GROWTH_EVENT_ID = 40014
TASK_STATUS_WILL = 0
TASK_STATUS_DONE = 1
TASK_STATUS_DOING = 3
TASK_STATUS_WAIT = 6

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://pan.baidu.com/wap/svip/growth/task",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

STATUS_ICONS = {
    "success": "✅",
    "warning": "⚠️",
    "fail": "❌",
}
STATUS_PRIORITY = {
    "success": 0,
    "warning": 1,
    "fail": 2,
}


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
class StepResult:
    status: str
    message: str


@dataclass(frozen=True)
class Question:
    answer: str
    ask_id: str


@dataclass(frozen=True)
class GrowthProgress:
    next_level: int | None = None
    target_value: int | None = None
    remaining_value: int | None = None
    yesterday_value: int | None = None
    estimated_days: int | None = None
    max_level: bool = False
    warning: str = ""


@dataclass(frozen=True)
class MemberResult:
    status: str
    level: str
    growth: str
    progress: GrowthProgress = field(default_factory=GrowthProgress)


@dataclass(frozen=True)
class AppSignState:
    signed: bool
    growth_reward: int | None = None


@dataclass(frozen=True)
class TaskState:
    task_id: str
    task_from: str
    task_status: int
    growth_reward: int | None = None


@dataclass(frozen=True)
class AccountResult:
    index: int
    status: str
    web_signin: str
    app_signin: str
    answer: str
    level: str
    growth: str
    progress: GrowthProgress = field(default_factory=GrowthProgress)


# 环境变量与凭证解析

def parse_accounts(raw: str) -> list[str]:
    """按行解析账号。"""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def parse_device_ids(raw: str) -> list[str]:
    """设备标识按行与账号对齐，不在日志中回显。"""
    return [line.strip() for line in raw.splitlines()]


def merge_statuses(*statuses: str) -> str:
    """按失败、提醒、成功的优先级合并多个状态。"""
    return max(statuses, key=STATUS_PRIORITY.__getitem__)


def _parse_payload(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}


def _find_value(payload: Any, key: str) -> Any:
    """递归读取接口字段，避免依赖字段所在的固定层级。"""
    if isinstance(payload, Mapping):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_value(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_value(value, key)
            if found is not None:
                return found
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_embedded_json(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    parsed = _parse_payload(value)
    return parsed if isinstance(parsed, Mapping) else {}


def _find_task(payload: Any, task_type: int) -> Mapping[str, Any] | None:
    """按任务类型查找当天任务。"""
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    tasks = result.get("list")
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        current_type = _to_int(task.get("task_type"))
        if current_type is None:
            current_type = _to_int(
                _parse_embedded_json(task.get("task_extra")).get("task_type")
            )
        if current_type == task_type:
            return task
    return None


def _growth_prize_value(prizes: Any) -> int | None:
    """读取 APP 签到奖品中的成长值；其他奖品类型不进入通知。"""
    if not isinstance(prizes, list):
        return None
    for prize in prizes:
        if not isinstance(prize, Mapping):
            continue
        prize_type = _to_int(prize.get("type"))
        value = _to_int(prize.get("unit"))
        if prize_type == 3 and value is not None:
            return value
    return None


def _task_growth_reward(task: Mapping[str, Any]) -> int | None:
    extra = _parse_embedded_json(task.get("task_extra"))
    return _to_int(extra.get("task_grow_score"))


def _task_state_from_mapping(
    task: Mapping[str, Any],
    *,
    task_name: str,
) -> tuple[TaskState | None, StepResult]:
    """解析当天任务状态。"""
    task_id = str(task.get("task_id_str") or task.get("task_id") or "").strip()
    task_from = str(task.get("task_from") or "").strip()
    task_status = _to_int(task.get("task_status"))
    if not task_id or not task_from or task_status is None:
        return None, StepResult("warning", f"{task_name}任务参数不完整")
    return (
        TaskState(
            task_id,
            task_from,
            task_status,
            _task_growth_reward(task),
        ),
        StepResult("success", "状态已读取"),
    )


def _growth_reward_suffix(growth_reward: int | None) -> str:
    if growth_reward is None:
        return "｜成长值未返回"
    return f"｜成长值+{growth_reward}"


def china_today() -> date:
    return datetime.now(CHINA_TIMEZONE).date()


def parse_level_thresholds(payload: Any) -> dict[int, int]:
    """读取 config 响应中的 data.level_infos.<等级>.value。"""
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return {}
    level_infos = data.get("level_infos")
    if not isinstance(level_infos, Mapping):
        return {}

    thresholds: dict[int, int] = {}
    for raw_level, raw_info in level_infos.items():
        if not isinstance(raw_info, Mapping):
            continue
        level = _to_int(raw_level)
        value = _to_int(raw_info.get("value"))
        if level is not None and value is not None:
            thresholds[level] = value
    return thresholds


def calculate_growth_progress(
    current_level: int,
    current_value: int,
    thresholds: Mapping[int, int],
    yesterday_value: int | None,
    *,
    warning: str = "",
) -> GrowthProgress:
    """按接口门槛和昨日全部成长值计算下一等级进度。"""
    higher_levels = sorted(level for level in thresholds if level > current_level)
    if not higher_levels:
        return GrowthProgress(
            yesterday_value=yesterday_value,
            max_level=bool(thresholds) and current_level >= max(thresholds),
            warning=warning,
        )

    next_level = higher_levels[0]
    target_value = thresholds[next_level]
    remaining_value = max(0, target_value - current_value)
    if remaining_value == 0:
        estimated_days = 0
    elif yesterday_value is not None and yesterday_value > 0:
        estimated_days = ceil(remaining_value / yesterday_value)
    else:
        estimated_days = None

    return GrowthProgress(
        next_level=next_level,
        target_value=target_value,
        remaining_value=remaining_value,
        yesterday_value=yesterday_value,
        estimated_days=estimated_days,
        warning=warning,
    )


def _growth_record_date(record: Mapping[str, Any]) -> date | None:
    timestr = str(record.get("timestr") or "").strip()
    if len(timestr) >= 10:
        try:
            return date.fromisoformat(timestr[:10])
        except ValueError:
            pass

    timestamp = _to_int(record.get("time"))
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, CHINA_TIMEZONE).date()
    except (OSError, OverflowError, ValueError):
        return None


# 网络请求与业务逻辑

def _request_error(task: str, error: Exception) -> str:
    if isinstance(error, requests.exceptions.Timeout):
        return f"{task}请求超时"
    if isinstance(error, requests.exceptions.ConnectionError):
        return f"{task}网络连接失败"
    return f"{task}请求异常（{type(error).__name__}）"


class BaiduPan:
    def __init__(
        self,
        cookie: str,
        index: int = 1,
        device_id: str = "",
        *,
        session: Any = None,
        sleep_func: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        today_func: Callable[[], date] = china_today,
    ) -> None:
        self.cookie = cookie.strip()
        self.index = index
        self.device_id = device_id.strip()
        self.session = session or requests.Session()
        self.sleep_func = sleep_func
        self.random_uniform = random_uniform
        self.today_func = today_func
        self._growth_history_pages: dict[int, list[Mapping[str, Any]]] = {}

    def _headers(self) -> dict[str, str]:
        headers = BASE_HEADERS.copy()
        headers["Cookie"] = self.cookie
        return headers

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        safe_retry: bool = False,
    ):
        """查询请求可重试一次；签到和答题写请求始终只发送一次。"""
        attempts = 2 if safe_retry else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                request_kwargs: dict[str, Any] = {
                    "headers": self._headers(),
                    "timeout": REQUEST_TIMEOUT,
                }
                if params is not None:
                    request_kwargs["params"] = dict(params)
                response = self.session.get(url, **request_kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                last_error = error
                if attempt + 1 < attempts:
                    continue
                raise
            if (
                safe_retry
                and response.status_code in {502, 503, 504}
                and attempt + 1 < attempts
            ):
                continue
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("请求未返回结果")

    def _mobile_params(self) -> dict[str, Any]:
        return {
            "clienttype": 1,
            "cuid": self.device_id,
            "devuid": self.device_id,
        }

    def _query_app_sign_state(
        self,
    ) -> tuple[AppSignState | None, StepResult]:
        try:
            response = self._get(
                APP_SIGN_LIST_URL,
                params=self._mobile_params(),
                safe_retry=True,
            )
        except Exception as error:
            return None, StepResult("warning", _request_error("APP签到状态", error))

        if response.status_code in (401, 403):
            return None, StepResult(
                "fail",
                f"Cookie无效或已过期（HTTP {response.status_code}）",
            )
        if response.status_code != 200:
            return None, StepResult(
                "warning",
                f"APP签到状态查询失败（HTTP {response.status_code}）",
            )

        payload = _parse_payload(response.text)
        if not isinstance(payload, Mapping) or _to_int(payload.get("errno")) != 0:
            message = str(
                _find_value(payload, "error")
                or _find_value(payload, "errmsg")
                or "返回异常"
            ).strip()
            return None, StepResult("warning", f"APP签到状态查询失败：{message}")

        data = payload.get("data")
        if not isinstance(data, Mapping):
            return None, StepResult("warning", "APP签到状态未返回 data")
        day = _to_int(data.get("day"))
        current_row: Mapping[str, Any] | None = None
        rows = data.get("signin_list")
        if isinstance(rows, list) and day is not None:
            current_row = next(
                (
                    row
                    for row in rows
                    if isinstance(row, Mapping) and _to_int(row.get("day")) == day
                ),
                None,
            )
        signed = _to_int(data.get("signed_today")) == 1
        if current_row is not None:
            signed = signed or _to_int(current_row.get("signed")) == 1
        growth_reward = (
            _growth_prize_value(current_row.get("prize")) if current_row else None
        )
        return AppSignState(signed, growth_reward), StepResult(
            "success",
            "状态已读取",
        )

    def _get_app_sign_task(
        self,
    ) -> tuple[Mapping[str, Any] | None, StepResult]:
        params = self._mobile_params()
        params["task_from"] = "task_sys_daily"
        try:
            response = self._get(APP_TASK_LIST_URL, params=params, safe_retry=True)
        except Exception as error:
            return None, StepResult("warning", _request_error("APP签到任务", error))
        if response.status_code != 200:
            return None, StepResult(
                "warning",
                f"APP签到任务查询失败（HTTP {response.status_code}）",
            )
        payload = _parse_payload(response.text)
        if not isinstance(payload, Mapping) or _to_int(payload.get("errno")) != 0:
            return None, StepResult("warning", "APP签到任务返回异常")
        task = _find_task(payload, APP_SIGN_TASK_TYPE)
        if task is None:
            return None, StepResult("warning", "未找到当天APP签到任务")
        return task, StepResult("success", "任务已读取")

    def _query_app_reward_task(
        self,
    ) -> tuple[TaskState | None, StepResult]:
        task, query_result = self._get_app_sign_task()
        if task is None:
            return None, query_result
        return _task_state_from_mapping(task, task_name="APP签到领奖")

    @staticmethod
    def _app_sign_message(prefix: str, state: AppSignState) -> str:
        return f"{prefix}{_growth_reward_suffix(state.growth_reward)}"

    def app_signin(self) -> StepResult:
        """首次运行提交一次 APP 签到，重复运行只读取当日状态。"""
        if not self.device_id:
            return StepResult("warning", f"未配置 {DEVICE_ENV_NAME}")

        before, state_result = self._query_app_sign_state()
        if before is None:
            return state_result
        if before.signed:
            return StepResult(
                "success",
                self._app_sign_message("今日已签到", before),
            )

        task, task_result = self._get_app_sign_task()
        if task is None:
            return task_result
        task_id = str(task.get("task_id_str") or task.get("task_id") or "").strip()
        task_from = str(task.get("task_from") or "").strip()
        if not task_id or not task_from:
            return StepResult("warning", "APP签到任务参数不完整")

        params = self._mobile_params()
        params.update(
            {
                "task_id": task_id,
                "task_id_str": task_id,
                "task_from": task_from,
                "is_growth": 1,
            }
        )
        self.sleep_func(self.random_uniform(2, 5))
        try:
            response = self._get(APP_SIGN_URL, params=params)
        except Exception as error:
            return StepResult("fail", _request_error("APP签到", error))
        if response.status_code in (401, 403):
            return StepResult(
                "fail",
                f"Cookie无效或已过期（HTTP {response.status_code}）",
            )
        if response.status_code != 200:
            return StepResult("fail", f"APP签到请求失败（HTTP {response.status_code}）")
        payload = _parse_payload(response.text)
        if not isinstance(payload, Mapping) or _to_int(payload.get("errno")) != 0:
            message = str(
                _find_value(payload, "error")
                or _find_value(payload, "errmsg")
                or "返回异常"
            ).strip()
            return StepResult("fail", f"APP签到失败：{message}")

        self.sleep_func(0.8)
        after, verify_result = self._query_app_sign_state()
        if after is None:
            return StepResult(
                "warning",
                f"APP签到已提交，但{verify_result.message}",
            )
        if not after.signed:
            return StepResult("warning", "APP签到已提交，结果尚未确认")
        return StepResult(
            "success",
            self._app_sign_message("签到成功", after),
        )

    def web_signin(self) -> StepResult:
        try:
            response = self._get(SIGNIN_URL)
        except Exception as error:
            return StepResult("fail", _request_error("签到", error))

        if response.status_code in (401, 403):
            return StepResult(
                "fail",
                f"Cookie无效或已过期（HTTP {response.status_code}）",
            )
        if response.status_code != 200:
            return StepResult(
                "fail",
                f"签到请求失败（HTTP {response.status_code}）",
            )

        payload = _parse_payload(response.text)
        growth_reward = _to_int(_find_value(payload, "points"))
        if growth_reward is not None:
            return StepResult("success", f"签到成功｜成长值+{growth_reward}")

        message = str(_find_value(payload, "error_msg") or "").strip()
        completed_keywords = (
            "已签到",
            "重复签到",
            "repeat signin",
            "already sign",
            "not allow",
        )
        if message and any(
            keyword in message.lower() for keyword in completed_keywords
        ):
            growth_reward, warning = self.get_today_web_sign_growth()
            if growth_reward is not None:
                return StepResult(
                    "success",
                    f"今日已签到｜成长值+{growth_reward}",
                )
            return StepResult(
                "warning",
                f"今日已签到｜{warning or '成长值未返回'}",
            )
        if message:
            return StepResult("fail", f"签到失败：{message}")
        return StepResult("fail", "签到结果未确认")

    def get_daily_question(self) -> tuple[Question | None, StepResult]:
        try:
            response = self._get(QUESTION_URL, safe_retry=True)
        except Exception as error:
            return None, StepResult("warning", _request_error("获取每日问题", error))

        if response.status_code in (401, 403):
            return None, StepResult(
                "fail",
                f"Cookie无效或已过期（HTTP {response.status_code}）",
            )
        if response.status_code != 200:
            return None, StepResult(
                "warning",
                f"获取每日问题失败（HTTP {response.status_code}）",
            )

        payload = _parse_payload(response.text)
        answer = _find_value(payload, "answer")
        ask_id = _find_value(payload, "ask_id")
        if answer is None or ask_id is None:
            return None, StepResult("warning", "未获取到每日问题")
        return Question(str(answer), str(ask_id)), StepResult(
            "success",
            "已获取每日问题",
        )

    def answer_question(self, question: Question) -> StepResult:
        url = (
            f"{ANSWER_URL}?app_id=250528&web=5"
            f"&ask_id={question.ask_id}&answer={question.answer}"
        )
        try:
            response = self._get(url)
        except Exception as error:
            return StepResult("fail", _request_error("答题", error))

        if response.status_code in (401, 403):
            return StepResult(
                "fail",
                f"Cookie无效或已过期（HTTP {response.status_code}）",
            )
        if response.status_code != 200:
            return StepResult(
                "fail",
                f"答题请求失败（HTTP {response.status_code}）",
            )

        payload = _parse_payload(response.text)
        score = _find_value(payload, "score")
        if score is not None:
            return StepResult("success", f"答题完成（成长值+{score}待领取）")

        message = str(_find_value(payload, "show_msg") or "").strip()
        completed_keywords = (
            "已回答",
            "已答题",
            "次数已用完",
            "次数用完",
            "exceeded",
            "already",
            "超出",
            "超限",
        )
        if message and any(
            keyword in message.lower() for keyword in completed_keywords
        ):
            return StepResult("success", "今日已答题")
        if message:
            return StepResult("fail", f"答题失败：{message}")
        return StepResult("fail", "答题结果未确认")

    def _query_answer_task(
        self,
    ) -> tuple[TaskState | None, StepResult]:
        try:
            response = self._get(
                GROWTH_TASK_LIST_URL,
                params={"task_from": "task_sys_task_growth"},
                safe_retry=True,
            )
        except Exception as error:
            return None, StepResult("warning", _request_error("答题领奖状态", error))
        if response.status_code in (401, 403):
            return None, StepResult(
                "fail",
                f"Cookie无效或已过期（HTTP {response.status_code}）",
            )
        if response.status_code != 200:
            return None, StepResult(
                "warning",
                f"答题领奖状态查询失败（HTTP {response.status_code}）",
            )

        payload = _parse_payload(response.text)
        if not isinstance(payload, Mapping) or _to_int(payload.get("errno")) != 0:
            return None, StepResult("warning", "答题领奖状态返回异常")
        task = _find_task(payload, ANSWER_TASK_TYPE)
        if task is None:
            return None, StepResult("warning", "未找到当天答题领奖任务")
        return _task_state_from_mapping(task, task_name="答题领奖")

    @staticmethod
    def _answer_task_result(task: TaskState) -> StepResult:
        rewards = _growth_reward_suffix(task.growth_reward)
        if task.task_status == TASK_STATUS_DONE:
            return StepResult("success", f"已完成并领取{rewards}")
        if task.task_status == TASK_STATUS_WAIT:
            return StepResult("success", f"已完成，奖励待手动领取{rewards}")
        if task.task_status == TASK_STATUS_DOING:
            return StepResult(
                "warning",
                f"任务处理中，请手动打开成长值页面确认{rewards}",
            )
        if task.task_status == TASK_STATUS_WILL:
            return StepResult("success", f"已答题，奖励待手动领取{rewards}")
        return StepResult(
            "warning",
            f"领奖状态未知（{task.task_status}）{rewards}",
        )

    @staticmethod
    def _app_reward_result(
        signin: StepResult,
        task: TaskState | None,
        query_result: StepResult,
    ) -> StepResult:
        if signin.status == "fail":
            return signin
        if task is None:
            return StepResult(
                merge_statuses(signin.status, query_result.status),
                f"{signin.message}；{query_result.message}",
            )
        if task.task_status == TASK_STATUS_DONE:
            title, separator, reward = signin.message.partition("｜")
            message = f"{title}，奖励已领取"
            if separator:
                message += f"｜{reward}"
            return StepResult(signin.status, message)
        if task.task_status in {TASK_STATUS_WAIT, TASK_STATUS_WILL}:
            return StepResult(
                signin.status,
                f"{signin.message}；签到奖励待手动领取",
            )
        if task.task_status == TASK_STATUS_DOING:
            return StepResult(
                "warning",
                f"{signin.message}；签到奖励处理中，请手动确认",
            )
        return StepResult(
            "warning",
            f"{signin.message}；签到领奖状态未知（{task.task_status}）",
        )

    def _answer_reward_result(
        self,
        submission: StepResult,
        task: TaskState | None,
        query_result: StepResult,
    ) -> StepResult:
        if task is None:
            if query_result.status == "fail":
                return query_result
            if submission.status == "fail":
                return submission
            return StepResult(
                "warning",
                f"{submission.message}；{query_result.message}",
            )
        if submission.status == "fail" and task.task_status != TASK_STATUS_DONE:
            return submission
        if task.task_status == TASK_STATUS_WILL and submission.status == "warning":
            rewards = _growth_reward_suffix(task.growth_reward)
            return StepResult(
                "warning",
                f"{submission.message}；奖励任务尚未完成{rewards}",
            )
        return self._answer_task_result(task)

    def resolve_task_rewards(
        self,
        app_signin: StepResult,
        submission: StepResult,
    ) -> tuple[StepResult, StepResult]:
        """只查询两项目标奖励状态；待领取时交由用户手动处理。"""
        app_task, app_query = self._query_app_reward_task()
        answer_task, answer_query = self._query_answer_task()

        app_result = self._app_reward_result(app_signin, app_task, app_query)
        answer_result = self._answer_reward_result(
            submission,
            answer_task,
            answer_query,
        )
        return app_result, answer_result

    def get_level_thresholds(self) -> tuple[dict[int, int] | None, str]:
        try:
            response = self._get(
                MEMBERSHIP_LEVEL_URL,
                params={"method": "config", "app_id": "250528", "web": "5"},
                safe_retry=True,
            )
        except Exception as error:
            return None, _request_error("等级配置", error)

        if response.status_code != 200:
            return None, f"等级配置查询失败（HTTP {response.status_code}）"
        payload = _parse_payload(response.text)
        if not isinstance(payload, Mapping) or _to_int(payload.get("error_code")) != 0:
            return None, "等级配置返回异常"
        thresholds = parse_level_thresholds(payload)
        if not thresholds:
            return None, "等级配置未返回门槛"
        return thresholds, ""

    def _get_growth_records_for_date(
        self,
        target_date: date,
        task_name: str,
    ) -> tuple[list[Mapping[str, Any]] | None, str]:
        """分页读取指定日期的明细，并复用同一次运行中已读取的页面。"""
        records: list[Mapping[str, Any]] = []
        start = 0

        for _page in range(GROWTH_HISTORY_MAX_PAGES):
            rows = self._growth_history_pages.get(start)
            if rows is None:
                try:
                    response = self._get(
                        MEMBERSHIP_LEVEL_URL,
                        params={
                            "method": "list",
                            "start": start,
                            "limit": GROWTH_HISTORY_PAGE_SIZE,
                        },
                        safe_retry=True,
                    )
                except Exception as error:
                    return None, _request_error(task_name, error)

                if response.status_code != 200:
                    return None, f"{task_name}查询失败（HTTP {response.status_code}）"
                payload = _parse_payload(response.text)
                if (
                    not isinstance(payload, Mapping)
                    or _to_int(payload.get("error_code")) != 0
                ):
                    return None, f"{task_name}返回异常"

                raw_rows = payload.get("level_list_infos")
                if not isinstance(raw_rows, list):
                    return None, f"{task_name}未返回明细"
                rows = [row for row in raw_rows if isinstance(row, Mapping)]
                self._growth_history_pages[start] = rows
            if not rows:
                return records, ""

            reached_older_record = False
            for row in rows:
                record_date = _growth_record_date(row)
                if record_date == target_date:
                    records.append(row)
                elif record_date is not None and record_date < target_date:
                    reached_older_record = True

            if reached_older_record or len(rows) < GROWTH_HISTORY_PAGE_SIZE:
                return records, ""
            start += GROWTH_HISTORY_PAGE_SIZE

        return None, f"{task_name}明细超过分页上限"

    def get_today_web_sign_growth(self) -> tuple[int | None, str]:
        records, warning = self._get_growth_records_for_date(
            self.today_func(),
            "网页签到成长值",
        )
        if records is None:
            return None, warning
        for record in records:
            if _to_int(record.get("event_id")) != WEB_SIGN_GROWTH_EVENT_ID:
                continue
            value = _to_int(record.get("value"))
            if value is not None:
                return value, ""
        return None, "网页签到成长值未返回"

    def get_yesterday_growth(self) -> tuple[int | None, str]:
        records, warning = self._get_growth_records_for_date(
            self.today_func() - timedelta(days=1),
            "昨日成长",
        )
        if records is None:
            return None, warning
        total = 0
        for record in records:
            value = _to_int(record.get("value"))
            if value is not None:
                # 升级估算使用昨日全部成长值记录。
                total += value
        return total, ""

    def get_user_info(self) -> MemberResult:
        try:
            response = self._get(MEMBER_URL, safe_retry=True)
        except Exception as error:
            return MemberResult(
                "warning",
                "未知",
                "未知",
                GrowthProgress(warning=_request_error("会员信息", error)),
            )

        if response.status_code != 200:
            return MemberResult(
                "warning",
                "未知",
                "未知",
                GrowthProgress(
                    warning=f"会员信息查询失败（HTTP {response.status_code}）"
                ),
            )

        payload = _parse_payload(response.text)
        if not isinstance(payload, Mapping) or _to_int(payload.get("error_code")) != 0:
            return MemberResult(
                "warning",
                "未知",
                "未知",
                GrowthProgress(warning="会员信息返回异常"),
            )

        level_info = payload.get("level_info")
        if not isinstance(level_info, Mapping):
            return MemberResult(
                "warning",
                "未知",
                "未知",
                GrowthProgress(warning="会员信息未返回 level_info"),
            )
        level = _to_int(level_info.get("current_level"))
        growth = _to_int(level_info.get("current_value"))
        if level is None or growth is None:
            return MemberResult(
                "warning",
                f"SVIP{level}" if level is not None else "未知",
                str(growth) if growth is not None else "未知",
                GrowthProgress(warning="会员等级信息不完整"),
            )

        thresholds, threshold_warning = self.get_level_thresholds()
        yesterday_value, history_warning = self.get_yesterday_growth()
        warnings = [
            message for message in (threshold_warning, history_warning) if message
        ]
        progress = calculate_growth_progress(
            level,
            growth,
            thresholds or {},
            yesterday_value,
            warning="；".join(warnings),
        )
        status = "warning" if warnings else "success"
        return MemberResult(status, f"SVIP{level}", str(growth), progress)

    def run(self) -> AccountResult:
        web_signin = self.web_signin()
        if web_signin.status == "fail":
            return AccountResult(
                self.index,
                "fail",
                web_signin.message,
                "未执行" if self.device_id else "",
                "未执行",
                "未知",
                "未知",
            )

        question, question_result = self.get_daily_question()
        if question is not None:
            # 写请求之间保留调用间隔。
            self.sleep_func(self.random_uniform(2, 5))
            answer_submission = self.answer_question(question)
        else:
            answer_submission = question_result

        if self.device_id:
            app_signin = self.app_signin()
            app_signin, answer = self.resolve_task_rewards(app_signin, answer_submission)
        else:
            app_signin = StepResult("success", "")
            answer_task, answer_query = self._query_answer_task()
            answer = self._answer_reward_result(answer_submission, answer_task, answer_query)

        member = self.get_user_info()
        return AccountResult(
            self.index,
            merge_statuses(
                web_signin.status,
                app_signin.status,
                answer.status,
                member.status,
            ),
            web_signin.message,
            app_signin.message,
            answer.message,
            member.level,
            member.growth,
            member.progress,
        )


# 日志、通知与合集结果

def format_duration(elapsed: float) -> str:
    seconds = max(0, int(round(elapsed)))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"


def format_account_block(result: AccountResult) -> str:
    icon = STATUS_ICONS[result.status]
    lines = [
        f"{icon} 账号{result.index}",
        f"网页签到：{result.web_signin}",
    ]
    if result.app_signin:
        lines.append(f"APP签到：{result.app_signin}")
    lines.extend([
        f"答题：{result.answer}",
        f"会员：{result.level}",
        f"成长值：{format_growth_value(result)}",
    ])
    upgrade_line = format_upgrade_line(result)
    if upgrade_line:
        lines.append(upgrade_line)
    if result.progress.warning:
        lines.append(f"提醒：{result.progress.warning}")
    return "\n".join(lines)


def format_growth_value(result: AccountResult) -> str:
    target = result.progress.target_value
    if target is None:
        return result.growth
    return f"{result.growth} / {target}"


def format_upgrade_line(result: AccountResult) -> str:
    progress = result.progress
    if progress.max_level:
        return "升级：已达最高等级"

    parts: list[str] = []
    if progress.remaining_value is not None:
        parts.append(f"还差{progress.remaining_value}")
    if progress.yesterday_value is not None:
        parts.append(f"昨日+{progress.yesterday_value}")
    if progress.estimated_days is not None:
        parts.append(f"预计{progress.estimated_days}天")
    elif progress.remaining_value is not None and progress.yesterday_value is not None:
        parts.append("暂无法估算")
    return f"升级：{'｜'.join(parts)}" if parts else ""


def format_notification(results: list[AccountResult], elapsed: float) -> str:
    lines = ["📋 任务结果", ""]
    for index, result in enumerate(results):
        if index:
            lines.append("")
        lines.extend(format_account_block(result).splitlines())
    lines.extend(["", f"⏱️ 耗时：{format_duration(elapsed)}"])
    return "\n".join(lines)


def build_daily_details(results: list[AccountResult]) -> list[str]:
    if len(results) == 1:
        result = results[0]
        details = [
            f"网页签到：{result.web_signin}",
        ]
        if result.app_signin:
            details.append(f"APP签到：{result.app_signin}")
        details.extend([
            f"答题：{result.answer}",
            f"会员：{result.level}｜成长值：{format_growth_value(result)}",
        ])
        upgrade_line = format_upgrade_line(result)
        if upgrade_line:
            details.append(upgrade_line)
        if result.progress.warning:
            details.append(f"提醒：{result.progress.warning}")
        return details

    success_count = sum(result.status == "success" for result in results)
    warning_count = sum(result.status == "warning" for result in results)
    failure_count = sum(result.status == "fail" for result in results)
    details = [
        f"完成 {success_count}/{len(results)}｜"
        f"提醒 {warning_count}｜失败 {failure_count}"
    ]
    for result in results:
        details.append(
            f"账号{result.index}：网页签到：{result.web_signin}"
        )
        if result.app_signin:
            details.append(f"账号{result.index}：APP签到：{result.app_signin}")
        details.append(f"账号{result.index}：答题：{result.answer}")
        details.append(
            f"账号{result.index}：会员：{result.level}｜"
            f"成长值：{format_growth_value(result)}"
        )
        upgrade_line = format_upgrade_line(result)
        if upgrade_line:
            details.append(f"账号{result.index}：{upgrade_line}")
        if result.progress.warning:
            details.append(f"账号{result.index}：提醒：{result.progress.warning}")
    return details


def notification_title(status: str) -> str:
    suffix = {
        "success": "全部完成",
        "warning": "有提醒",
        "fail": "有失败",
    }[status]
    return f"{SCRIPT_NAME} · {suffix}"


def system_notify(title: str, content: str, daily_child: bool) -> str:
    if daily_child:
        return "由合集统一发送"
    try:
        QLAPI.systemNotify({"title": title, "content": content})
        return "已发送"
    except NameError:
        return "当前不是青龙 task 环境，已跳过"
    except Exception as error:
        return f"发送失败（{type(error).__name__}）"


def emit_daily_result(status: str, details: list[str], daily_child: bool) -> None:
    if not daily_child:
        return
    payload = {
        "version": 1,
        "status": "warn" if status == "warning" else status,
        "details": details,
    }
    print(
        DAILY_RESULT_PREFIX
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


# 程序入口

def main(
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[str, int], Any] | None = None,
    notify_func: Callable[[str, str, bool], str] = system_notify,
) -> int:
    configure_utf8_output()
    started = time.monotonic()
    env = os.environ if environ is None else environ
    daily_child = str(env.get(DAILY_CHILD_ENV_NAME, "") or "").strip() == "1"

    print(
        f"==== {SCRIPT_NAME}每日任务 | "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===="
    )

    raw = str(env.get(COOKIE_ENV_NAME, "") or "")
    if skip_inactive_service(raw, str(env.get("DAILY_ALL_DISABLED", "") or ""), daily_child):
        return 0
    accounts = parse_accounts(raw)

    device_ids = parse_device_ids(str(env.get(DEVICE_ENV_NAME, "") or ""))
    print(f"发现 {len(accounts)} 个账号")
    results: list[AccountResult] = []
    for index, cookie in enumerate(accounts, start=1):
        device_id = device_ids[index - 1] if index <= len(device_ids) else ""
        try:
            if client_factory is None:
                client = BaiduPan(cookie, index, device_id)
            else:
                client = client_factory(cookie, index)
            result = client.run()
            if not isinstance(result, AccountResult):
                raise TypeError("账号任务没有返回 AccountResult")
        except Exception as error:
            result = AccountResult(
                index,
                "fail",
                f"执行异常（{type(error).__name__}）",
                "未执行" if device_id else "",
                "未执行",
                "未知",
                "未知",
            )
        results.append(result)
        print()
        print(format_account_block(result))

    overall_status = merge_statuses(*(result.status for result in results))
    elapsed = time.monotonic() - started
    details = build_daily_details(results)
    emit_daily_result(overall_status, details, daily_child)

    success_count = sum(result.status == "success" for result in results)
    warning_count = sum(result.status == "warning" for result in results)
    failure_count = sum(result.status == "fail" for result in results)
    print(
        f"\n结果：完成 {success_count}/{len(results)}｜"
        f"提醒 {warning_count}｜失败 {failure_count}"
    )

    notify_result = notify_func(
        notification_title(overall_status),
        format_notification(results, elapsed),
        daily_child,
    )
    print(f"通知：{notify_result}")
    print(f"==== 完成 | 耗时 {format_duration(elapsed)} ====")
    return 1 if overall_status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
