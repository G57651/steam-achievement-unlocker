"""在装有 Steam 的机器上做真实集成自检（手动运行，本脚本会操作真实账户）。

用法：
  python scripts/steam_selftest.py 440                     # 只读：列出成就
  python scripts/steam_selftest.py 440 --unlock ACH_NAME   # 解锁指定成就（需 --confirm）
  python scripts/steam_selftest.py 440 --unlock ACH_NAME --confirm --yes

前置条件：Steam 客户端正在运行且已登录；本账户拥有该应用；
sdk/steam_api64.dll 存在（scripts/fetch_sdk.py 可下载）或用 --dll 指定。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sau.service import ServiceError, UnlockService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Steam 成就真实环境自检")
    parser.add_argument("appid", type=int)
    parser.add_argument("--unlock", action="append", default=[], metavar="NAME")
    parser.add_argument("--confirm", action="store_true", help="确认执行真实解锁")
    parser.add_argument("--yes", action="store_true", help="解锁全部时跳过确认")
    parser.add_argument("--all", action="store_true", help="解锁全部未获得成就（危险）")
    parser.add_argument("--dll")
    parser.add_argument("--settle", type=float, default=2.0)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    service = UnlockService(dll_path=args.dll)
    try:
        if args.unlock or args.all:
            if (args.unlock or args.all) and not args.confirm:
                print("实际解锁会写入你的 Steam 账户，请追加 --confirm")
                return 2
            report = service.unlock(
                args.appid,
                names=args.unlock,
                unlock_all=args.all,
                settle=args.settle,
            )
            print("新解锁:", ", ".join(report.newly_unlocked) or "无")
            print("已是解锁状态:", ", ".join(report.already_unlocked) or "无")
            print("未识别:", ", ".join(report.unknown) or "无")
            print("失败:", ", ".join(report.failed) or "无")
            print("统计同步:", "成功" if report.stored else "失败")
            return 0 if report.ok else 1

        rows = service.list_achievements(args.appid)
        done = sum(1 for a in rows if a.achieved)
        print(f"应用 {args.appid} 成就 {len(rows)} 个，已解锁 {done} 个")
        for a in rows:
            mark = "[x]" if a.achieved else "[ ]"
            print(f"{mark} {a.api_name}  |  {a.display_name}")
        return 0
    except ServiceError as exc:
        print(f"错误：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
