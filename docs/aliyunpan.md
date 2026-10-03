# 阿里云盘

[返回总说明](../README.md) · [脚本](../scripts/aliyunpan.py)

## 功能

每日签到、查询空间、刷新登录凭证；配置领奖密钥后自动领取当天奖励。支持多账号和备注。

## 配置与凭证

依赖：`requests`；启用自动领奖另需 `ecdsa`。

| 环境变量 | 用途 |
| --- | --- |
| `ALIYUN_ACCOUNTS` | 基础凭证，填写 `refresh_token#备注`；启用签到及空间查询 |
| `ALIYUN_SIGNATURE_V2_KEY` | 可选，启用自动领奖；本人 Windows 客户端对应的 40 位小写十六进制 HMAC 密钥 |
| `CLIENT_ID`、`CLIENT_SECRET` | 青龙 OpenAPI 回写配置，两项一起配置并授予环境变量权限 |

浏览器登录阿里云盘，F12 → 网络，复制登录刷新请求 JSON 中的 `refresh_token`。多账号换行或用 `&` 分隔，备注可选：

```text
实际refresh_token#主号
另一个实际refresh_token#小号
```

自动领奖需自行准备 Windows 客户端的 Signature V2 密钥，将原始密钥填入 `ALIYUN_SIGNATURE_V2_KEY`。保持该变量为空即可只签到和查空间。

配置[凭证回写](../README.md#自动回写凭证)，保存更新后的 refresh_token。

## 运行与结果

单独运行：`task QingLongScripts/aliyunpan.py now`；运行开关标识：`aliyunpan`。

通知展示签到、累计天数和空间，启用自动领奖后增加奖励。

通知与停用方式见[总说明](../README.md#配置统一通知)。
