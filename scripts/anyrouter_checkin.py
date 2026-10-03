#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
名称：AnyRouter 签到
用途：每日签到，查询本次奖励、可用额度和已用额度。

青龙配置：
- 执行：task QingLongScripts/anyrouter_checkin.py now。
- 独立任务：启用、单次类型；日常由 daily_all.py 执行。
- 依赖：requests。
- 通知：在青龙面板配置，合集只发送汇总通知。

环境变量：
- ANYROUTER_CONFIG：单账号 JSON 对象，多账号 JSON 数组。
- JSON 字段：user_id、cookie 必填；username 是可选备注。

凭证获取：
1. 登录 https://anyrouter.top/console，F12 → 网络，刷新并选择 /api/user/self 请求。
2. 请求头 Cookie 的完整值填入 cookie，New-Api-User 的值填入 user_id。

填写示例：
- {"username":"主号","user_id":"实际ID","cookie":"session=实际值"}

使用说明：
- 当前请求不校验 TLS 证书；签到网络重试可能重复提交。
- 停用标识：anyrouter；缺凭证或已停用时跳过。详见 docs/anyrouter.md。
"""

from __future__ import annotations

import os
import re
import time
import json
import sys
from dataclasses import dataclass
from datetime import datetime

# 运行配置

def skip_inactive_service(raw: str, disabled_value: str, daily_child: bool) -> bool:
    """未配置凭证或已停用时跳过。"""
    disabled = {item.strip().casefold() for item in re.split(r"[,;|&\n]+", disabled_value)}
    if raw.strip() and not disabled.intersection({"anyrouter", "anyrouter_checkin"}):
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
    os.getenv("ANYROUTER_CONFIG", ""), os.getenv("DAILY_ALL_DISABLED", ""),
    os.getenv("DAILY_ALL_CHILD", "").strip() == "1",
):
    raise SystemExit(0)


import requests

# 配置与签到请求

CONFIG_FILE = 'config.json'
CONFIG_KEY = 'anyrouter'
ENV_CONFIG = 'ANYROUTER_CONFIG'

DOMAIN = 'anyrouter.top'
SIGN_IN_URL = f'https://{DOMAIN}/api/user/sign_in'
USER_INFO_URL = f'https://{DOMAIN}/api/user/self'

requests.packages.urllib3.disable_warnings()

HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'origin': f'https://{DOMAIN}',
    'referer': f'https://{DOMAIN}/console',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}

def unsbox(arg1):
    box = [0xf,0x23,0x1d,0x18,0x21,0x10,0x1,0x26,0xa,0x9,0x13,0x1f,0x28,0x1b,0x16,0x17,0x19,0xd,0x6,0xb,0x27,0x12,0x14,0x8,0xe,0x15,0x20,0x1a,0x2,0x1e,0x7,0x4,0x11,0x5,0x3,0x1c,0x22,0x25,0xc,0x24]
    unsboxed = [''] * 40
    for i in range(40):
        unsboxed[i] = arg1[box[i] - 1]
    return ''.join(unsboxed)

def hex_xor(s1, mask):
    res = ''
    for i in range(len(s1)):
        if i % 2 != 0:
            continue
        v1 = int(s1[i:i + 2], 16)
        v2 = int(mask[i:i + 2], 16)
        x = hex(v1 ^ v2)[2:]
        if len(x) == 1:
            x = '0' + x
        res += x
    return res

def get_acw_sc_v2(arg1):
    return hex_xor(unsbox(arg1), '3000176000856006061501533003690027800375')

def fetch_with_retry(url, method, headers):
    for i in range(3):
        try:
            if method == 'POST':
                return requests.post(url, headers=headers, timeout=15, verify=False)
            return requests.get(url, headers=headers, timeout=15, verify=False)
        except Exception as e:
            if i < 2:
                print(f'   ⚠️ 网络异常重试中... ({str(e)[:30]})')
                time.sleep(2)
            else:
                raise Exception(f'请求失败: {str(e)[:50]}')

def auto_bypass_waf_request(url, account, method='GET'):
    temp_headers = HEADERS.copy()
    temp_headers['new-api-user'] = account['user_id']
    temp_headers['cookie'] = account['cookie']
    res = fetch_with_retry(url, method, temp_headers)
    if 'arg1=' in res.text:
        print('   🛡️ 遭遇阿里云 WAF 拦截, 正在启动底层算法解盾...')
        match = re.search(r"arg1='([0-9A-F]+)'", res.text)
        if not match:
            raise Exception('未能从 WAF 拦截页面提取到 arg1!')
        acw = get_acw_sc_v2(match.group(1))
        cookie = account['cookie']
        if not cookie.endswith(';'):
            cookie += ';'
        cookie += f' acw_sc__v2={acw}'
        temp_headers['cookie'] = cookie
        print('   ✅ 解盾算法执行完毕，已获取通行凭证')
        return fetch_with_retry(url, method, temp_headers)
    return res

def load_env_config():
    raw = os.getenv(ENV_CONFIG, '').strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get(CONFIG_KEY), list):
            return data.get(CONFIG_KEY, [])
        return [data]
    raise Exception(f'环境变量 {ENV_CONFIG} 格式错误，只支持 JSON 对象或数组')

def load_file_config():
    script_path = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(script_path, CONFIG_FILE)
    if not os.path.exists(file_path):
        return []
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f).get(CONFIG_KEY, [])

def load_config():
    try:
        env_users = load_env_config()
        if env_users:
            print(f'已从环境变量 {ENV_CONFIG} 加载 {len(env_users)} 个账号')
            return env_users
    except Exception as e:
        raise Exception(f'读取环境变量失败: {e}')
    file_users = load_file_config()
    if file_users:
        print(f'已从 {CONFIG_FILE} 加载 {len(file_users)} 个账号')
        return file_users
    return []

def process_account(user, index):
    username = user.get('username', '')
    user_id = str(user.get('user_id', '')).strip()
    cookie = user.get('cookie', '').strip()
    display_name = f'{username}(UID:{user_id})' if username else f'账号{index}'
    print(f'\n====== 正在处理: {display_name} ======')
    log_msg = f'【AnyRouter - {display_name}】'
    if not user_id or not cookie:
        return log_msg + '\n❌ 配置缺失'
    account = {'user_id': user_id, 'cookie': cookie}
    try:
        print('   -> 正在查询签到前余额...')
        pre_res = auto_bypass_waf_request(USER_INFO_URL, account, method='GET')
        pre_balance = 0.0
        try:
            if pre_res.json().get('success'):
                pre_balance = round(pre_res.json().get('data', {}).get('quota', 0) / 500000, 4)
        except Exception:
            pass
        print('   -> 发起签到请求...')
        sign_res = auto_bypass_waf_request(SIGN_IN_URL, account, method='POST')
        sign_success = False
        msg = ''
        try:
            sign_data = sign_res.json()
            msg = sign_data.get('message', '')
            if sign_data.get('success'):
                sign_success = True
        except Exception:
            pass
        print('   -> 正在查询签到后余额...')
        post_res = auto_bypass_waf_request(USER_INFO_URL, account, method='GET')
        post_balance = 0.0
        used = 0.0
        try:
            if post_res.json().get('success'):
                data = post_res.json().get('data', {}) or {}
                post_balance = round(data.get('quota', 0) / 500000, 4)
                used = round(data.get('used_quota', 0) / 500000, 4)
        except Exception:
            pass
        reward = round(post_balance - pre_balance, 4)
        if sign_success:
            if reward > 0:
                print(f'   🎉 签到成功! 获得奖励: ${reward}')
                log_msg += f'\n🎉 签到成功! (获得 ${reward})'
            else:
                print('   ✅ 签到操作成功，但余额未增加 (奖励0或已满额)')
                log_msg += '\n✅ 签到成功! (但余额未增加)'
        else:
            print(f"   ℹ️ {msg if msg else '今日已签到'}")
            log_msg += f"\n✅ {msg if msg else '今日已签到'}"
        print(f'   💰 可用余额: ${post_balance}')
        log_msg += f'\n💰 可用余额: ${post_balance}'
        log_msg += f'\n📊 已用额度: ${used}'
    except Exception as e:
        print(f'   ❌ 报错: {str(e)}')
        log_msg += f'\n❌ 运行报错: {str(e)}'
    print(log_msg)
    return log_msg

# 日志、通知与合集结果

DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="


@dataclass
class AccountSummary:
    name: str
    status: str
    details: list[str]


def configure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, TypeError, ValueError):
                pass


def redact_summary(text: str, user: dict) -> str:
    cookie = str(user.get("cookie") or "").strip()
    if cookie:
        text = text.replace(cookie, "[凭证已隐藏]")
        for part in cookie.split(";"):
            _name, separator, value = part.strip().partition("=")
            if separator and len(value) >= 8:
                text = text.replace(value, "[已隐藏]")
    return re.sub(r"\s+", " ", text).strip()[:240]


def summarize_account(content: str, user: dict, index: int) -> AccountSummary:
    """脱敏结果文本，保留业务状态和账号分块格式。"""
    name = redact_summary(str(user.get("username") or f"账号{index}"), user)
    details: list[str] = []
    failed = False
    succeeded = False
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if line.startswith("❌"):
            failed = True
        elif line.startswith(("✅", "🎉")):
            succeeded = True
        elif not line.startswith(("💰 可用余额:", "📊 已用额度:")):
            continue
        details.append(redact_summary(line, user))
    if not (failed or succeeded):
        details.insert(0, "⚠️ 原脚本未返回明确签到结果，请查看日志")
    status = "fail" if failed else "success" if succeeded else "warn"
    return AccountSummary(name, status, details)


def overall_status(results: list[AccountSummary]) -> str:
    statuses = {result.status for result in results}
    if "fail" in statuses:
        return "fail"
    return "warn" if "warn" in statuses else "success"


def build_daily_details(results: list[AccountSummary]) -> list[str]:
    """通过多行文本传递账号分块通知。"""
    return [format_notification(results)]


def format_notification(results: list[AccountSummary]) -> str:
    """按账号分块生成通知。"""
    blocks: list[str] = []
    icons = {"success": "✅", "warn": "⚠️", "fail": "❌"}
    for result in results:
        lines = list(result.details)
        if lines and not lines[0].startswith(("✅", "🎉", "❌", "⚠️")):
            lines[0] = f"{icons[result.status]} {lines[0]}"
        blocks.append("\n".join([f"【AnyRouter - {result.name}】", *lines]))
    return "\n\n".join(blocks)


def system_notify(title: str, content: str, daily_child: bool) -> str:
    if daily_child:
        return "由合集统一发送"
    try:
        QLAPI.systemNotify({"title": title, "content": content})
        return "已发送"
    except NameError:
        return "当前环境没有 QLAPI，已跳过"
    except Exception as error:
        return f"发送失败（{type(error).__name__}）"


def finish(results: list[AccountSummary], started: float, daily_child: bool) -> int:
    elapsed = time.monotonic() - started
    status = overall_status(results)
    if daily_child:
        payload = {"version": 1, "status": status, "details": build_daily_details(results)}
        print(DAILY_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    notice = system_notify("AnyRouter 每日签到", format_notification(results), daily_child)
    print(f"通知：{notice}")
    print(f"==== 完成 | 耗时 {max(0, int(round(elapsed)))}秒 ====")
    return 1 if status == "fail" else 0


# 程序入口

def main() -> int:
    configure_utf8_output()
    started = time.monotonic()
    daily_child = os.getenv("DAILY_ALL_CHILD", "").strip() == "1"
    print(f"==== AnyRouter 签到 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    raw = os.getenv(ENV_CONFIG, "")
    if skip_inactive_service(raw, os.getenv("DAILY_ALL_DISABLED", ""), daily_child):
        return 0
    try:
        user_list = load_config()
    except Exception as error:
        message = f"配置读取失败（{type(error).__name__}），请检查 {ENV_CONFIG} 的 JSON 格式"
        print(f"❌ {message}")
        return finish([AccountSummary("配置", "fail", [message])], started, daily_child)
    if not user_list:
        message = f"未找到可用配置：请设置环境变量 {ENV_CONFIG}"
        print(f"❌ {message}")
        return finish([AccountSummary("配置", "fail", [message])], started, daily_child)
    results: list[AccountSummary] = []
    for index, user in enumerate(user_list, start=1):
        try:
            result_content = process_account(user, index)
            results.append(summarize_account(result_content, user, index))
        except Exception as error:
            message = f"账号配置或执行异常（{type(error).__name__}），请查看配置和日志"
            print(f"❌ 账号{index}：{message}")
            results.append(AccountSummary(f"账号{index}", "fail", [message]))
        time.sleep(2)
    return finish(results, started, daily_child)


if __name__ == "__main__":
    raise SystemExit(main())
