#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 每日任务合集
# cron: 45 8 * * *
"""
名称：每日任务合集
用途：顺序运行已配置的同级子脚本，发送一条汇总通知。

青龙配置：
- 执行：task QingLongScripts/daily_all.py now。
- 定时：45 8 * * *，每天 08:45；面板时区 Asia/Shanghai。
- 依赖：仅标准库。通知在青龙面板统一配置。

环境变量：
- DAILY_ALL_DISABLED（可选）：关闭的服务标识，多个用逗号或换行分隔。
- 执行顺序及标识：baidupan、quark、aliyunpan、bilibili、nodeseek、anyrouter、isvoro、akile。

订阅部署：
- 执行后：task GGBondHere_QingLongScripts_main/scripts/daily_all.py now -- --organize。
- 文件整理到 QingLongScripts/，同时创建缺失任务；已有定时和启停状态保留。
- 同目录 state.json 保存订阅归属及签到统计，更新时请保留。
- 仅补建任务：task QingLongScripts/daily_all.py now -- --setup-crons。

使用说明：
- 文件缺失、凭证为空或服务已停用时跳过；全部跳过时不发送通知。
- 普通子任务为单次类型；Akile 另有每天 22:00 保活任务。
- 订阅设置、依赖和账号配置见 README.md。
"""

from __future__ import annotations

import json
import argparse
import builtins
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


# 配置与数据模型

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
DAILY_RESULT_PREFIX = "__DAILY_ALL_RESULT__="
VALID_RESULT_STATUSES = {"success", "warn", "fail", "skip"}
DISABLED_ENV_NAME = "DAILY_ALL_DISABLED"


@dataclass
class ChildRun:
    returncode: int
    output: str
    duration: float
    timed_out: bool = False


@dataclass(frozen=True)
class TaskSpec:
    name: str
    candidates: tuple[str, ...]
    timeout: int = 900
    key: str = ""
    required_env: tuple[str, ...] = ()


@dataclass
class TaskResult:
    name: str
    status: str
    details: list[str]
    returncode: int
    duration: float
    output: str
    path: Path


def configure_utf8_output() -> None:
    """确保 Windows 本地终端与青龙日志统一使用 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, TypeError, ValueError):
            pass


class StateStore:
    """共用 state.json；分区更新在跨进程锁内重读并原子保存。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if self.path.is_symlink():
            raise ValueError("state.json 不能是符号链接")
        if not self.path.exists():
            return {"version": 1}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError("state.json 格式损坏，已保留原文件") from None
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("state.json 版本或结构无效，已保留原文件")
        return data

    def read(self, namespace: str) -> dict:
        section = self._load().get(namespace, {})
        if not isinstance(section, dict):
            raise ValueError(f"state.json 的 {namespace} 分区无效")
        return section

    @contextmanager
    def locked(self, scope: str = "state", timeout: float = 10):
        # 锁放在系统临时目录且不删除，避免等待进程锁住不同 inode；进程退出自动解锁。
        identity = os.path.normcase(str(self.path.resolve())) + ":" + scope
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        lock_path = Path(tempfile.gettempdir()) / ("qinglong-" + digest + ".lock")
        with lock_path.open("a+b") as file:
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            if os.name == "nt":
                import msvcrt
                def acquire():
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    acquire()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("状态正在被其他进程使用，请稍后运行") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                release()

    def update(self, namespace: str, updater) -> dict:
        with self.locked():
            data = self._load()
            section = data.get(namespace, {})
            if not isinstance(section, dict):
                raise ValueError(f"state.json 的 {namespace} 分区无效")
            updated = updater(section)
            if not isinstance(updated, dict):
                raise ValueError("状态更新必须返回对象")
            if updated == section and namespace in data:
                return updated
            data[namespace] = updated
            content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".state-", suffix=".tmp", delete=False) as file:
                    temporary = Path(file.name)
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return updated


# 子脚本定位与结构化协议

def resolve_script(root: Path, candidates: Sequence[str]) -> Path | None:
    """按候选顺序定位子脚本。"""
    root = root.resolve()
    for relative in candidates:
        path = (root / relative).resolve()
        if path.is_file() and path.is_relative_to(root):
            return path
    return None


