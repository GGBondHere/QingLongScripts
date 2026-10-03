# AkileCloud

[返回总说明](../README.md) · [脚本](../scripts/akile_checkin.py)

## 功能

AK 币签到、连续天数与累计签到所得统计、登录保活，支持单账号。

## 配置与凭证

依赖：`requests`。

| 环境变量 | 用途 |
| --- | --- |
| `AKILE_TOKEN` | 基础凭证，网站本地存储中的 `akile-token` 原始值 |
| `CLIENT_ID`、`CLIENT_SECRET` | 青龙 OpenAPI 回写配置，两项一起配置并授予环境变量权限 |

浏览器登录 `https://akile.ai/console/ak-coin-shop`，F12 → 应用/Application → 本地存储/Local Storage → `https://akile.ai`，复制 `akile-token` 的值：

```text
eyJ...完整实际JWT...
```

直接填写 Token，不加 `Bearer` 或 `#备注`。同时配置[凭证回写](../README.md#自动回写凭证)。

## 运行与结果

单独运行：`task QingLongScripts/akile_checkin.py now`；运行开关标识：`akile`。

只读检查：`task QingLongScripts/akile_checkin.py now -- --check-only`，仅验证凭证和查询状态，不签到、刷新、回写或通知。

保留 08:45 合集和 22:00 保活任务，时间见[总说明](../README.md#定时任务初始状态)。

签到前查询今日签到流水，已有记录不重复提交。通知展示连续天数、余额和累计签到所得，新签到时增加本次奖励；余额取签到流水中的签到后余额。

单独运行只输出日志，不发送通知，包括首次签到、重复运行、警告和失败；只有合集调用时由合集统一发送通知。

统计自动保存在脚本同目录的 `state.json`，与订阅记录共用，更新脚本时保留即可。首次运行或统计丢失时补查历史，之后增量更新；累计只计签到所得。

通知与停用方式见[总说明](../README.md#配置统一通知)。
