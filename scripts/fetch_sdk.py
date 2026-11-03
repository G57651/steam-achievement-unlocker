"""下载 Steamworks 可再发行组件 steam_api64.dll 及 SDK 头文件到 sdk/ 目录。

官方渠道 partner.steamgames.com 需要合作伙伴账号登录，无法脚本化下载；
本脚本默认从 Steamworks.NET 仓库（广泛使用的 Steamworks C# 绑定，随仓库
分发了 Valve 官方 redist DLL 与 CodeGen 用的 SDK 头文件）拉取，并用 sha256
校验完整性。

用法：
  python scripts/fetch_sdk.py                  # 下载并校验（已知版本）
  python scripts/fetch_sdk.py --url ... --sha256 ...   # 自定义来源
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_DIR = PROJECT_ROOT / "sdk"

# rlabrecque/Steamworks.NET @ master（2026-10 验证过的可用源）
DEFAULT_DLL_URL = (
    "https://raw.githubusercontent.com/rlabrecque/Steamworks.NET"
    "/master/com.rlabrecque.steamworks.net/Plugins/steam_api64.dll"
)
DEFAULT_DLL_SHA256 = "e6d9bafb9a41e42fba7b21553db49f8719027af3f0deb23ff86cfc44d60e776d"
HEADER_BASE = (
    "https://raw.githubusercontent.com/rlabrecque/Steamworks.NET"
    "/master/CodeGen/steam"
)
HEADERS = ["steam_api_flat.h", "isteamuserstats.h", "steam_api.h", "steamtypes.h"]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    print(f"  -> {dest}（{dest.stat().st_size} 字节）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll-url", default=DEFAULT_DLL_URL)
    parser.add_argument("--sha256", default=DEFAULT_DLL_SHA256)
    parser.add_argument("--skip-headers", action="store_true")
    args = parser.parse_args()

    SDK_DIR.mkdir(exist_ok=True)
    dll_path = SDK_DIR / "steam_api64.dll"
    try:
        download(args.dll_url, dll_path)
    except OSError as exc:
        print(f"下载失败：{exc}\n可手动下载后放到 {dll_path}", file=sys.stderr)
        return 1

    actual = sha256_of(dll_path)
    print(f"sha256 = {actual}")
    if args.sha256 and actual != args.sha256.lower():
        print("校验失败：哈希与预期不符！拒绝使用，请检查下载来源。", file=sys.stderr)
        dll_path.unlink()
        return 1
    print("校验通过")

    if not args.skip_headers:
        header_dir = SDK_DIR / "steam"
        header_dir.mkdir(exist_ok=True)
        for name in HEADERS:
            try:
                download(f"{HEADER_BASE}/{name}", header_dir / name)
            except OSError as exc:
                print(f"头文件 {name} 下载失败（不影响运行）：{exc}", file=sys.stderr)

    # 下载后立即用 peinfo 做一次预检
    sys.path.insert(0, str(PROJECT_ROOT))
    from sau import peinfo

    info = peinfo.inspect(dll_path)
    if info is None:
        print("警告：下载的文件无法解析为 PE！", file=sys.stderr)
        return 1
    print(f"预检：{info['machine_name']}（{info['bits']} 位），导出 {len(info['exports'] or [])} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
