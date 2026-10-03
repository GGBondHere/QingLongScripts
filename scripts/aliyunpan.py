#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
名称：阿里云盘签到
用途：每日签到、查询空间，可选领取当天奖励；刷新并保存登录凭证。

青龙配置：
- 执行：task QingLongScripts/aliyunpan.py now。
- 独立任务：启用、单次类型；日常由 daily_all.py 执行。
- 依赖：requests；自动领奖另需 ecdsa。
- 通知：在青龙面板配置，合集只发送汇总通知。

环境变量：
- ALIYUN_ACCOUNTS：refresh_token#备注，多账号换行或 & 分隔，备注可选。
- ALIYUN_SIGNATURE_V2_KEY（可选）：40 位小写十六进制 HMAC 密钥，配置后启用自动领奖。
- CLIENT_ID、CLIENT_SECRET：青龙 OpenAPI 应用凭证，应用需授予环境变量权限。

凭证获取：
1. 登录阿里云盘，F12 → 网络，从登录刷新请求的 JSON 复制 refresh_token。
2. 自动领奖密钥从本人 Windows 客户端的 Signature V2 签名实现取得。

填写示例：
- 实际refresh_token#主号
- 另一个实际refresh_token#小号

使用说明：
- 未配置回写凭证时，新 refresh_token 不会保存到面板。
- 停用标识：aliyunpan；缺凭证或已停用时跳过。详见 docs/aliyunpan.md。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# 运行配置

def skip_inactive_service(raw: str, disabled_value: str, daily_child: bool) -> bool:
    """未配置凭证或已停用时跳过。"""
    disabled = {item.strip().casefold() for item in re.split(r"[,;|&\n]+", disabled_value)}
    if raw.strip() and not disabled.intersection({"aliyunpan", "阿里云盘"}):
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
    os.getenv("ALIYUN_ACCOUNTS", ""), os.getenv("DAILY_ALL_DISABLED", ""),
    os.getenv("DAILY_ALL_CHILD", "").strip() == "1",
):
    raise SystemExit(0)


import requests


# 配置与数据模型

ACCOUNT_ENV_NAME = "ALIYUN_ACCOUNTS"
SIGNATURE_KEY_ENV_NAME = "ALIYUN_SIGNATURE_V2_KEY"
DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="
QL_OPEN_BASE = "http://127.0.0.1:5700/open"

APP_ID = "25dzX3vbYqktVxyX"
API_ID = "pJZInNHN2dZWk8qg"
REQUEST_TIMEOUT = (5, 15)
LOCAL_REQUEST_TIMEOUT = (3, 8)
SIGNATURE_KEY_PATTERN = re.compile(r"^[0-9a-f]{40}$")

COMMON_HEADERS = {
    "Connection": "keep-alive",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Origin": "https://www.aliyundrive.com",
    "Referer": "https://www.aliyundrive.com/",
    "Accept-Encoding": "identity",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

MOBILE_SIGNIN_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 AlphaDrive/3.0.0"
)

WINDOWS_REWARD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) aDrive/6.9.3 "
    "Chrome/112.0.5615.165 Electron/24.1.3.6 Safari/537.36"
)
WINDOWS_CANARY = "client=windows,app=adrive,version=v6.9.3"


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


@dataclass
class AccountConfig:
    refresh_token: str
    original_refresh_token: str
    remark: str
    display_name: str

    @property
    def token_changed(self) -> bool:
        return self.refresh_token != self.original_refresh_token


@dataclass
class AccountResult:
    name: str
    ok: bool
    signin_ok: bool
    signin: str
    signin_count: int
    reward: str
    capacity: str
    reward_enabled: bool = True


@dataclass
class WritebackResult:
    enabled: bool
    attempted: bool
    ok: bool
    message: str


# 环境变量与凭证解析

def parse_accounts(raw: str) -> List[AccountConfig]:
    """解析账号，同时保留可用于回写的备注与原 refresh token。"""
    accounts: List[AccountConfig] = []
    for item in re.split(r"[&\n]+", raw.strip()):
        item = item.strip()
        if not item:
            continue

        token_part, separator, remark_part = item.partition("#")
        token = token_part.strip()
        if not token:
            continue

        remark = remark_part.strip() if separator else ""
        display_name = remark or f"账号{len(accounts) + 1}"
        accounts.append(
            AccountConfig(
                refresh_token=token,
                original_refresh_token=token,
                remark=remark,
                display_name=display_name,
            )
        )
    return accounts


