# QingLongScripts

[简体中文](README.md) · [English](README.en.md)

按配置选任务，把日常结果收成一条通知。

面向青龙的每日任务脚本集，支持网盘签到、社区签到、账户奖励和成长进度查询。合集统一运行，各服务也可单独执行。

适用环境：**青龙 v2.22.0 · Python 3.10+ · Asia/Shanghai 时区**。

## 功能特点

- 只运行已下载且已配置凭证的服务，未配置的服务静默跳过。
- 合集发送一条通知，详细过程留在日志；今日已完成不算失败。
- 支持多账号、按需启用功能，以及阿里云盘、ISVORO、Akile 的凭证维护。

## 1. 配置依赖、账号和通知

### 安装依赖

在 **依赖管理 → Python3 → 新建依赖** 安装所用脚本需要的包，等待安装成功。订阅不会自动安装依赖。

| 包名 | 需要的脚本 |
| --- | --- |
| `requests` | 除 NodeSeek 外的各服务 |
| `ecdsa` | 阿里云盘自动领奖 |
| `curl_cffi` | NodeSeek |

全部使用时安装以上三个包，清单见 [requirements.txt](requirements.txt)。

### 配置账号

在 **环境变量** 添加所用服务的凭证，获取步骤和填写模板见对应说明。保留 `daily_all.py`，至少选用一个子脚本。

下表按合集执行顺序排列：

| 服务与说明 | 基础凭证 | 功能 |
| --- | --- | --- |
| [百度网盘](docs/baidupan.md) | `BAIDU_COOKIE` | 网页/APP 签到、答题、成长值和升级预测 |
| [夸克网盘](docs/quark.md) | `QUARK_COOKIE` | 签到、空间奖励、连签和容量查询 |
| [阿里云盘](docs/aliyunpan.md) | `ALIYUN_ACCOUNTS` | 签到、领奖、空间查询 |
| [B站](docs/bilibili.md) | `BILIBILI_COOKIE` | 漫画签到、观看、分享、投币和升级预测 |
| [NodeSeek](docs/nodeseek.md) | `NS_COOKIE` | 多账号签到和鸡腿收益 |
| [AnyRouter](docs/anyrouter.md) | `ANYROUTER_CONFIG` | 签到、可用额度和已用额度 |
| [ISVORO](docs/isvoro.md) | `ISVORO_COOKIE` | 签到、积分和连续天数 |
| [AkileCloud](docs/akile.md) | `AKILE_TOKEN` | AK 币签到、余额和登录保活 |

按需追加：

- 百度网盘：`BAIDU_DEVICE_ID` 启用 APP 签到。APP 与答题奖励需在手机成长值页手动领取；广告、连续签到奖励也由用户处理。
- 阿里云盘：`ALIYUN_SIGNATURE_V2_KEY` 配合 `ecdsa` 启用当天自动领奖。

### 配置统一通知

在 **系统设置 → 通知设置** 配置渠道并测试。所有脚本使用青龙内置通知，合集只发送一条汇总，无需额外通知文件。

### 自动回写凭证

阿里云盘、ISVORO、Akile 共用以下配置：

| 环境变量 | 内容 |
| --- | --- |
| `CLIENT_ID` | 青龙 OpenAPI 应用的 Client ID |
| `CLIENT_SECRET` | 同一应用的 Client Secret |

在 **系统设置 → 应用设置** 新建应用，授予 **环境变量** 权限，再添加上述变量。

不配置时，有效凭证仍可运行，但新凭证不会保存，下次执行可能需手动更新。Akile 长期保活需配置回写，并保留早晚两次执行。面板端口不是 5700 时，调整脚本中的 OpenAPI 地址。

## 2. 新建订阅

在 **订阅管理 → 新建 → 公开仓库** 填写：