def _clean_output(output: str) -> str:
    return ANSI_RE.sub("", output or "").replace("\r", "")


def extract_child_result(
    output: str,
) -> tuple[tuple[str, list[str]] | None, str]:
    """读取并隐藏子脚本的内部结果行。"""
    result: tuple[str, list[str]] | None = None
    visible_lines: list[str] = []
    for raw_line in _clean_output(output).splitlines():
        line = raw_line.strip()
        if not line.startswith(DAILY_RESULT_PREFIX):
            visible_lines.append(raw_line)
            continue

        try:
            payload = json.loads(line.removeprefix(DAILY_RESULT_PREFIX))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("version") != 1:
            continue
        status = payload.get("status")
        details = payload.get("details")
        if status not in VALID_RESULT_STATUSES or not isinstance(details, list):
            continue
        if not all(isinstance(item, str) and item.strip() for item in details):
            continue
        result = (status, [item.strip() for item in details])

    visible = "\n".join(visible_lines)
    if output.endswith(("\n", "\r")) and visible:
        visible += "\n"
    return result, visible


# 子脚本执行

def run_python_child(script: Path, timeout: int = 900) -> ChildRun:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["DAILY_ALL_CHILD"] = "1"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            cwd=str(script.parent),
        )
        return ChildRun(completed.returncode, completed.stdout or "", time.monotonic() - started)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return ChildRun(124, output, time.monotonic() - started, timed_out=True)


# 日志与统一通知

def format_report(results: Sequence[TaskResult], total_seconds: float) -> str:
    icons = {"success": "✅", "warn": "⚠️", "fail": "❌"}
    lines = ["📋 每日任务", ""]
    for result in results:
        lines.append(f"{icons.get(result.status, '⚠️')} {result.name}")
        lines.extend(result.details or ["无结果"])
        lines.append("")

    minutes, seconds = divmod(int(total_seconds), 60)
    duration = f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"
    lines.append(f"⏱️ 耗时：{duration}")
    return "\n".join(lines).strip()


def build_default_tasks() -> list[TaskSpec]:
    """注册子脚本、基础凭证和执行顺序。"""
    return [
        TaskSpec(
            name="百度网盘",
            candidates=("baidupan.py",),
            timeout=600,
            key="baidupan",
            required_env=("BAIDU_COOKIE",),
        ),
        TaskSpec(
            name="夸克网盘",
            candidates=("quark.py",),
            timeout=180,
            key="quark",
            required_env=("QUARK_COOKIE",),
        ),
        TaskSpec(
            name="阿里云盘",
            candidates=("aliyunpan.py",),
            timeout=180,
            key="aliyunpan",
            required_env=("ALIYUN_ACCOUNTS",),
        ),
        TaskSpec(
            name="B站每日任务",
            candidates=("Bilibili.py",),
            timeout=600,
            key="bilibili",
            required_env=("BILIBILI_COOKIE",),
        ),
        TaskSpec(
            name="NodeSeek",
            candidates=("nodeseek.py",),
            timeout=600,
            key="nodeseek",
            required_env=("NS_COOKIE",),
        ),
        TaskSpec(
            name="AnyRouter",
            candidates=("anyrouter_checkin.py",),
            timeout=600,
            key="anyrouter",
            required_env=("ANYROUTER_CONFIG",),
        ),
        TaskSpec(
            name="ISVORO",
            candidates=("isvoro_checkin.py",),
            timeout=180,
            key="isvoro",
            required_env=("ISVORO_COOKIE",),
        ),
        TaskSpec(
            name="AkileCloud",
            candidates=("akile_checkin.py",),
            timeout=180,
            key="akile",
            required_env=("AKILE_TOKEN",),
        ),
    ]


