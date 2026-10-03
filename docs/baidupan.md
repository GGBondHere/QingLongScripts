# 百度网盘

[返回总说明](../README.md) · [脚本](../scripts/baidupan.py)

## 功能

网页签到、每日答题、APP 签到，查询会员等级、成长值和升级进度。升级天数按昨日全部成长值增量估算，包含手动任务。

各项任务分别展示成长值。APP 与答题奖励需在手机成长值页手动领取；广告与连续签到奖励也由用户处理。

## 配置与凭证

依赖：`requests`。

| 环境变量 | 用途 |
| --- | --- |
| `BAIDU_COOKIE` | 基础凭证，启用网页签到、答题和成长值查询 |
| `BAIDU_DEVICE_ID` | 可选，启用对应账号的 APP 签到及状态查询 |

1. 浏览器登录 `https://pan.baidu.com/`，F12 → 网络 → 刷新，复制本站请求头 Cookie 的值到 `BAIDU_COOKIE`。
2. 需要 APP 签到时，用 Reqable 等抓取手机“成长值任务”页面，找到 `/coins/taskcenter/signinlist` 请求，将查询参数 `cuid` 或 `devuid` 填入 `BAIDU_DEVICE_ID`。

Cookie 不加 `#备注`。多账号每行一个，设备标识按 Cookie 的账号顺序对应；某个账号只用网页功能时，该账号的设备行留空。变量分别填写，例如：

```text
BAIDU_COOKIE 的值：
完整Cookie1
完整Cookie2

BAIDU_DEVICE_ID 的值：
设备值1
设备值2
```

## 运行与结果

单独运行：`task QingLongScripts/baidupan.py now`；运行开关标识：`baidupan`。

仅配置 Cookie 时运行网页任务，追加设备标识后运行 APP 签到。通知展示各项任务的成长值和升级进度，待手动领奖按完成处理。

通知与停用方式见[总说明](../README.md#配置统一通知)。
