# AnyRouter

[返回总说明](../README.md) · [脚本](../scripts/anyrouter_checkin.py)

## 功能

每日签到，查询可用额度和已用额度。支持多账号，独立运行与合集均按账号分块展示。

## 配置与凭证

依赖：`requests`。基础凭证为 `ANYROUTER_CONFIG`，填写 JSON 对象或数组。

1. 浏览器登录 `https://anyrouter.top/console`，F12 → 网络 → 刷新。
2. 找 `/api/user/self`，复制请求头 Cookie 的完整值到 `cookie`。
3. 将同一请求头 `New-Api-User` 的值填入 `user_id`。
4. `username` 为可选显示备注，未填使用默认账号名。

单账号：

```json
{"username":"主号","user_id":"实际ID","cookie":"session=实际值"}
```

多账号：

```json
[
  {"username":"主号","user_id":"实际ID1","cookie":"完整Cookie1"},
  {"username":"小号","user_id":"实际ID2","cookie":"完整Cookie2"}
]
```

整个 JSON 填入环境变量的值，账号备注放在 `username` 字段。

## 运行与结果

单独运行：`task QingLongScripts/anyrouter_checkin.py now`；运行开关标识：`anyrouter`。

通知按账号展示签到结果、可用额度和已用额度。

注意：请求不校验 TLS 证书，签到网络异常会重试，存在证书验证缺失和重复提交风险。

通知与停用方式见[总说明](../README.md#配置统一通知)。
