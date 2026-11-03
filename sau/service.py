"""成就查询 / 解锁业务流程。

依赖注入：`bindings_factory` 生成 FlatBindings 兼容对象，测试时换成
FakeBindings 即可，不需要真实 Steam 环境。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .models import Achievement, UnlockReport
from . import steamapi
from .steamapi import app_root

# 子进程 JSON 结果行前缀（stdout 可能混有 Steam 客户端的 minidump 日志）。
JSON_PREFIX = "##JSON##"

# StoreStats 之后给 Steam 客户端跑回调、把状态同步出去的时间。
CALLBACK_TICK = 0.2


class ServiceError(Exception):
    """业务层错误（信息已面向用户整理好）。"""


def load_names_json(path, appid: int) -> list[str]:
    """读取离线成就名称表。

    支持两种格式：
      ["ACH_A", "ACH_B"]                —— 直接列表
      {"440": ["ACH_A", ...], ...}      —— 按 appid 分组的字典
    文件用 utf-8-sig 读取，兼容 Windows 记事本写出的 BOM。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ServiceError(f"无法读取名称表 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ServiceError(f"名称表 {path} 不是合法 JSON：{exc}") from exc
    if isinstance(data, dict):
        data = data.get(str(appid))
    if not isinstance(data, list) or not all(isinstance(n, str) and n for n in data):
        raise ServiceError(f"名称表 {path} 需要是名称列表，或 {{appid: [名称...]}} 字典")
    return _dedupe(data)


def _dedupe(items) -> list[str]:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class UnlockService:
    def __init__(self, bindings_factory=None, dll_path=None):
        self._factory = bindings_factory or steamapi.FlatBindings.load
        self._dll_path = dll_path

    @property
    def dll_path(self) -> str | None:
        return str(self._dll_path) if self._dll_path else None

    # ---- 内部 -----------------------------------------------------------

    def _bindings(self):
        dll = self._dll_path or steamapi.default_dll_path()
        if dll is None:
            raise ServiceError(
                "未指定 steam_api DLL。请用 --dll 或环境变量 SAU_STEAM_API_DLL 指定，"
                "或把 DLL 放到 <项目>/sdk/steam_api64.dll（见 scripts/fetch_sdk.py）。"
            )
        try:
            return self._factory(dll)
        except steamapi.SteamApiError as exc:
            raise ServiceError(str(exc)) from exc

    @contextlib.contextmanager
    def _appid_cwd(self, appid: int):
        # steam_appid.txt 逻辑在 steamapi.appid_cwd（游戏库扫描共用）
        with steamapi.appid_cwd(appid):
            yield

    def _resolve_schema(self, user_stats, names_json, appid: int) -> list[str]:
        runtime_names = user_stats.get_achievement_names()
        if runtime_names is not None:
            return [n for n in runtime_names if n]
        if names_json:
            return load_names_json(names_json, appid)
        raise ServiceError(
            "该 DLL 不支持枚举成就（接口过旧，缺 GetNumAchievements/GetAchievementName），"
            "请通过 --names-json 提供成就 API 名称列表。"
        )

    # ---- 对外 -----------------------------------------------------------

    def count_achievements(self, appid: int) -> int | None:
        """只查询该应用的成就数量（轻量，供游戏库扫描用）。"""
        bindings = self._bindings()
        try:
            with self._appid_cwd(appid):
                bindings.init()
                try:
                    us = bindings.user_stats()
                    count = us.get_num_achievements()
                    if count == 0:
                        # 冷启动竞态兜底：成就统计是异步加载的，刚 init 完可能
                        # 还没就绪（真机偶发：TF2 首次冻结启动返回 0，重查 520）
                        bindings.run_callbacks()
                        time.sleep(0.4)
                        count = us.get_num_achievements()
                    return count
                finally:
                    bindings.shutdown()
        except steamapi.SteamApiError:
            return None

    def list_achievements(self, appid: int, names_json=None) -> list[Achievement]:
        bindings = self._bindings()
        try:
            with self._appid_cwd(appid):
                bindings.init()
                try:
                    us = bindings.user_stats()
                    us.request_current_stats()
                    schema = self._resolve_schema(us, names_json, appid)
                    return [
                        Achievement(
                            api_name=name,
                            display_name=us.display_attribute(name, "name"),
                            description=us.display_attribute(name, "desc"),
                            achieved=us.get_achievement(name),
                        )
                        for name in schema
                    ]
                finally:
                    bindings.shutdown()
        except steamapi.SteamApiError as exc:
            raise ServiceError(str(exc)) from exc

    def unlock(
        self,
        appid: int,
        names=None,
        unlock_all: bool = False,
        dry_run: bool = False,
        names_json=None,
        settle: float = 2.0,
        sleep=time.sleep,
        progress=None,
    ) -> UnlockReport:
        """解锁成就并同步到 Steam 服务器。

        - `names` 与 `unlock_all` 二选一；同时传入时以 `unlock_all` 为准
          （`names` 被忽略——CLI 层已显式拒绝这种用法）；
        - `dry_run` 时只查询并返回"将要解锁"的清单，不改动任何状态；
        - 未识别的名称不会中断流程：其余成就照常解锁，结果记入 report.unknown；
        - `progress(done, total)` 每处理完一个成就回调一次（GUI 进度条用）。
        """
        if not unlock_all and not names:
            raise ServiceError("必须指定要解锁的成就名称，或使用 --all")
        bindings = self._bindings()
        try:
            with self._appid_cwd(appid):
                bindings.init()
                try:
                    us = bindings.user_stats()
                    us.request_current_stats()
                    schema = self._resolve_schema(us, names_json, appid)
                    requested = _dedupe(schema) if unlock_all else _dedupe(names or [])
                    known = set(schema)
                    report = UnlockReport(
                        appid=appid,
                        requested=requested,
                        unknown=[n for n in requested if n not in known],
                        dry_run=dry_run,
                    )
                    targets = [n for n in requested if n in known]
                    for index, name in enumerate(targets, start=1):
                        if us.get_achievement(name):
                            report.already_unlocked.append(name)
                        elif dry_run:
                            report.newly_unlocked.append(name)
                        elif us.set_achievement(name):
                            report.newly_unlocked.append(name)
                        else:
                            report.failed.append(name)
                        if progress is not None:
                            progress(index, len(targets))
                    if not dry_run and report.newly_unlocked:
                        report.stored = us.store_stats()
                        if report.stored and settle > 0:
                            for _ in range(int(settle / CALLBACK_TICK)):
                                bindings.run_callbacks()
                                sleep(CALLBACK_TICK)
                    return report
                finally:
                    bindings.shutdown()
        except steamapi.SteamApiError as exc:
            raise ServiceError(str(exc)) from exc


def _run_sau_json(args: list[str]) -> dict | None:
    """运行内部子命令并取回 JSON 结果；失败返回 None。

    源码模式：`python -m sau <args>`，解析 stdout 的 ##JSON## 行。
    冻结模式（PyInstaller 单文件）：windowed exe 没有 stdout，改为 exe 自调用
    `sau_gui.exe --json-out <临时文件> <args>`，结果经文件传递。
    """
    if getattr(sys, "frozen", False):
        out_file = Path(tempfile.gettempdir()) / f"sau_json_{os.getpid()}_{time.time_ns()}.json"
        cmd = [sys.executable, "--json-out", str(out_file), *args]
        try:
            subprocess.run(cmd, capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            return None
        try:
            return json.loads(out_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        finally:
            out_file.unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "sau", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, cwd=str(app_root())
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in proc.stdout.splitlines():
        if line.startswith(JSON_PREFIX):
            try:
                return json.loads(line[len(JSON_PREFIX):])
            except ValueError:
                return None
    return None


def run_sau_json(args: list[str]) -> dict | None:
    """_run_sau_json 的公开别名（library/gui 跨模块使用）。"""
    return _run_sau_json(args)


def list_achievements_subprocess(appid: int, dll: str | None = None,
                                 names_json: str | None = None) -> list[Achievement]:
    """在独立子进程中列出单个游戏的成就（GUI 用，避免 schema 串台）。"""
    args = ["_list-one", str(appid)]
    if names_json:
        args += ["--names-json", names_json]
    if dll:
        args = ["--dll", dll, *args]
    data = _run_sau_json(args)
    if data is None:
        raise ServiceError(f"子进程执行失败（appid {appid}）：无 JSON 结果")
    if data.get("error"):
        raise ServiceError(f"appid {appid}：{data['error']}")
    return [
        Achievement(
            api_name=row["api_name"],
            display_name=row.get("display_name") or "",
            description=row.get("description") or "",
            achieved=bool(row.get("achieved")),
        )
        for row in data.get("achievements") or []
    ]


def unlock_one_subprocess(appid: int, dry_run: bool = False, settle: float = 1.0,
                          dll: str | None = None, all_achievements: bool = True,
                          names: list[str] | None = None) -> UnlockReport:
    """在独立子进程中解锁单个游戏的成就。

    SteamAPI 同进程内重复 Init 切换 appid 后，ISteamUserStats 会继续返回第一个
    应用的 schema（实测：init 440 后再 init 730/570/602320 全部返回 520）——跨游戏
    操作必须每游戏一个子进程，否则会把成就解锁到错误的游戏上。
    """
    args = ["_unlock-one", str(appid), "--settle", str(settle)]
    if names:
        args.extend(names)
    if all_achievements:
        args.append("--all")
    if dry_run:
        args.append("--dry-run")
    if dll:
        args = ["--dll", dll, *args]
    data = _run_sau_json(args)
    if data is None:
        raise ServiceError(f"子进程执行失败（appid {appid}）：无 JSON 结果")
    if data.get("error"):
        raise ServiceError(f"appid {appid}：{data['error']}")
    return UnlockReport(
        appid=appid,
        requested=data.get("requested") or [],
        newly_unlocked=data.get("newly_unlocked") or [],
        already_unlocked=data.get("already_unlocked") or [],
        unknown=data.get("unknown") or [],
        failed=data.get("failed") or [],
        stored=bool(data.get("stored")),
        dry_run=bool(data.get("dry_run")),
    )


def count_achievements_subprocess(appid: int, dll: str | None = None) -> int | None:
    """在独立子进程中查询成就数量（游戏库扫描用）。None = 无法查询。"""
    args = ["_count-achievements", str(appid)]
    if dll:
        args = ["--dll", dll, *args]
    data = _run_sau_json(args)
    if data is None or data.get("error"):
        return None
    count = data.get("achievements")
    return None if count is None else int(count)
