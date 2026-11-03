"""命令行入口。

用法：
    python -m sau doctor
    python -m sau list <appid>
    python -m sau unlock <appid> <NAME>... [--dry-run]
    python -m sau unlock <appid> --all [--yes]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, peinfo
from .steamapi import FlatBindings, SteamApiError, default_dll_path
from .library import (
    LibraryError,
    build_library,
    find_steam_dir,
    games_with_achievements,
)
from .service import JSON_PREFIX, ServiceError, UnlockService, unlock_one_subprocess


def _emit_json(args, payload, out) -> None:
    """内部子命令的 JSON 输出：源码模式走 stdout；冻结模式经 --json-out 文件。"""
    line = JSON_PREFIX + json.dumps(payload, ensure_ascii=False)
    json_out = getattr(args, "json_out", None)
    if json_out:
        Path(json_out).write_text(line, encoding="utf-8")
    else:
        print(line, file=out)

_STEAM_COMMON_DIRS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
)
# 注：find_steam_dir 现由 sau.library 提供（games/unlock-library 共用），
# 此处保留常量仅为向后兼容。


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sau",
        description="Steam 成就解锁工具（仅解锁成就；操作自己的账户，风险自负）",
    )
    parser.add_argument("--version", action="version", version=f"sau {__version__}")
    parser.add_argument("--json-out", help=argparse.SUPPRESS)  # 冻结模式子进程 JSON 落盘
    parser.add_argument("--dll", help="steam_api(64).dll 路径（默认查找 SAU_STEAM_API_DLL 或 sdk/steam_api64.dll）")
    parser.add_argument("--names-json", help="离线成就 API 名称表 JSON（DLL 过旧无法枚举时提供）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="环境自检（Steam 安装、DLL、架构、导出）")
    sub.add_parser("gui", help="启动图形界面")

    cmd_list = sub.add_parser("list", help="列出应用的成就（含 API 名称，区分大小写）")
    cmd_list.add_argument("appid", type=int)

    cmd_unlock = sub.add_parser("unlock", help="解锁成就并同步到 Steam")
    cmd_unlock.add_argument("appid", type=int)
    cmd_unlock.add_argument("names", nargs="*", help="成就 API 名称（用 list 查看，区分大小写）")
    cmd_unlock.add_argument("--all", action="store_true", help="解锁全部未获得成就")
    cmd_unlock.add_argument("--dry-run", action="store_true", help="只演示将要解锁的内容，不实际改动")
    cmd_unlock.add_argument("--yes", "-y", action="store_true", help="--all 时跳过确认")
    cmd_unlock.add_argument("--settle", type=float, default=2.0, help="解锁后等待 Steam 同步的秒数（默认 2）")

    cmd_games = sub.add_parser("games", help="列出账户游戏库（自动获取，无需手输 AppID）")
    cmd_games.add_argument("--mode", choices=("installed", "owned", "webapi"), default="installed",
                           help="installed=本地已安装(离线)；owned=全库所有权扫描(免key)；webapi=官方接口(需key)")
    cmd_games.add_argument("--api-key", help="Web API key（webapi 模式必需）")
    cmd_games.add_argument("--steamid", type=int, help="SteamID64（webapi 模式默认自动读取当前账户）")
    cmd_games.add_argument("--with-achievements", action="store_true",
                           help="逐个扫描每个游戏的成就数量（较慢，结果缓存到 sdk/library_cache.json）")
    cmd_games.add_argument("--refresh", action="store_true", help="忽略缓存重新扫描")
    cmd_games.add_argument("--max-appid", type=int, default=400_000, help="owned 模式扫描的 AppID 上限")
    cmd_games.add_argument("--steam-dir", help="Steam 安装目录（默认读注册表/常见路径）")
    cmd_games.add_argument("--include-system-apps", action="store_true", help="包含 AppID<10 的 Steam 内部应用")

    cmd_ul = sub.add_parser("unlock-library", help="批量解锁游戏库内各游戏的全部未获得成就（危险）")
    cmd_ul.add_argument("--mode", choices=("installed", "owned", "webapi"), default="owned")
    cmd_ul.add_argument("--api-key")
    cmd_ul.add_argument("--steamid", type=int)
    cmd_ul.add_argument("--dry-run", action="store_true", help="只演示将要解锁的内容")
    cmd_ul.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    cmd_ul.add_argument("--settle", type=float, default=1.0, help="每个游戏解锁后等待同步的秒数")
    cmd_ul.add_argument("--limit", type=int, help="只处理前 N 个有成就的游戏（试运行用）")
    cmd_ul.add_argument("--refresh", action="store_true", help="忽略缓存重新扫描")
    cmd_ul.add_argument("--max-appid", type=int, default=400_000)
    cmd_ul.add_argument("--steam-dir")
    cmd_ul.add_argument("--include-system-apps", action="store_true")

    # 内部子命令：跨游戏操作必须每游戏一个子进程（同进程切换 appid 会读到
    # 第一个应用的 schema，见 DEVELOPMENT_LOG 坑 AA）。对 help 隐藏。
    sub.add_parser("_unlock-one", help=argparse.SUPPRESS)
    sub.add_parser("_count-achievements", help=argparse.SUPPRESS)
    sub.add_parser("_list-one", help=argparse.SUPPRESS)
    sub.add_parser("_scan-owned", help=argparse.SUPPRESS)
    sub.add_parser("_steam-id", help=argparse.SUPPRESS)
    sub.choices["_unlock-one"].add_argument("appid", type=int)
    sub.choices["_unlock-one"].add_argument("names", nargs="*")
    sub.choices["_unlock-one"].add_argument("--all", action="store_true")
    sub.choices["_unlock-one"].add_argument("--dry-run", action="store_true")
    sub.choices["_unlock-one"].add_argument("--settle", type=float, default=1.0)
    sub.choices["_count-achievements"].add_argument("appid", type=int)
    sub.choices["_list-one"].add_argument("appid", type=int)
    sub.choices["_list-one"].add_argument("--names-json")
    sub.choices["_scan-owned"].add_argument("--max-appid", type=int, default=400_000)
    return parser


def cmd_doctor(args, out) -> int:
    ok = True
    steam_dir = find_steam_dir()
    if steam_dir is None:
        print("[!!] 未找到 Steam 安装目录（本工具仅支持 Windows + 运行中的 Steam 客户端）", file=out)
        ok = False
    elif not (steam_dir / "steam.exe").exists():
        print(f"[!!] Steam 目录存在但缺少 steam.exe：{steam_dir}", file=out)
        ok = False
    else:
        print(f"[OK] Steam 安装目录：{steam_dir}", file=out)

    dll = args.dll or default_dll_path()
    if dll is None:
        print(
            "[!!] 未找到 steam_api DLL：请用 --dll / SAU_STEAM_API_DLL 指定，"
            "或运行 scripts/fetch_sdk.py 放到 sdk/steam_api64.dll",
            file=out,
        )
        return 1
    print(f"[--] DLL：{dll}", file=out)
    if not Path(dll).exists():
        print("[!!] 找不到 DLL 文件", file=out)
        return 1

    import struct

    python_bits = struct.calcsize("P") * 8
    info = peinfo.inspect(dll)
    if info is None or info["exports"] is None:
        print("[!!] 无法解析该文件（不是有效的 PE/DLL？）", file=out)
        return 1
    dll_bits = info["bits"]
    if dll_bits == python_bits:
        print(f"[OK] 架构匹配：DLL {info['machine_name']} / Python {python_bits} 位", file=out)
    else:
        print(
            f"[!!] 架构不匹配：DLL {info['machine_name']}（{dll_bits} 位）/ Python {python_bits} 位，"
            "加载会报 WinError 193",
            file=out,
        )
        ok = False

    try:
        FlatBindings.load(dll)
        print("[OK] 关键导出齐全，可以初始化（是否成功还取决于 Steam 运行状态）", file=out)
    except SteamApiError as exc:
        print(f"[!!] {exc}", file=out)
        ok = False
    return 0 if ok else 1


def cmd_list(args, service: UnlockService, out) -> int:
    achievements = service.list_achievements(args.appid, names_json=args.names_json)
    if not achievements:
        print(f"应用 {args.appid} 没有枚举到任何成就（该应用可能确实没有成就）", file=out)
        return 0
    done = sum(1 for a in achievements if a.achieved)
    print(f"应用 {args.appid} 成就（共 {len(achievements)}，已解锁 {done}）：", file=out)
    for a in achievements:
        mark = "[x]" if a.achieved else "[ ]"
        line = f"{mark} {a.api_name}"
        if a.display_name:
            line += f"  |  {a.display_name}"
        if a.description:
            line += f"  -  {a.description}"
        print(line, file=out)
    return 0


def cmd_unlock(args, service: UnlockService, out, input_fn) -> int:
    if args.all and args.names:
        print("错误：--all 与具体成就名称不要同时使用", file=out)
        return 2
    if not args.all and not args.names:
        print("错误：需要提供成就 API 名称，或使用 --all。可用 `sau list <appid>` 查看", file=out)
        return 2

    if not args.dry_run and args.all and not args.yes:
        answer = input_fn(
            f"将对应用 {args.appid} 解锁全部未获得的成就，并同步到 Steam 服务器（无法撤销）。"
            "继续？[y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("已取消", file=out)
            return 1

    report = service.unlock(
        args.appid,
        names=args.names,
        unlock_all=args.all,
        dry_run=args.dry_run,
        names_json=args.names_json,
        settle=args.settle,
    )
    _print_report(report, out)
    return 0 if report.ok else 1


def _library_steam_dir(args):
    return Path(args.steam_dir) if getattr(args, "steam_dir", None) else find_steam_dir()


def cmd_games(args, out) -> int:
    progress = (lambda done, total: print(f"  … 扫描中 {done}/{total}", file=out))
    try:
        entries, cache = build_library(
            args.mode,
            steam_dir=_library_steam_dir(args),
            api_key=args.api_key,
            steam_id=args.steamid,
            with_achievements=args.with_achievements,
            refresh=args.refresh,
            max_appid=args.max_appid,
            include_system_apps=args.include_system_apps,
            progress=progress,
            dll=args.dll,
        )
    except (LibraryError, ServiceError) as exc:
        print(f"错误：{exc}", file=out)
        return 1
    if not entries:
        print("游戏库为空（该模式未枚举到任何应用）", file=out)
        return 0
    scanned = sum(1 for e in entries if e.achievements is not None)
    with_ach = sum(1 for e in entries if (e.achievements or 0) > 0)
    not_applicable = sum(1 for v in cache.achievements.values() if v < 0)
    header = f"游戏库（{args.mode} 模式，共 {len(entries)} 个"
    if cache.steam_id:
        header += f"，SteamID {cache.steam_id}"
    if scanned:
        header += f"，成就查询成功 {scanned}，其中有成就 {with_ach}"
        if not_applicable:
            header += f"，不适用/失败 {not_applicable}（--refresh 重试）"
    header += "）："
    print(header, file=out)
    entries.sort(key=lambda e: (-(e.achievements or 0), e.appid))
    for e in entries:
        marks = []
        if e.achievements is not None:
            marks.append(f"成就 {e.achievements}")
        if e.installed:
            marks.append("已安装")
        mark = f" [{'，'.join(marks)}]" if marks else ""
        print(f"  {e.appid:>7}  {e.name}{mark}", file=out)
    if not scanned and not args.with_achievements:
        print("提示：加 --with-achievements 可扫描每个游戏的成就数量（owned 模式较慢，结果缓存）", file=out)
    return 0


def cmd_unlock_library(args, out, input_fn) -> int:
    progress = (lambda done, total: print(f"  … 扫描中 {done}/{total}", file=out))
    try:
        entries, _ = build_library(
            args.mode,
            steam_dir=_library_steam_dir(args),
            api_key=args.api_key,
            steam_id=args.steamid,
            with_achievements=True,
            refresh=args.refresh,
            max_appid=args.max_appid,
            include_system_apps=args.include_system_apps,
            progress=progress,
            dll=args.dll,
        )
    except (LibraryError, ServiceError) as exc:
        print(f"错误：{exc}", file=out)
        return 1
    targets = games_with_achievements(entries)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("没有需要处理的游戏（有成就的游戏为 0 个）", file=out)
        return 0
    if not args.dry_run and not args.yes:
        answer = input_fn(
            f"将对 {len(targets)} 个游戏批量解锁全部未获得成就，并同步到 Steam 服务器"
            "（不可逆的大批量操作）。继续？[y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("已取消", file=out)
            return 1
    print(
        f"{'DRY-RUN 演示' if args.dry_run else '开始批量解锁'}：{len(targets)} 个有成就的游戏"
        "（每游戏独立子进程，避免 schema 串扰）",
        file=out,
    )
    totals = {"unlocked": 0, "already": 0, "unknown": 0, "failed": 0, "games_ok": 0,
              "games_error": 0}
    try:
        for index, entry in enumerate(targets, start=1):
            try:
                report = unlock_one_subprocess(
                    entry.appid, dry_run=args.dry_run, settle=args.settle, dll=args.dll
                )
            except ServiceError as exc:
                totals["games_error"] += 1
                print(
                    f"  [{index}/{len(targets)}] {entry.appid} {entry.name}: 出错（{exc}）",
                    file=out,
                )
                continue
            totals["unlocked"] += len(report.newly_unlocked)
            totals["already"] += len(report.already_unlocked)
            totals["unknown"] += len(report.unknown)
            totals["failed"] += len(report.failed)
            if report.ok or report.dry_run:
                totals["games_ok"] += 1
            print(
                f"  [{index}/{len(targets)}] {entry.appid} {entry.name}: "
                f"{'将解锁' if args.dry_run else '新解锁'} {len(report.newly_unlocked)}，"
                f"跳过 {len(report.already_unlocked)}，未识别 {len(report.unknown)}，"
                f"失败 {len(report.failed)}，同步 {'-' if args.dry_run else '成功' if report.stored else '失败'}",
                file=out,
            )
    except KeyboardInterrupt:
        print("已中断（已完成的游戏不受影响）", file=out)
        return 130
    print(
        f"完成：{totals['games_ok']}/{len(targets)} 个游戏成功"
        + (f"，{totals['games_error']} 个出错" if totals["games_error"] else "")
        + f"，{'将解锁' if args.dry_run else '新解锁'} {totals['unlocked']} 个成就，"
        f"跳过 {totals['already']}，未识别 {totals['unknown']}，失败 {totals['failed']}",
        file=out,
    )
    return 0 if totals["failed"] == 0 and totals["games_error"] == 0 else 1


def cmd_unlock_one(args, out) -> int:
    """内部命令：单游戏解锁，输出 ##JSON## 结果行（父进程解析）。"""
    service = UnlockService(dll_path=args.dll)
    try:
        report = service.unlock(
            args.appid, names=args.names or None, unlock_all=args.all,
            dry_run=args.dry_run, settle=args.settle,
        )
    except ServiceError as exc:
        _emit_json(args, {"appid": args.appid, "error": str(exc)}, out)
        return 1
    _emit_json(args, {
        "appid": report.appid,
        "requested": report.requested,
        "newly_unlocked": report.newly_unlocked,
        "already_unlocked": report.already_unlocked,
        "unknown": report.unknown,
        "failed": report.failed,
        "stored": report.stored,
        "dry_run": report.dry_run,
    }, out)
    return 0 if report.ok else 1


