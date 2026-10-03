# QingLongScripts

[简体中文](README.md) · [English](README.en.md)

Run the services you configure. Receive one daily report.

Daily-task scripts for Qinglong, covering cloud drives, communities, account rewards and growth progress. Run the collection or any service independently.

Target environment: **Qinglong v2.22.0 · Python 3.10+ · Asia/Shanghai timezone**.

## Features

- Run only downloaded services with configured credentials; skip unconfigured services silently.
- Receive one collection report, with detailed execution logs kept separately. Already-completed tasks count as success.
- Support multiple accounts, optional features and credential maintenance for Aliyun, ISVORO and Akile.

## 1. Configure packages, accounts and notifications

### Install packages

In **Dependency Management → Python3**, install the packages required by your selected services. Subscriptions do not install packages automatically.

| Package | Used by |
| --- | --- |
| `requests` | All services except NodeSeek |
| `ecdsa` | Aliyun automatic reward claims |
| `curl_cffi` | NodeSeek |

For all services, install these three packages. See [requirements.txt](requirements.txt).

### Configure accounts

Add the credentials in **Environment Variables**. Keep `daily_all.py` and select at least one service file. Guides include acquisition steps and templates; they are currently in Chinese.

The table follows collection execution order:

| Service and guide | Base credential | Features |
| --- | --- | --- |
| [Baidu Netdisk](docs/baidupan.md) | `BAIDU_COOKIE` | Web/APP sign-in, quiz, growth and level estimates |
| [Quark Drive](docs/quark.md) | `QUARK_COOKIE` | Sign-in, storage rewards, streak and capacity |
| [Aliyun Drive](docs/aliyunpan.md) | `ALIYUN_ACCOUNTS` | Sign-in, reward claims and capacity |
| [Bilibili](docs/bilibili.md) | `BILIBILI_COOKIE` | Manga sign-in, watch/share tasks, coins and level estimates |
| [NodeSeek](docs/nodeseek.md) | `NS_COOKIE` | Multi-account sign-in and rewards |
| [AnyRouter](docs/anyrouter.md) | `ANYROUTER_CONFIG` | Sign-in, available and used quota |
| [ISVORO](docs/isvoro.md) | `ISVORO_COOKIE` | Sign-in, points and streak |
| [AkileCloud](docs/akile.md) | `AKILE_TOKEN` | AK coin sign-in, streak, total sign-in earnings and login keepalive |

Optional features:

- **Baidu:** add `BAIDU_DEVICE_ID` for APP sign-in. Claim APP and quiz rewards manually on the mobile growth page. Ads and milestone rewards are also handled manually.
- **Aliyun:** add `ALIYUN_SIGNATURE_V2_KEY` and install `ecdsa` for daily automatic reward claims.

### Unified notifications

Configure and test a channel in **System Settings → Notification Settings**. All scripts use Qinglong's built-in notifications; the collection sends one summary. No additional notification files are needed.

### Save rotated credentials

Aliyun, ISVORO and Akile share these settings:

| Variable | Value |
| --- | --- |
| `CLIENT_ID` | Qinglong OpenAPI application's Client ID |
| `CLIENT_SECRET` | The same application's Client Secret |

Create an application in **System Settings → Application Settings**, grant **Environment Variables** permission, and add both variables.

Without these settings, valid credentials can run, but rotated values are not saved and may need manual updates before the next run. Akile keepalive requires saving credentials and retaining both morning and night runs. If the panel port is not 5700, adjust the OpenAPI address in the scripts.

## 2. Create a subscription

In **Subscription Management**, create a public-repository subscription:

| Field | Recommended value |
| --- | --- |
| Name | `QingLongScripts` |
| Type | Public repository |
| Repository | `https://github.com/GGBondHere/QingLongScripts.git` |
| Branch | `main` |
| Unique value | Keep the generated value |
| Schedule type | `crontab` |
| Update schedule | `0 7 * * *` — 07:00 daily |
| Allowlist, blocklist | Leave blank to pull all scripts |
| Dependency files | `daily_all\.py$` |
| File suffix | `py` |
| Before command | Leave blank |
| After command | The command below |
| Proxy | Configure if needed |
| Auto-add tasks | **Off** |
| Auto-delete tasks | **Off** |

After command:

```sh
task GGBondHere_QingLongScripts_main/scripts/daily_all.py now -- --organize
```

This arranges files and creates missing tasks without signing in. Keep the `main` branch and after command as shown.

Runtime scripts share one flat **`QingLongScripts/`** folder.

### Initial task states

| Task | Type and schedule | State |
| --- | --- | --- |
| Daily collection | `45 8 * * *` — 08:45 daily | Enabled |
| Ordinary services | `@once` — manual only | Enabled |
| Akile keepalive | `0 22 * * *` — 22:00 daily | Enabled |

Existing schedules and enabled states are preserved. Updates overwrite script files; keep code customizations in your own repository. Avoid subscription updates while tasks are running.

Keep the automatically generated `state.json` in the runtime folder when updating scripts. It stores subscription ownership and sign-in statistics, needs no manual configuration, and contains no Cookie or Token.

Optional filters: allowlist `(^|/)(daily_all|nodeseek)\.py$` for the collection and NodeSeek, or blocklist `(^|/)Bilibili\.py$` to exclude Bilibili. Keep `daily_all\.py$` in Dependency files.

## 3. First run

1. Configure packages, credentials, notifications and any required credential-saving variables.
2. Run the subscription, confirm successful arrangement, and check task schedules and states.
3. Run the daily collection manually and check its logs and report.

Collection command:

```sh
task QingLongScripts/daily_all.py now
```

Individual commands are in the service guides. `now` skips Qinglong's startup random delay; service-specific request intervals remain.

## 4. Pause or resume

Clear or disable a service's base credential to pause it. Restoring it takes effect on the next run. Alternatively, retain credentials and set:

```text
DAILY_ALL_DISABLED = quark,anyrouter
```

Supported keys: `baidupan`, `quark`, `aliyunpan`, `bilibili`, `nodeseek`, `anyrouter`, `isvoro`, `akile`. Separate keys with commas or newlines. This applies to both collection and independent runs.

Disabling only an independent panel task does not stop the collection from calling that service.

To remove the project, first stop its tasks, then delete the subscription, the `QingLongScripts/` runtime folder and unused environment variables. Deleting the subscription does not automatically remove the arranged runtime folder.

## Reports and troubleshooting

Reports use ✅ for completed services, ⚠️ for warnings and ❌ for failures. One failure does not block other services. Baidu tasks awaiting manual reward claims count as completed.

Check dependency logs for installation, subscription logs for updates, and service logs for task errors. Share only sanitized relevant details.

## Repository layout

```text
QingLongScripts/
├── README.md           # Chinese guide
├── README.en.md        # English guide
├── LICENSE
├── requirements.txt
├── scripts/           # Collection and eight standalone services
└── docs/              # Service configuration and credential guides
```

[.gitignore](.gitignore) excludes local tests, captures, notes and private configuration from publication.

## Contributing

Include the service, Qinglong version, independent/collection execution mode and sanitized logs in issue reports. Do not submit cookies, tokens, keys or raw HAR files.

For pull requests, update configuration guides and collection registration. Preserve standalone execution, silent skipping and unified notifications. Test first runs, repeat runs, missing configuration and network failures.

## Disclaimer

Use only your own accounts or accounts you are authorized to operate, and comply with platform terms and applicable law. This is not an official product. API changes may affect execution; review settings that spend coins or other resources. This project does not bypass verification or risk controls.

## License

This project is licensed under the [MIT License](LICENSE). Existing third-party copyright notices and license terms still apply.
