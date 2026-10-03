# B站每日任务

[返回总说明](../README.md) · [脚本](../scripts/Bilibili.py)

## 功能

漫画签到、观看、分享、每日投币，查询等级、经验、硬币和升级预计天数。当前单账号，默认每日投 1 枚硬币，投币后的余额至少保留 100 枚。

## 配置与凭证

依赖：`requests`。基础凭证为 `BILIBILI_COOKIE`。

浏览器登录 `https://www.bilibili.com/`，F12 → 网络 → 刷新，复制本站请求头完整 Cookie 的值。需包含 `SESSDATA`、`bili_jct`，直接填写 Cookie，不加 `#备注`。

```text
SESSDATA=实际值; bili_jct=实际值; DedeUserID=实际值; 其他Cookie字段...
```

| 可选环境变量 | 默认及含义 |
| --- | --- |
| `BILIBILI_COIN_NUM` | `1`，每日投币目标，范围 0–5；0 关闭投币 |
| `BILIBILI_COIN_RESERVE` | `100`，投币后最低余额 |
| `BILIBILI_COIN_TYPE` | `1` 优先关注 UP；`2` 热门视频 |
| `BILIBILI_SILVER2COIN` | `false`；`true` 启用银瓜子兑换硬币 |

使用默认配置时无需添加可选变量。投币消耗账号硬币；余额不足或达到保护线时跳过投币并提示。

## 运行与结果

单独运行：`task QingLongScripts/Bilibili.py now`；运行开关标识：`bilibili`。

通知展示任务结果、经验和升级预计天数。

通知与停用方式见[总说明](../README.md#配置统一通知)。