def cmd_count_achievements(args, out) -> int:
    """内部命令：查询单游戏成就数量，输出 ##JSON## 结果行。"""
    service = UnlockService(dll_path=args.dll)
    count = service.count_achievements(args.appid)
    if count is None:
        _emit_json(args, {"appid": args.appid, "error": "查询失败"}, out)
        return 1
    _emit_json(args, {"appid": args.appid, "achievements": count}, out)
    return 0


def cmd_list_one(args, out) -> int:
    """内部命令：单游戏成就列表，输出 ##JSON## 结果行（GUI 用）。"""
    service = UnlockService(dll_path=args.dll)
    try:
        rows = service.list_achievements(args.appid, names_json=args.names_json)
    except ServiceError as exc:
        _emit_json(args, {"appid": args.appid, "error": str(exc)}, out)
        return 1
    _emit_json(args, {
        "appid": args.appid,
        "achievements": [
            {
                "api_name": a.api_name,
                "display_name": a.display_name,
                "description": a.description,
                "achieved": a.achieved,
            }
            for a in rows
        ],
    }, out)
    return 0


def cmd_scan_owned(args, out) -> int:
    """内部命令：所有权全区间扫描，输出 ##JSON## 结果行（GUI 用）。"""
    from .library import scan_owned

    def factory(default_dll):
        return FlatBindings.load(args.dll or default_dll)

    try:
        owned = scan_owned(factory, max_appid=args.max_appid)
    except (LibraryError, ServiceError) as exc:
        _emit_json(args, {"error": str(exc)}, out)
        return 1
    _emit_json(args, {"owned": owned}, out)
    return 0