def partition_tasks(
    tasks: Sequence[TaskSpec],
    raw_disabled: str,
) -> tuple[list[TaskSpec], list[TaskSpec]]:
    """按运行开关筛选任务，保持执行顺序。"""
    disabled_tokens = {
        token.strip().casefold()
        for token in re.split(r"[,;|&\n]+", raw_disabled or "")
        if token.strip()
    }
    if not disabled_tokens:
        return list(tasks), []

    enabled: list[TaskSpec] = []
    disabled: list[TaskSpec] = []
    for task in tasks:
        aliases = {task.name.casefold()}
        if task.key:
            aliases.add(task.key.casefold())
        aliases.update(Path(candidate).stem.casefold() for candidate in task.candidates)
        (disabled if aliases & disabled_tokens else enabled).append(task)
    return enabled, disabled


def select_runnable_tasks(
    root: Path, environ: Mapping[str, str],
) -> tuple[list[TaskSpec], list[tuple[str, str]]]:
    """只选择文件存在、基础凭证非空且未关闭的任务，不解析凭证。"""
    tasks = build_default_tasks()
    _, disabled = partition_tasks(tasks, str(environ.get(DISABLED_ENV_NAME, "") or ""))
    disabled_keys = {task.key for task in disabled}
    runnable: list[TaskSpec] = []
    skipped: list[tuple[str, str]] = []
    for task in tasks:
        if task.key in disabled_keys:
            skipped.append((task.name, "已关闭"))
        elif resolve_script(root, task.candidates) is None:
            skipped.append((task.name, "未拉取脚本"))
        else:
            missing = [name for name in task.required_env
                       if not str(environ.get(name, "") or "").strip()]
            if missing:
                skipped.append((task.name, "未配置 " + "、".join(missing)))
            else:
                runnable.append(task)
    return runnable, skipped


# 定时任务初始化

def _cron_script_path(command: str) -> Path | None:
    """识别 task [-l] [-m 超时] 文件 [now]，不把脚本参数或其他仓库认成同一任务。"""
    try:
        parts = shlex.split(command)
        if not parts or parts[0] != "task":
            return None
        index = 1
        while index < len(parts) and parts[index].startswith("-"):
            options = parts[index][1:]
            if not options:
                return None
            for position, option in enumerate(options):
                if option == "l":
                    continue
                if option != "m":
                    return None
                value = options[position + 1:]
                if not value:
                    index += 1
                    if index >= len(parts):
                        return None
                    value = parts[index]
                if not value or value.startswith("-"):
                    return None
                break
            index += 1
        remaining = parts[index:]
        if len(remaining) not in (1, 2) or (len(remaining) == 2 and remaining[1] != "now"):
            return None
        path = Path(remaining[0])
        if not path.is_absolute():
            base = os.getenv("dir_scripts", "")
            if not base:
                return None
            path = Path(base) / path
        return path.resolve()
    except (OSError, ValueError):
        return None


def _api_data(response: object) -> object:
    if not isinstance(response, dict) or response.get("code") != 200:
        raise RuntimeError("青龙 API 返回非成功结果")
    return response.get("data")