def serialize_accounts(accounts: List[AccountConfig]) -> str:
    """按原账号顺序重建 ALIYUN_ACCOUNTS，只替换 refresh token。"""
    values = []
    for account in accounts:
        value = account.refresh_token
        if account.remark:
            value += f"#{account.remark}"
        values.append(value)
    return "&".join(values)


def validate_signature_v2_key(key: str) -> str:
    """只接受客户端实际使用的 40 位小写十六进制文本密钥。"""
    clean_key = key.strip()
    if not SIGNATURE_KEY_PATTERN.fullmatch(clean_key):
        raise ValueError(f"{SIGNATURE_KEY_ENV_NAME} 必须是 40 位小写十六进制密钥")
    return clean_key


def stable_device_id(user_id: str) -> str:
    """为当前阿里云盘账号生成稳定且独立的脚本设备 ID。"""
    return hashlib.sha256(f"ql-aliyun-device:{user_id}".encode("utf-8")).hexdigest()[:32]


def build_signature_v2_headers(
    device_id: str,
    key: str,
    timestamp_ms: Optional[int] = None,
    nonce: Optional[str] = None,
) -> Dict[str, str]:
    """生成领奖接口需要的 x-timestamp、x-nonce 与 x-signature-v2。"""
    clean_device_id = device_id.strip()
    if not clean_device_id:
        raise ValueError("Signature V2 需要有效的设备 ID")
    clean_key = validate_signature_v2_key(key)

    actual_timestamp = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    actual_nonce = str(uuid.uuid4()) if nonce is None else str(nonce).strip()
    if actual_timestamp <= 0 or not actual_nonce:
        raise ValueError("Signature V2 的时间戳或 nonce 无效")

    message = f"{clean_device_id}{actual_timestamp}{actual_nonce}".encode("utf-8")
    digest = hmac.new(clean_key.encode("utf-8"), message, hashlib.sha1).hexdigest()
    return {
        "x-timestamp": str(actual_timestamp),
        "x-nonce": actual_nonce,
        "x-signature-v2": digest,
    }


def build_ecc_identity(user_id: str, device_id: str) -> Tuple[str, str]:
    """生成本次 ECC 会话的 secp256k1 公钥与 x-signature。"""
    try:
        from ecdsa import SECP256k1, SigningKey
        from ecdsa.util import sigencode_string_canonize
    except ImportError as error:
        raise RuntimeError("缺少依赖 ecdsa，请在青龙依赖管理中安装 ecdsa") from error

    signing_key = SigningKey.generate(curve=SECP256k1, hashfunc=hashlib.sha256)
    public_key = "04" + signing_key.get_verifying_key().to_string().hex()
    message = f"{APP_ID}:{device_id}:{user_id}:0".encode("utf-8")
    signature = signing_key.sign_deterministic(
        message,
        hashfunc=hashlib.sha256,
        sigencode=sigencode_string_canonize,
    )
    return public_key, signature.hex() + "01"


def extract_user_id_from_access_token(access_token: str) -> str:
    """refresh 响应缺少 user_id 时，从刚取得的 JWT 中读取。"""
    try:
        payload_part = access_token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part).decode("utf-8"))

        direct = payload.get("userId") or payload.get("user_id")
        if direct:
            return str(direct)

        custom = payload.get("customJson")
        if isinstance(custom, str):
            custom = json.loads(custom)
        if isinstance(custom, dict):
            return str(custom.get("userId") or custom.get("user_id") or "")
    except Exception:
        pass
    return ""