def cmd_steam_id(args, out) -> int:
    """内部命令：读取当前账户 SteamID，输出 ##JSON## 结果行（GUI 用）。"""
    from .library import _detect_steam_id

    def factory(default_dll):
        return FlatBindings.load(args.dll or default_dll)

    try:
        steam_id = _detect_steam_id(factory)
    except (LibraryError, ServiceError) as exc:
        _emit_json(args, {"error": str(exc)}, out)
        return 1
    _emit_json(args, {"steam_id": steam_id}, out)
    return 0 if steam_id else 1


def _print_report(report, out) -> None:
    prefix = "DRY-RUN 演示（未实际改动）" if report.dry_run else "解锁结果"
    print(f"应用 {report.appid} {prefix}：", file=out)
    verb = "将解锁" if report.dry_run else "新解锁"
    if report.newly_unlocked:
        print(f"  {verb} {len(report.newly_unlocked)} 个：{', '.join(report.newly_unlocked)}", file=out)
    else:
        print(f"  {verb} 0 个", file=out)
    if report.already_unlocked:
        print(
            f"  已是解锁状态（跳过）{len(report.already_unlocked)} 个："
            f"{', '.join(report.already_unlocked)}",
            file=out,
        )
    if report.unknown:
        print(
            f"  未识别 {len(report.unknown)} 个：{', '.join(report.unknown)}"
            "（请用 `sau list` 核对 API 名称，区分大小写）",
            file=out,
        )
    if report.failed:
        print(f"  解锁失败 {len(report.failed)} 个：{', '.join(report.failed)}", file=out)
    if report.dry_run:
        print("  统计同步：未执行（dry-run）", file=out)
    elif report.newly_unlocked:
        print("  统计同步：成功" if report.stored else "  统计同步：失败（可能未拥有该应用，或 Steam 未在线）", file=out)


