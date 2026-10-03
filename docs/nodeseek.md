# NodeSeek

[返回总说明](../README.md) · [脚本](../scripts/nodeseek.py)

## 功能

每日签到与鸡腿收益查询，支持多账号顺序执行。

## 配置与凭证

依赖：`curl_cffi`。基础凭证为 `NS_COOKIE`。

浏览器登录 `https://www.nodeseek.com`，F12 → 网络 → 刷新，复制本站请求头完整 Cookie 的值。每行一个账号，`#备注` 可选：

```text
完整Cookie1#大号
完整Cookie2#小号
```

| 可选环境变量 | 默认及含义 |
| --- | --- |
| `NS_RANDOM` | `true`，站点的随机奖励策略；可填 `true/false` |
| `NS_IMPERSONATE` | `chrome110`，请求使用的浏览器指纹 |

`NS_RANDOM` 控制签到奖励，不是延迟设置。

## 运行与结果

单独运行：`task QingLongScripts/nodeseek.py now`；运行开关标识：`nodeseek`。

通知按账号展示签到结果和鸡腿收益，单账号省略计数。

通知与停用方式见[总说明](../README.md#配置统一通知)。