| 字段 | 推荐值 |
| --- | --- |
| 名称 | `QingLongScripts` |
| 类型 | 公开仓库 |
| 链接 | `https://github.com/GGBondHere/QingLongScripts.git` |
| 分支 | `main` |
| 唯一值 | 保持自动生成 |
| 定时类型 | `crontab` |
| 定时规则 | `0 7 * * *`，每天 07:00 更新订阅 |
| 白名单、黑名单 | 留空，默认拉取全部 |
| 依赖文件 | `daily_all\.py$` |
| 文件后缀 | `py` |
| 执行前 | 留空 |
| 执行后 | 下方命令 |
| 代理 | 按需填写 |
| 自动添加任务 | **关闭** |
| 自动删除任务 | **关闭** |

执行后命令：

```sh
task GGBondHere_QingLongScripts_main/scripts/daily_all.py now -- --organize
```

此命令整理文件并创建缺失任务，不执行签到。请按表格保留 `main` 分支和执行后命令。

运行文件统一放在 **`QingLongScripts/`**，各脚本同级。

### 定时任务初始状态

| 任务 | 类型与时间 | 状态 |
| --- | --- | --- |
| 每日任务合集 | 每天 08:45，`45 8 * * *` | 启用 |
| 普通子任务 | 单次类型，`@once`，仅手动执行 | 启用 |
| Akile 保活 | 每天 22:00，`0 22 * * *` | 启用 |

已有任务保留原时间和启停状态。更新会覆盖脚本文件，请在自己的仓库保存代码修改；不要在任务运行时更新订阅。

运行目录的 `state.json` 自动保存订阅归属和签到统计；更新时请保留，不需要手动填写，不保存 Cookie 或 Token。

只需部分脚本时，可设置白名单 `(^|/)(daily_all|nodeseek)\.py$`（合集和 NodeSeek），或黑名单 `(^|/)Bilibili\.py$`（排除 B站）。依赖文件仍保留 `daily_all\.py$`。

## 3. 首次运行

1. 配置依赖、凭证、通知及所需回写变量。
2. 运行订阅，确认整理成功，并在“定时任务”核对时间与状态。
3. 手动运行“每日任务合集”，检查日志和通知。

合集命令：

```sh
task QingLongScripts/daily_all.py now
```

单脚本命令见各服务说明。`now` 跳过青龙启动随机延迟，站点所需请求间隔仍保留。

## 4. 停用与恢复

清空或禁用该服务的基础凭证即可停用，恢复凭证后下次运行生效。也可保留凭证，设置：

```text
名称：DAILY_ALL_DISABLED
值：quark,anyrouter
```

可填 `baidupan`、`quark`、`aliyunpan`、`bilibili`、`nodeseek`、`anyrouter`、`isvoro`、`akile`，多个用逗号或换行分隔。该开关同时适用于合集和独立脚本。

仅禁用面板中的独立任务，不会阻止合集调用该服务。

完全移除时，先停用本仓库任务，再删除订阅、`QingLongScripts/` 运行目录及不再使用的环境变量。删除订阅不会自动清理整理后的运行目录。

## 通知与排错

通知用 ✅ 完成、⚠️ 提醒、❌ 失败标记服务结果。单项失败不阻止其他服务；已签到或待手动领奖的百度任务按完成处理。

安装问题看依赖日志，订阅问题看订阅日志，任务问题看对应子脚本日志。反馈时只提供脱敏后的关键内容。

## 仓库布局

```text
QingLongScripts/
├── README.md           # 中文说明
├── README.en.md        # English guide
├── LICENSE
├── requirements.txt
├── scripts/           # 合集和八个独立服务脚本
└── docs/              # 各服务配置与凭证获取
```

测试、抓包、笔记和私有配置由 [.gitignore](.gitignore) 排除，不参与发布。

## 贡献指南

反馈请附服务名称、青龙版本、独立/合集运行方式及脱敏日志，不要提交 Cookie、Token、密钥或原始 HAR。

提交 PR 时，请同步配置说明和合集注册，保持单文件运行、静默跳过与统一通知，并测试首次运行、重复运行、缺配置和网络异常。

## 免责声明

仅用于本人或明确授权的账号，请遵守平台条款及适用法律。本项目非官方产品，接口变化可能影响运行；涉及投币等资源消耗时请确认配置。本项目不提供验证码或风控绕过。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方代码的原有版权声明及许可条件仍适用。