def main(argv=None, *, factory=None, input_fn=input, out=None) -> int:
    if out is None:
        out = sys.stdout
        try:  # 中文 Windows 控制台可能是 GBK，兜底避免个别字符触发 UnicodeEncodeError
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    appid = getattr(args, "appid", None)
    if appid is not None and appid <= 0:
        print("错误：appid 必须是正整数", file=out)
        return 2

    service = UnlockService(bindings_factory=factory, dll_path=args.dll)
    try:
        if args.command == "doctor":
            return cmd_doctor(args, out)
        if args.command == "gui":
            from .gui import launch  # 延迟导入：CLI 不依赖 tkinter

            return launch()
        if args.command == "list":
            return cmd_list(args, service, out)
        if args.command == "unlock":
            return cmd_unlock(args, service, out, input_fn)
        if args.command == "games":
            return cmd_games(args, out)
        if args.command == "unlock-library":
            return cmd_unlock_library(args, out, input_fn)
        if args.command == "_unlock-one":
            return cmd_unlock_one(args, out)
        if args.command == "_count-achievements":
            return cmd_count_achievements(args, out)
        if args.command == "_list-one":
            return cmd_list_one(args, out)
        if args.command == "_scan-owned":
            return cmd_scan_owned(args, out)
        if args.command == "_steam-id":
            return cmd_steam_id(args, out)
    except (LibraryError, ServiceError) as exc:
        print(f"错误：{exc}", file=out)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=out)
        return 130
    return 2