def _task_script_argument(path: Path) -> str:
    """青龙脚本目录内使用相对路径，其余路径保持原值。"""
    base = os.getenv("dir_scripts", "")
    if base and Path(base).is_absolute():
        try:
            return path.resolve().relative_to(Path(base).resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _call_ql_api(api: object, method: str, payload: dict) -> object:
    """调用内置任务 API；未直接提供的方法通过 Node 桥接执行。"""
    function = getattr(api, method, None)
    if callable(function):
        return function(payload)
    bridge = getattr(api, "_execute_node", None)
    if method in {"getCrons", "updateCron"} and callable(bridge):
        return bridge(method, payload)
    raise RuntimeError("青龙客户端不支持需要的任务 API")


def setup_crons(root: Path, api: object | None = None) -> int:
    """初始化定时任务；已有同路径任务的定时及启停状态不变。"""
    if api is None:
        api = globals().get("QLAPI", getattr(builtins, "QLAPI", None))
    if api is None:
        print("初始化失败：没有青龙内置 QLAPI，请通过 task 执行 --setup-crons")
        return 1
    try:
        existing = _api_data(_call_ql_api(api, "getCrons", {"searchValue": ""}))
        if not isinstance(existing, list):
            raise RuntimeError("青龙任务列表格式无效")
        paths = {_cron_script_path(str(row.get("command", ""))) for row in existing
                 if isinstance(row, dict)}
        entries = [(task.name, resolve_script(root, task.candidates),
                    "0 22 * * *" if task.key == "akile" else "@once")
                   for task in build_default_tasks()]
        entries.append(("每日任务合集", root / "daily_all.py", "45 8 * * *"))
        for name, path, schedule in entries:
            if path is None or not path.is_file():
                continue
            path = path.resolve()
            if path in paths:
                print(f"保留已有任务：{name}（不更改定时和启停状态）")
                continue
            payload = {"name": name,
                       "command": f"task {shlex.quote(_task_script_argument(path))} now",
                       "schedule": schedule, "labels": ["QingLongScripts"],
                       "extra_schedules": []}
            sub_id = os.getenv("SUB_ID", "")
            if sub_id.isdecimal() and int(sub_id) > 0:
                payload["sub_id"] = int(sub_id)
            created = _api_data(api.createCron(payload))
            if (not isinstance(created, dict) or not isinstance(created.get("id"), int)
                    or created["id"] <= 0):
                raise RuntimeError("青龙创建任务未返回有效 ID")
            paths.add(path)
            rule = "单次（手动运行）" if schedule == "@once" else schedule
            print(f"新增任务：{name}｜启用｜{rule}")
    except Exception as exc:
        print(f"初始化失败（{type(exc).__name__}），请查看订阅日志和面板任务列表")
        return 1
    print("任务初始化完成；没有执行签到、安装依赖或发送通知")
    return 0


# 订阅运行目录整理

class OrganizeError(Exception):
    """可直接写入日志的整理边界错误，不包含接口响应或凭证。"""


SUBSCRIPTION_RECORD_PREFIX = b"# QingLongScripts subscription: "


def _runtime_subscription_record(content: bytes) -> object:
    """读取合集末行的目录归属信息，不执行脚本。"""
    last_line = content.rstrip(b"\r\n").rsplit(b"\n", 1)[-1]
    if not last_line.startswith(SUBSCRIPTION_RECORD_PREFIX):
        return None
    try:
        return json.loads(last_line.removeprefix(SUBSCRIPTION_RECORD_PREFIX))
    except ValueError:
        raise OrganizeError("目标合集的目录归属信息无效，未覆盖文件") from None


def _replace_file(path: Path, content: bytes) -> None:
    """同目录临时文件原子替换；内容相同时保留文件及其修改时间。"""
    if path.is_file() and path.read_bytes() == content:
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".organize-", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _process_ancestors() -> set[int]:
    """识别当前整理进程及 task/timeout 外层进程；无法读取时保守停止追溯。"""
    ancestors = {os.getpid()}
    pid = os.getppid()
    for _ in range(32):
        if pid <= 0 or pid in ancestors:
            break
        ancestors.add(pid)
        try:
            # comm 字段可含空格和括号，必须从最后一个右括号后取 state、ppid。
            stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return ancestors


def _organize_cron_updates(
    rows: list, source: Path, target: Path, names: Mapping[str, str],
) -> list[dict]:
    """仅迁移标准 task 命令，保留定时、标签、钩子和订阅关联。"""
    updates: list[dict] = []
    source_paths = {source / name for name in names}
    managed_paths = source_paths | {target / name for name in names}
    counts: dict[Path, int] = {}
    ancestors = _process_ancestors()
    for row in rows:
        if not isinstance(row, dict):
            raise OrganizeError("任务列表格式无效，未整理文件")
        path = _cron_script_path(str(row.get("command", "")))
        status = row.get("status")
        own_organizer = (path == source / "daily_all.py" and status == 0
                         and type(row.get("pid")) is int and row["pid"] in ancestors)
        # 青龙结束任务后可能保留旧 PID；以状态区分空闲/禁用与真正运行/排队。
        busy = status in (0, 3) or (status not in (1, 2) and bool(row.get("pid")))
        if path in managed_paths and busy and not own_organizer:
            phase = {0: "运行中", 3: "排队中"}.get(status, "状态未确认")
            identity = row.get("id") if type(row.get("id")) is int else "未知"
            raise OrganizeError(f"任务未结束：{names[path.name]}｜{phase}｜ID={identity}；请结束后重新运行订阅")
        if path is not None:
            counts[path] = counts.get(path, 0) + 1
    for row in rows:
        command = str(row.get("command", ""))
        path = _cron_script_path(command)
        if path not in managed_paths:
            # 自定义参数或其他执行器不能悄悄遗留在将被清理的旧路径。
            if path is None:
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    tokens = []
                base = source.parent.parent
                references = {str(p) for p in source_paths} | {p.as_posix() for p in source_paths}
                references |= {p.relative_to(base).as_posix() for p in source_paths}
                if any(token in references for token in tokens):
                    raise OrganizeError("旧目录存在带自定义参数的任务，请先在面板调整命令")
            continue
        destination = target / path.name
        if counts.get(path, 0) > 1 or (path in source_paths and counts.get(destination, 0)):
            raise OrganizeError("同一脚本存在重复任务，请先在面板保留一个任务")
        if not isinstance(row.get("id"), int) or not row.get("schedule"):
            raise OrganizeError("待迁移任务缺少有效 ID 或定时规则")
        parts = shlex.split(command)
        parts[-2 if parts[-1] == "now" else -1] = _task_script_argument(destination)
        updated_command = shlex.join(parts)
        if path == destination and updated_command == command:
            continue
        payload = {"id": row["id"], "command": updated_command,
                   "name": names[path.name] if path in source_paths else row.get("name", names[path.name]),
                   "schedule": row["schedule"],
                   "labels": row.get("labels") or [],
                   "extra_schedules": row.get("extra_schedules") or []}
        for field in ("sub_id", "task_before", "task_after"):
            if row.get(field) is not None:
                payload[field] = row[field]
        updates.append(payload)
    return updates


def organize_subscription(root: Path, api: object | None = None) -> int:
    """整理单个订阅到固定同级目录；可重复执行，失败不清理未经核对的源文件。"""
    if api is None:
        api = globals().get("QLAPI", getattr(builtins, "QLAPI", None))
    lock: Path | None = None
    try:
        if api is None:
            raise OrganizeError("没有青龙内置 QLAPI，请通过 task 执行 --organize")
        base_value = os.getenv("dir_scripts", "")
        if not base_value or not Path(base_value).is_absolute():
            raise OrganizeError("缺少有效的青龙 dir_scripts，未整理文件")
        base = Path(base_value).resolve()
        source = root.absolute()
        target = base / "QingLongScripts"
        if (source.name != "scripts" or source.parent.parent != base
                or source.parent == target or not source.is_dir()
                or source.is_symlink() or source.parent.is_symlink()
                or source.resolve() != source or target.is_symlink()):
            raise OrganizeError("源目录必须是青龙脚本目录内的单个订阅/scripts，且不能是链接")
        names = {task.candidates[0]: task.name for task in build_default_tasks()}
        names["daily_all.py"] = "每日任务合集"
        files: dict[str, bytes] = {}
        for name in names:
            file = source / name
            if file.is_symlink() or (file.exists() and not file.is_file()):
                raise OrganizeError(f"源文件不是普通文件：{name}")
            if file.is_file():
                files[name] = file.read_bytes()
        if "daily_all.py" not in files:
            raise OrganizeError("本次订阅没有合集脚本，未整理文件")
        helpers = {name: (source.parent / name).read_bytes()
                   for name in ("notify.py", "sendNotify.js")
                   if (source.parent / name).is_file() and not (source.parent / name).is_symlink()}

        # 新记录与业务状态共用一个文件；旧记录仅用于迁移。
        store = StateStore(target / "state.json")
        marker = target / ".subscription.json"
        legacy_content: bytes | None = None
        owned: set[str] = set()
        if target.exists():
            if not target.is_dir() or marker.is_symlink():
                raise OrganizeError("目标目录或归属记录无效，未覆盖文件")
            collection = target / "daily_all.py"
            if (collection.is_symlink() or (collection.exists() and not collection.is_file())
                    or (marker.exists() and not marker.is_file())):
                raise OrganizeError("目标合集或旧归属记录不是普通文件，未覆盖文件")
            records: list[object] = []
            saved = store.read("subscription")
            if saved:
                records.append(saved)
            if collection.is_file():
                runtime_record = _runtime_subscription_record(collection.read_bytes())
                if runtime_record is not None:
                    records.append(runtime_record)
            if marker.is_file():
                legacy_content = marker.read_bytes()
                records.append(json.loads(legacy_content))
            for record in records:
                if (not isinstance(record, dict) or record.get("version") != 1
                        or record.get("source") != source.parent.name
                        or not isinstance(record.get("files"), list)
                        or not all(isinstance(name, str) and name in names for name in record["files"])):
                    raise OrganizeError("目标目录归属不匹配，未覆盖文件")
                owned.update(record["files"])
            if not records and any(file.name != "state.json" for file in target.iterdir()):
                raise OrganizeError("QingLongScripts 已有文件但无法确认本订阅归属，未覆盖文件")
        for name in files:
            file = target / name
            if file.is_symlink() or (file.exists() and (name not in owned or not file.is_file())):
                raise OrganizeError(f"目标文件归属不明或不是普通文件：{name}")
        rows = _api_data(_call_ql_api(api, "getCrons", {"searchValue": ""}))
        if not isinstance(rows, list):
            raise OrganizeError("青龙任务列表格式无效")
        updates = _organize_cron_updates(rows, source, target, {name: names[name] for name in files})

        target.mkdir(exist_ok=True)
        candidate_lock = target / ".organize.lock"
        try:
            with candidate_lock.open("x", encoding="utf-8") as file:
                file.write(str(os.getpid()))
            lock = candidate_lock
        except FileExistsError:
            raise OrganizeError("整理锁已存在，请确认没有整理进程运行；异常中断时手动移除 .organize.lock")
        record = {"version": 1, "source": source.parent.name, "files": sorted(owned | files.keys())}
        def save_ownership(previous):
            if previous and previous.get("source") != record["source"]:
                raise OrganizeError("目录归属发生变化，未覆盖文件")
            return {**previous, **record}
        # 锁内更新自己的分区，不覆盖同时写入的签到统计。
        store.update("subscription", save_ownership)
        destination_files = {"daily_all.py": files["daily_all.py"], **files}
        # 先保存归属，后续复制中断也可直接重试。
        for name, content in destination_files.items():
            _replace_file(target / name, content)
        for payload in updates:
            _api_data(_call_ql_api(api, "updateCron", payload))
            print(f"迁移任务：{payload['name']}（保留定时和启停状态）")
        if setup_crons(target, api) != 0:
            raise OrganizeError("任务初始化未完成，保留源文件，修复后重新运行订阅")

        # 所有目标与源文件再次比对通过后，才逐个删除明确的源副本。
        for name, content in files.items():
            if ((source / name).is_symlink() or (source / name).read_bytes() != content
                    or (target / name).is_symlink()
                    or (target / name).read_bytes() != destination_files[name]):
                raise OrganizeError("整理期间文件发生变化，保留源文件，请重新运行订阅")
        if legacy_content is not None:
            if (marker.is_symlink() or not marker.is_file()
                    or marker.read_bytes() != legacy_content):
                raise OrganizeError("旧归属记录在整理期间发生变化，保留源文件")
            marker.unlink()
        for name in files:
            (source / name).unlink()
        for name, content in helpers.items():
            helper = source.parent / name
            if helper.is_file() and not helper.is_symlink() and helper.read_bytes() == content:
                helper.unlink()
        for directory in (source, source.parent):
            if directory.exists():
                if any(directory.iterdir()):
                    print(f"保留含未知文件的源目录：{directory.relative_to(base).as_posix()}")
                else:
                    directory.rmdir()
    except OrganizeError as exc:
        print(f"整理停止：{exc}")
        return 1
    except Exception as exc:
        print(f"整理失败（{type(exc).__name__}）；已复制文件和任务保留，可重新运行订阅")
        return 1
    finally:
        if lock is not None:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                print("整理锁无法清理，确认进程结束后手动移除 .organize.lock")
    print(f"整理完成：QingLongScripts/｜{len(files)} 个脚本；不保留通知辅助文件")
    return 0


def execute_task(root: Path, spec: TaskSpec) -> TaskResult:
    script = resolve_script(root, spec.candidates)
    if script is None:
        return TaskResult(
            spec.name,
            "skip",
            [f"未找到脚本：{' 或 '.join(spec.candidates)}"],
            0,
            0.0,
            "",
            root,
        )

    print(f"\n{'=' * 16} {spec.name} {'=' * 16}")
    display_path = script.relative_to(root) if script.is_relative_to(root) else script
    print(f"脚本：{display_path}")

    child = run_python_child(script, timeout=spec.timeout)

    structured_result, visible_output = extract_child_result(child.output)
    if visible_output.strip():
        print("--- 子脚本日志 ---")
        print(visible_output.rstrip())
        print("--- 子脚本日志结束 ---")

    if child.timed_out:
        status, details = "fail", [f"执行超时（>{spec.timeout}秒）"]
    elif structured_result is not None:
        status, details = structured_result
        if child.returncode != 0 and status != "fail":
            status = "fail"
            details = [
                f"子脚本异常退出（退出码 {child.returncode}）",
                *details,
            ][:3]
    else:
        status = "fail"
        details = ["子脚本未返回有效的结构化结果"]
        if child.returncode != 0:
            details.insert(0, f"子脚本异常退出（退出码 {child.returncode}）")

    print(f"汇总：{status}｜{'；'.join(details)}")
    return TaskResult(
        spec.name,
        status,
        details,
        child.returncode,
        child.duration,
        visible_output,
        script,
    )


def system_notify(title: str, content: str) -> str:
    """只在合集末尾调用一次青龙系统通知。"""
    try:
        response = QLAPI.systemNotify({"title": title, "content": content})  # type: ignore[name-defined]
        _api_data(response)
        return "已发送"
    except NameError:
        return "当前环境没有 QLAPI，已跳过"
    except Exception as exc:
        return f"发送失败（{type(exc).__name__}）"


def _notification_title(results: Sequence[TaskResult]) -> str:
    if any(result.status == "fail" for result in results):
        return "每日任务 · 有失败"
    if any(result.status == "warn" for result in results):
        return "每日任务 · 有提醒"
    return "每日任务 · 全部完成"


# 程序入口

def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_output()
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="运行已配置的每日任务，或初始化青龙定时任务")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--setup-crons", action="store_true", help="只初始化任务，不签到、不通知")
    modes.add_argument("--organize", action="store_true", help="整理订阅文件和任务路径，再初始化任务")
    args = parser.parse_args(argv)
    if args.organize:
        return organize_subscription(Path(__file__).absolute().parent)
    if args.setup_crons:
        return setup_crons(root)
    started = time.monotonic()
    results: list[TaskResult] = []
    enabled_tasks, skipped = select_runnable_tasks(root, os.environ)

    print(f"==== 每日任务合集开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    for name, reason in skipped:
        print(f"跳过 {name}：{reason}")
    if not enabled_tasks:
        print("没有可运行的子脚本：至少拉取一个子脚本并配置其必填变量；不发送通知")
        return 0
    for spec in enabled_tasks:
        try:
            result = execute_task(root, spec)
            if result.status != "skip":
                results.append(result)
        except Exception as exc:
            message = f"调度异常（{type(exc).__name__}）"
            print(f"❌ {spec.name}：{message}")
            results.append(
                TaskResult(spec.name, "fail", [message], 1, 0.0, "", root)
            )

    if not results:
        print("全部子脚本已跳过，不发送通知")
        return 0
    total_seconds = time.monotonic() - started
    report = format_report(results, total_seconds)
    report += f"\n🕒 完成：{datetime.now().strftime('%m-%d %H:%M')}"

    print("\n==== 每日任务合集汇总 ====")
    print(report)
    print(f"通知：{system_notify(_notification_title(results), report)}")

    print(f"\n==== 每日任务合集结束 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    return 1 if any(result.status == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
