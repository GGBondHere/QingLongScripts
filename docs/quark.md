# 夸克网盘

[返回总说明](../README.md) · [脚本](../scripts/quark.py)

## 功能

移动端签到获得空间，查询当天奖励、连续签到进度和总空间。支持多账号；今日已签到按完成处理。

## 配置与凭证

依赖：`requests`。基础凭证为 `QUARK_COOKIE`，填写移动端请求的 `kps`、`sign`、`vcode` 三个参数，或完整请求 URL。

1. 用 Reqable 等抓取夸克 APP 网盘签到页面。
2. 筛选 `drive-m.quark.cn`，选择含有上述三个参数的请求。
3. 复制完整 URL，或将三个参数按模板填写到环境变量。

三参数格式：

```text
kps=实际值; sign=实际值; vcode=实际值#夸克网盘
```

完整 URL 格式：

```text
https://drive-m.quark.cn/实际路径?kps=实际值&sign=实际值&vcode=实际值#夸克网盘
```

多账号每行一个，`#备注` 可选；未填备注使用默认账号名。保留参数原始编码，账号用换行分隔，URL 内的 `&` 仅作为参数分隔符。

## 运行与结果

单独运行：`task QingLongScripts/quark.py now`；运行开关标识：`quark`。

通知展示签到结果、奖励、连签和空间，单账号省略计数。

通知与停用方式见[总说明](../README.md#配置统一通知)。
