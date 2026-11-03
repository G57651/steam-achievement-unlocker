"""PyInstaller 单文件入口。

无参数 → 启动图形界面；带参数 → 内部 CLI（冻结模式下子进程 exe 自调用，
见 sau/service.py 的 --json-out 协议）。
windowed 模式没有控制台，致命错误写入 exe 旁的 sau_crash.log。
"""

import sys
from pathlib import Path


def _install_crash_hook() -> None:
    import traceback

    def hook(exc_type, exc, tb):
        try:
            if getattr(sys, "frozen", False):
                base = Path(sys.executable).resolve().parent
            else:
                base = Path(__file__).resolve().parent
            (base / "sau_crash.log").write_text(
                "".join(traceback.format_exception(exc_type, exc, tb)),
                encoding="utf-8",
            )
        except OSError:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def main() -> int:
    _install_crash_hook()
    from sau.cli import main as cli_main

    argv = sys.argv[1:]
    if not argv:
        argv = ["gui"]  # 双击 exe 直接进图形界面
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