def find_today_entry(
    result: Dict[str, Any],
    day_of_month: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """从签到列表中找到今天的非 miss 记录。"""
    actual_day = datetime.now().day if day_of_month is None else day_of_month
    target = str(actual_day).zfill(2)
    for item in result.get("signInInfos", []) or []:
        if not isinstance(item, dict):
            continue
        date_value = str(item.get("date") or "").zfill(2)
        if date_value == target and item.get("status") != "miss":
            return item
    return None


def find_daily_reward(
    today_entry: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not today_entry:
        return None
    for reward in today_entry.get("rewards", []) or []:
        if isinstance(reward, dict) and reward.get("type") == "dailySignIn":
            return reward
    return None


def reward_status_label(status: str) -> str:
    return {
        "unfinished": "未完成",
        "finished": "待领取",
        "verification": "已领取",
        "notStart": "未开始",
    }.get(status, status or "未知状态")


def reward_summary(today_entry: Optional[Dict[str, Any]]) -> str:
    reward = find_daily_reward(today_entry)
    if not reward:
        return "未找到今日奖励"
    name = str(reward.get("name") or "今日奖励")
    status = reward_status_label(str(reward.get("status") or ""))
    return f"{name}（{status}）"


def _safe_json(response: Any) -> Dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _api_message(data: Dict[str, Any], fallback: str) -> str:
    message = data.get("message") or data.get("code")
    text = str(message)[:200] if message else fallback
    text = re.sub(
        r"(?i)(refresh_token|access_token|client_secret|x-signature-v2)\s*[:=]\s*[^\s,;]+",
        r"\1=[已隐藏]",
        text,
    )
    return text


def _network_error(action: str, error: Exception) -> str:
    """不展开可能携带查询参数或凭证的异常文本。"""
    return f"{action}网络异常（{type(error).__name__}）"


def _format_size(size: Any) -> str:
    try:
        value = int(size or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "0 GB"
    return f"{value / 1024 / 1024 / 1024:.2f} GB"


# 网络请求与业务逻辑

class QinglongOpenAPI:
    """保存青龙环境变量中的新凭证。"""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        session: Optional[Any] = None,
    ):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.session = session or requests.Session()
        if session is None and hasattr(self.session, "trust_env"):
            self.session.trust_env = False

    @staticmethod
    def _response_ok(response: Any, data: Dict[str, Any]) -> bool:
        status_code = int(getattr(response, "status_code", 0) or 0)
        api_code = data.get("code")
        return (
            200 <= status_code < 300
            and (api_code is None or str(api_code) == "200")
        )

    def _get_token(self) -> Tuple[bool, str]:
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
            return False, _network_error("青龙认证", error)

        token = (
            (data.get("data") or {}).get("token")
            if isinstance(data.get("data"), dict)
            else ""
        )
        if not self._response_ok(response, data) or not token:
            return False, (
                f"青龙认证失败：{_api_message(data, '未返回访问令牌')}"
            )
        return True, str(token)

    @staticmethod
    def _extract_env_records(
        data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        records: Any = data.get("data")
        if isinstance(records, dict):
            for key in ("data", "items", "rows", "list"):
                if isinstance(records.get(key), list):
                    records = records[key]
                    break
        if not isinstance(records, list):
            return []
        return [item for item in records if isinstance(item, dict)]

    def update_environment(
        self,
        name: str,
        original_value: str,
        new_value: str,
    ) -> WritebackResult:
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
                params={"searchValue": name},
                headers=headers,
                timeout=LOCAL_REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return WritebackResult(
                True,
                True,
                False,
                _network_error("查询青龙环境变量", error),
            )

        if not self._response_ok(response, data):
            return WritebackResult(
                True,
                True,
                False,
                f"查询青龙环境变量失败："
                f"{_api_message(data, '接口返回异常')}",
            )

        exact_matches = [
            item
            for item in self._extract_env_records(data)
            if item.get("name") == name
        ]
        original_matches = [
            item
            for item in exact_matches
            if item.get("value") == original_value
        ]
        if len(original_matches) != 1:
            return WritebackResult(
                True,
                True,
                False,
                "未能唯一确定 ALIYUN_ACCOUNTS，已拒绝回写",
            )

        target = original_matches[0]
        payload: Dict[str, Any] = {
            "name": name,
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
                _network_error("回写 refresh_token", error),
            )

        if not self._response_ok(response, data):
            return WritebackResult(
                True,
                True,
                False,
                f"refresh_token 回写失败："
                f"{_api_message(data, '接口返回异常')}",
            )
        return WritebackResult(
            True,
            True,
            True,
            "refresh_token 已自动回写",
        )


def write_back_accounts(
    original_value: str,
    new_value: str,
    client_id: str = "",
    client_secret: str = "",
    session: Optional[Any] = None,
) -> WritebackResult:
    """按配置决定是否调用青龙 OpenAPI；无变化时不访问接口。"""
    clean_id = client_id.strip()
    clean_secret = client_secret.strip()

    if original_value == new_value:
        return WritebackResult(
            bool(clean_id or clean_secret),
            False,
            True,
            "refresh_token 无变化",
        )
    if not clean_id and not clean_secret:
        return WritebackResult(
            False,
            False,
            True,
            "未启用 refresh_token 自动回写",
        )
    if not clean_id or not clean_secret:
        return WritebackResult(
            True,
            False,
            False,
            "CLIENT_ID 与 CLIENT_SECRET 必须同时配置",
        )

    api = QinglongOpenAPI(
        clean_id,
        clean_secret,
        session=session,
    )
    return api.update_environment(
        ACCOUNT_ENV_NAME,
        original_value,
        new_value,
    )


class AliyunPan:
    def __init__(
        self,
        refresh_token: str,
        name: str,
        index: int = 1,
        session: Optional[Any] = None,
    ):
        self.refresh_token = refresh_token.strip()
        self.name = name
        self.index = index
        self.session = session or requests.Session()
        self.session.headers.update(COMMON_HEADERS)

        self.access_token = ""
        self.token_type = "Bearer"
        self.user_id = ""
        self.device_id = ""
        self.public_key = ""
        self.x_signature = ""
        self.signature_v2_key = ""
        self.signin_count = 0
        self.today_entry: Optional[Dict[str, Any]] = None

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": (
                f"{self.token_type} {self.access_token}"
            )
        }

    def _signed_headers(self) -> Dict[str, str]:
        headers = self._auth_headers()
        headers["x-device-id"] = self.device_id
        headers["x-signature"] = self.x_signature
        return headers

    def _reward_headers(
        self,
    ) -> Dict[str, Optional[str]]:
        headers: Dict[str, Optional[str]] = dict(
            self._signed_headers()
        )
        headers.update(
            {
                "User-Agent": WINDOWS_REWARD_UA,
                "x-canary": WINDOWS_CANARY,
                "Content-Type": "application/json",
                "Origin": None,
                "Referer": None,
            }
        )
        headers.update(
            build_signature_v2_headers(
                device_id=self.device_id,
                key=self.signature_v2_key,
            )
        )
        return headers

    def refresh_access_token(
        self,
    ) -> Tuple[bool, str]:
        url = "https://auth.aliyundrive.com/v2/account/token"
        payload = {
            "refresh_token": self.refresh_token,
            "api_id": API_ID,
            "grant_type": "refresh_token",
        }
        try:
            response = self.session.post(
                url,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return False, _network_error("登录", error)

        access_token = data.get("access_token")
        if not access_token:
            return False, (
                f"登录失败："
                f"{_api_message(data, '未返回 access_token')}"
            )

        self.access_token = str(access_token)
        self.token_type = str(
            data.get("token_type") or "Bearer"
        )
        self.refresh_token = str(
            data.get("refresh_token") or self.refresh_token
        )
        self.user_id = (
            str(data.get("user_id") or "")
            or extract_user_id_from_access_token(
                self.access_token
            )
        )
        if not self.user_id:
            return False, "登录成功但未取得 user_id"
        return True, "登录成功"

    def query_signin_state(
        self,
    ) -> Tuple[bool, str]:
        url = (
            "https://member.aliyundrive.com"
            "/v2/activity/sign_in_list"
        )
        try:
            response = self.session.post(
                url,
                json={},
                headers=self._auth_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return False, _network_error(
                "查询签到状态",
                error,
            )

        if data.get("success") is not True:
            return False, (
                f"查询签到状态失败："
                f"{_api_message(data, '接口返回异常')}"
            )

        result = data.get("result") or {}
        if not isinstance(result, dict):
            return False, "查询签到状态失败：返回结构无效"
        try:
            self.signin_count = int(
                result.get("signInCount") or 0
            )
        except (TypeError, ValueError):
            self.signin_count = 0
        self.today_entry = find_today_entry(result)
        return True, "查询成功"

    def do_signin(
        self,
    ) -> Tuple[bool, str]:
        url = (
            "https://member.aliyundrive.com"
            "/v1/activity/sign_in_list"
        )
        headers = self._auth_headers()
        headers["User-Agent"] = MOBILE_SIGNIN_UA
        try:
            response = self.session.post(
                url,
                params={"_rx-s": "mobile"},
                json={"isReward": False},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return False, _network_error("签到", error)

        if data.get("success") is not True:
            return False, (
                f"签到失败："
                f"{_api_message(data, '接口返回异常')}"
            )

        result = data.get("result") or {}
        if not isinstance(result, dict):
            return False, "签到失败：返回结构无效"
        try:
            count = int(
                result.get("signInCount") or 0
            )
        except (TypeError, ValueError):
            count = 0
        if count:
            self.signin_count = count
        return True, "签到成功"

    def ensure_signed_today(
        self,
    ) -> Tuple[bool, str]:
        query_ok, query_message = (
            self.query_signin_state()
        )
        if not query_ok:
            return False, query_message
        if self.today_entry:
            return True, "今日已签到"

        signin_ok, signin_message = self.do_signin()
        if not signin_ok:
            return False, signin_message

        query_ok, query_message = (
            self.query_signin_state()
        )
        if not query_ok:
            return False, (
                "签到接口成功，但复查失败："
                f"{query_message}"
            )
        if not self.today_entry:
            return False, (
                "签到接口成功，但未确认到今日签到记录"
            )
        return True, "签到成功"

    def create_ecc_session(
        self,
    ) -> Tuple[bool, str]:
        self.device_id = stable_device_id(self.user_id)
        try:
            (
                self.public_key,
                self.x_signature,
            ) = build_ecc_identity(
                self.user_id,
                self.device_id,
            )
        except Exception as error:
            return False, str(error)

        url = (
            "https://api.aliyundrive.com"
            "/users/v1/users/device/create_session"
        )
        payload = {
            "deviceName": "Qinglong",
            "modelName": "Python",
            "pubKey": self.public_key,
        }
        try:
            response = self.session.post(
                url,
                json=payload,
                headers=self._signed_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return False, _network_error(
                "建立领奖设备会话",
                error,
            )

        if (
            data.get("success") is True
            or data.get("result") is True
        ):
            return True, "设备验证通过"
        return False, (
            f"设备验证失败："
            f"{_api_message(data, '接口返回异常')}"
        )

    def request_reward(
        self,
        sign_day: int,
    ) -> Tuple[bool, str]:
        """请求当天奖励；调用者负责状态门禁和一次调用限制。"""
        url = (
            "https://member.aliyundrive.com"
            "/v1/activity/sign_in_reward"
        )
        try:
            response = self.session.post(
                url,
                json={"signInDay": sign_day},
                headers=self._reward_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return False, _network_error(
                "领取奖励",
                error,
            )

        if data.get("success") is True:
            result = data.get("result") or {}
            notice = str(
                result.get("notice")
                or result.get("name")
                or "奖励接口返回成功"
            )
            sub_notice = str(
                result.get("subNotice") or ""
            )
            return (
                True,
                (
                    f"{notice}｜{sub_notice}"
                    if sub_notice
                    else notice
                ),
            )
        return False, (
            f"领取奖励失败："
            f"{_api_message(data, '接口返回异常')}"
        )

    def claim_today_reward(
        self,
        signature_key: str,
    ) -> Tuple[bool, str]:
        """仅在今日奖励明确待领取时发送一次请求，并以复查状态作为最终依据。"""
        reward = find_daily_reward(self.today_entry)
        if not reward:
            return (
                False,
                "未找到今日 dailySignIn 奖励，未执行领奖",
            )

        reward_name = str(
            reward.get("name") or "今日奖励"
        )
        status = str(reward.get("status") or "")
        if status == "verification":
            return True, (
                f"{reward_name}（已领取）"
            )
        if status != "finished":
            return False, (
                f"{reward_name}（"
                f"{reward_status_label(status)}，"
                "未执行领奖）"
            )

        try:
            self.signature_v2_key = (
                validate_signature_v2_key(
                    signature_key
                )
            )
        except ValueError as error:
            return False, str(error)

        try:
            sign_day = int(
                (self.today_entry or {}).get("day")
                or self.signin_count
                or 0
            )
        except (TypeError, ValueError):
            sign_day = 0
        if sign_day <= 0:
            return False, (
                "无法确定今日 signInDay，未执行领奖"
            )

        session_ok, session_message = (
            self.create_ecc_session()
        )
        if not session_ok:
            return False, session_message

        claim_ok, claim_message = (
            self.request_reward(sign_day)
        )
        if not claim_ok:
            return False, claim_message

        query_ok, query_message = (
            self.query_signin_state()
        )
        if not query_ok:
            return False, (
                "奖励接口已返回，但复查失败："
                f"{query_message}"
            )

        verified_reward = find_daily_reward(
            self.today_entry
        )
        verified_status = str(
            (verified_reward or {}).get("status")
            or ""
        )
        if verified_status != "verification":
            return False, (
                "奖励接口已返回，但服务端状态未确认："
                f"{reward_summary(self.today_entry)}"
            )
        return True, (
            f"{claim_message}（已确认）"
        )

    def get_capacity(
        self,
    ) -> str:
        url = (
            "https://api.aliyundrive.com"
            "/adrive/v1/user/driveCapacityDetails"
        )
        try:
            response = self.session.post(
                url,
                json={},
                headers=self._auth_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            data = _safe_json(response)
        except Exception as error:
            return _network_error(
                "查询空间",
                error,
            )

        if (
            not data
            or "drive_total_size" not in data
        ):
            return "空间查询失败：接口返回异常"
        total = _format_size(
            data.get("drive_total_size")
        )
        used = _format_size(
            data.get("drive_used_size")
        )
        return f"{used} / {total}"

    def run(
        self,
        signature_v2_key: str,
    ) -> AccountResult:
        reward_enabled = bool(signature_v2_key.strip())
        login_ok, login_message = (
            self.refresh_access_token()
        )
        if not login_ok:
            return AccountResult(
                name=self.name,
                ok=False,
                signin_ok=False,
                signin=login_message,
                signin_count=0,
                reward="未执行",
                capacity="未查询",
                reward_enabled=reward_enabled,
            )

        signin_ok, signin_message = (
            self.ensure_signed_today()
        )
        if not signin_ok:
            return AccountResult(
                name=self.name,
                ok=False,
                signin_ok=False,
                signin=signin_message,
                signin_count=self.signin_count,
                reward="未执行",
                capacity=self.get_capacity(),
                reward_enabled=reward_enabled,
            )

        if reward_enabled:
            reward_ok, reward_message = self.claim_today_reward(signature_v2_key)
        else:
            reward_ok, reward_message = True, "未启用自动领奖"
        capacity = self.get_capacity()
        return AccountResult(
            name=self.name,
            ok=signin_ok and reward_ok,
            signin_ok=signin_ok,
            signin=(
                f"{signin_message}（本月累计 "
                f"{self.signin_count} 天）"
            ),
            signin_count=self.signin_count,
            reward=reward_message,
            capacity=capacity,
            reward_enabled=reward_enabled,
        )


# 日志、通知与合集结果

def result_severity(result: AccountResult) -> str:
    if not result.ok:
        return "fail"
    if "失败" in result.capacity or result.capacity == "未查询":
        return "warn"
    return "success"


def format_account_result(index: int, result: AccountResult) -> str:
    severity = result_severity(result)
    icon = {"success": "✅", "warn": "⚠️", "fail": "❌"}[severity]
    return "\n".join(
        [
            f"{icon} 账号{index}：{result.name}",
            f"签到：{result.signin}",
            f"奖励：{result.reward}",
            f"空间：{result.capacity}",
        ]
    )


def _writeback_warning(writeback: WritebackResult, token_changed: bool) -> bool:
    return not writeback.ok or (token_changed and not writeback.attempted)


def merge_status(results: List[AccountResult]) -> str:
    severities = {result_severity(item) for item in results}
    if "fail" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    return "success"


def _total_capacity(capacity: str) -> str:
    return capacity.split(" / ")[-1] if " / " in capacity else capacity


def notification_reward(reward: str) -> str:
    """简化已领取奖励的通知文本。"""
    for suffix in ("（已领取）", "（已确认）"):
        if reward.endswith(suffix):
            return reward.removesuffix(suffix).strip()
    return reward.strip()


def format_notification(
    results: List[AccountResult],
    elapsed: float = 0,
) -> str:
    lines: List[str] = ["📋 任务结果", ""]
    for result in results:
        severity = result_severity(result)
        icon = {"success": "✅", "warn": "⚠️", "fail": "❌"}[severity]
        lines.extend([f"{icon} {result.name}", f"签到：{result.signin}"])
        if result.reward_enabled:
            lines.append(f"奖励：{notification_reward(result.reward)}")
        lines.extend([f"空间：{result.capacity}", ""])
    lines.extend(
        [
            f"⏱️ 耗时：{max(0, int(round(elapsed)))}秒",
            f"🕒 完成：{datetime.now().strftime('%m-%d %H:%M')}",
        ]
    )
    return "\n".join(lines).strip()


def build_daily_details(results: List[AccountResult]) -> List[str]:
    details: List[str] = []
    for result in results:
        prefix = f"{result.name}：" if len(results) > 1 else ""
        if result.signin_ok:
            details.append(f"{prefix}签到成功｜累计{result.signin_count}天")
            if result.reward_enabled:
                details.append(f"奖励：{notification_reward(result.reward)}")
            details.append(f"空间：{_total_capacity(result.capacity)}")
        else:
            details.append(f"{prefix}{result.signin}")
    return details


def emit_daily_result(status: str, details: List[str], daily_child: bool) -> None:
    if not daily_child:
        return
    payload = {"version": 1, "status": status, "details": details}
    print(DAILY_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def system_notify(title: str, content: str, daily_child: bool = False) -> str:
    """调用青龙系统通知；合集子任务模式下由合集统一发送。"""
    if daily_child:
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


def _finish_early(message: str, started_at: float, daily_child: bool) -> int:
    elapsed = time.monotonic() - started_at
    print(f"❌ 任务失败\n{message}")
    emit_daily_result("fail", [message], daily_child)
    notify_message = system_notify(
        "阿里云盘 · 有失败",
        "\n".join(["📋 任务结果", "", "❌ 任务失败", message, "", f"⏱️ 耗时：{max(0, int(round(elapsed)))}秒"]),
        daily_child,
    )
    print(f"通知：{notify_message}")
    print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
    return 1


# 程序入口

def main(
    environ: Optional[Mapping[str, str]] = None,
    client_factory: Optional[Callable[..., Any]] = None,
    writeback_func: Optional[Callable[..., WritebackResult]] = None,
) -> int:
    configure_utf8_output()
    started_at = time.monotonic()
    env = os.environ if environ is None else environ
    daily_child = str(env.get("DAILY_ALL_CHILD", "")).strip() == "1"
    print(f"==== 阿里云盘签到 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    raw_accounts = str(env.get(ACCOUNT_ENV_NAME, "") or "")
    if skip_inactive_service(raw_accounts, str(env.get("DAILY_ALL_DISABLED", "") or ""), daily_child):
        return 0
    accounts = parse_accounts(raw_accounts)
    if not accounts:
        return _finish_early(
            f"未找到 {ACCOUNT_ENV_NAME} 或账号格式无效",
            started_at,
            daily_child,
        )

    signature_key = str(env.get(SIGNATURE_KEY_ENV_NAME, "") or "").strip()
    factory = AliyunPan if client_factory is None else client_factory
    results: List[AccountResult] = []
    print(f"发现 {len(accounts)} 个账号")
    for index, account in enumerate(accounts, start=1):
        client: Optional[Any] = None
        try:
            client = factory(account.refresh_token, account.display_name, index)
            result = client.run(signature_key)
        except Exception as error:
            result = AccountResult(
                name=account.display_name,
                ok=False,
                signin_ok=False,
                signin=f"执行异常（{type(error).__name__}）",
                signin_count=0,
                reward="未执行",
                capacity="未查询",
                reward_enabled=bool(signature_key),
            )
        finally:
            if client is not None and getattr(client, "refresh_token", ""):
                account.refresh_token = client.refresh_token
            session = getattr(client, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        results.append(result)
        print()
        print(format_account_result(index, result))

    token_changed = any(account.token_changed for account in accounts)
    updated_accounts = serialize_accounts(accounts) if token_changed else raw_accounts
    writeback_runner = write_back_accounts if writeback_func is None else writeback_func
    writeback = writeback_runner(
        raw_accounts,
        updated_accounts,
        client_id=str(env.get("CLIENT_ID", "") or "").strip(),
        client_secret=str(env.get("CLIENT_SECRET", "") or "").strip(),
    )
    if token_changed or not writeback.ok:
        print()
        icon = "⚠️" if _writeback_warning(writeback, token_changed) else "✅"
        print(f"{icon} 凭证：{writeback.message}")

    status = merge_status(results)
    elapsed = time.monotonic() - started_at
    emit_daily_result(
        status,
        build_daily_details(results),
        daily_child,
    )
    suffix = {"success": "全部完成", "warn": "有提醒", "fail": "有失败"}[status]
    notify_message = system_notify(
        f"阿里云盘 · {suffix}",
        format_notification(results, elapsed),
        daily_child,
    )
    print(f"通知：{notify_message}")
    print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
