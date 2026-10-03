# ISVORO

[返回总说明](../README.md) · [脚本](../scripts/isvoro_checkin.py)

## 功能

每日签到，查询连续天数、签到奖励、积分余额和累计积分；自动刷新并回写登录 Cookie。当前单账号。

## 配置与凭证

依赖：`requests`。基础凭证为 `ISVORO_COOKIE`。

1. 浏览器登录 `https://isvoro.com/checkin`，F12 → 网络/Network，刷新页面并选择 `/api/v1/auth/me` 请求。
2. 复制请求头 `Cookie` 的完整值，填入 `ISVORO_COOKIE`。

Cookie 应包含以下三个字段：

```text
pmt_refresh=实际值; pmt_access=实际值; pmt_csrf=实际值
```

直接填写 Cookie，不加 `#备注`。配置[凭证回写](../README.md#自动回写凭证)，保存更新后的 Cookie。

## 运行与结果

单独运行：`task QingLongScripts/isvoro_checkin.py now`；运行开关标识：`isvoro`。

通知展示连续天数、余额和累计积分，新签到时增加本次奖励。

首次和重复运行都发送结果通知；合集只发送汇总通知。

通知与停用方式见[总说明](../README.md#配置统一通知)。
