"""游戏库枚举——不手输 AppID，自动获取账户拥有的游戏。

三种来源（`mode`）：
- installed：解析本地 libraryfolders.vdf + appmanifest_*.acf（离线、瞬时，仅已安装）
- owned：    全 AppID 区间 BIsSubscribedApp 所有权扫描（免 key，约 10 秒/40 万区间，
             结果含大量 Steam 内部应用与免费 DLC，配合成就扫描过滤出"真游戏"）
- webapi：   IPlayerService/GetOwnedGames（需用户自己的 Web API key，最权威）

名称解析优先级：appmanifest（已安装）> GetAppList 缓存（api.steampowered.com 免 key）
> Web API > "AppID xxx（名称未知）"。
成就支持扫描：对每个 owned AppID 临时 init + GetNumAchievements（约 90ms/个），
结果连同所有权/名称一起缓存到 sdk/library_cache.json。
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import steamapi
from .steamapi import app_root
from .service import ServiceError, count_achievements_subprocess, run_sau_json

APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
SCAN_APPID = 480  # Spacewar：Valve 官方测试应用，任何账户都可初始化
MIN_MEANINGFUL_APPID = 10  # 0-9 是 Steam 客户端内部应用，默认过滤
DEFAULT_MAX_APPID = 400_000
APPLIST_CACHE_MAX_AGE = 30 * 24 * 3600.0
APPLIST_FAILURE_COOLDOWN = 3600.0  # 获取失败后 1 小时内不再撞网络，直接用旧缓存

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = app_root() / "sdk"
APPLIST_CACHE = CACHE_DIR / "applist_cache.json"
LIBRARY_CACHE = CACHE_DIR / "library_cache.json"


class LibraryError(Exception):
    """游戏库枚举错误（信息面向用户）。"""


@dataclass
class LibraryEntry:
    appid: int
    name: str
    installed: bool = False
    owned: bool | None = None  # installed 模式下未知
    achievements: int | None = None  # 未扫描时为 None


@dataclass
class LibraryCache:
    steam_id: int | None = None
    scanned_at: float = 0.0
    owned: list[int] = field(default_factory=list)
    achievements: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "steam_id": self.steam_id,
            "scanned_at": self.scanned_at,
            "owned": self.owned,
            "achievements": self.achievements,
            "names": self.names,
        }

    @classmethod
    def from_json(cls, data: dict) -> "LibraryCache":
        return cls(
            steam_id=data.get("steam_id"),
            scanned_at=float(data.get("scanned_at") or 0),
            owned=[int(x) for x in data.get("owned") or []],
            achievements={int(k): int(v) for k, v in (data.get("achievements") or {}).items()},
            names={int(k): str(v) for k, v in (data.get("names") or {}).items()},
        )


def load_library_cache() -> LibraryCache:
    try:
        return LibraryCache.from_json(json.loads(LIBRARY_CACHE.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return LibraryCache()


def _atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再原子替换：进程中断也不留半个缓存文件。

    全库成就扫描可达数十分钟、每扫完一个游戏就落盘一次——非原子写入时
    中途被杀/断电会把整个缓存毁成半截 JSON，前功尽弃。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_library_cache(cache: LibraryCache) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _atomic_write_text(LIBRARY_CACHE, json.dumps(cache.to_json(), ensure_ascii=False))


# ---- Steam 安装目录与已安装游戏 --------------------------------------------


def find_steam_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
            return Path(value)
    except OSError:
        pass
    for cand in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        path = Path(cand)
        if path.exists():
            return path
    return None


def library_paths(steam_dir: Path) -> list[Path]:
    """解析 libraryfolders.vdf 得到全部库目录（含主目录）。"""
    paths = [steam_dir]
    vdf = steam_dir / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            path = Path(raw.replace("\\\\", "\\"))
            if path not in paths:
                paths.append(path)
    return paths


def _unescape_vdf(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def installed_apps(steam_dir: Path | None) -> dict[int, str]:
    """已安装应用：遍历各库目录的 appmanifest_*.acf。"""
    result: dict[int, str] = {}
    if steam_dir is None:
        return result
    for lib in library_paths(steam_dir):
        for manifest in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid_m = re.search(r'"appid"\s+"(\d+)"', text)
            name_m = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', text)
            if not appid_m:
                continue
            appid = int(appid_m.group(1))
            result[appid] = _unescape_vdf(name_m.group(1)) if name_m else f"AppID {appid}"
    return result


# ---- 名称缓存（GetAppList，免 key） ----------------------------------------


def fetch_app_names(refresh: bool = False, http_get=None) -> dict[int, str]:
    """公开 GetAppList（无需 API key），缓存 30 天；失败有 1 小时冷却期。

    冷却期内（或网络不可用时）直接返回旧缓存，避免每次打开对话框都长时间等待。
    """
    def _cached() -> dict:
        if APPLIST_CACHE.exists():
            try:
                return json.loads(APPLIST_CACHE.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return {}
        return {}

    if not refresh:
        data = _cached()
        names = {int(k): str(v) for k, v in data.get("names", {}).items()}
        if names and time.time() - float(data.get("fetched_at") or 0) < APPLIST_CACHE_MAX_AGE:
            return names
        if time.time() - float(data.get("last_failed_at") or 0) < APPLIST_FAILURE_COOLDOWN:
            return names  # 冷却期内不撞网络
    getter = http_get or _http_get
    last_exc: Exception | None = None
    names: dict[int, str] = {}
    for attempt in range(3):  # 该响应较大（约 25MB），网络抖动时重试
        try:
            raw = getter(APPLIST_URL, timeout=120)
            data = json.loads(raw)
            names = {
                int(app["appid"]): str(app.get("name") or "")
                for app in data["applist"]["apps"]
                if app.get("name")
            }
            break
        except (OSError, ValueError, KeyError) as exc:
            last_exc = exc
            time.sleep(1.0 + attempt)
    else:
        old = _cached()
        # 记录失败时间（冷却期起点），保留旧名称
        CACHE_DIR.mkdir(exist_ok=True)
        _atomic_write_text(
            APPLIST_CACHE,
            json.dumps({"fetched_at": old.get("fetched_at") or 0,
                        "last_failed_at": time.time(),
                        "names": old.get("names") or {}}, ensure_ascii=False),
        )
        if old.get("names"):
            return {int(k): str(v) for k, v in old["names"].items()}
        raise LibraryError(f"获取应用名称列表失败（{last_exc}）；已安装的游戏名称不受影响")
    CACHE_DIR.mkdir(exist_ok=True)
    _atomic_write_text(
        APPLIST_CACHE,
        json.dumps({"fetched_at": time.time(), "names": {str(k): v for k, v in names.items()}},
                   ensure_ascii=False),
    )
    return names


def _http_get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sau/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


# ---- 离线名称源：appcache/appinfo.vdf ---------------------------------------
#
# v0x29（现行格式，逆向自真实文件，见开发清单坑 AG）：
#   记录 = (appid u32 + 常量 00 03 00 00 00 01) 开头，随后若干条目
#   (0x01, u32 字符串索引, 内联 cstring)。名称 = 第一条非 40 位十六进制哈希的
#   内联字符串（前几条通常是 librarycache 封面哈希）。
# 旧 0x28：完整二进制 VDF 解析。
# 任何解析失败都静默降级为空——绝不产出错误名称。


def parse_appinfo_names(steam_dir: Path | None, wanted: set[int] | None = None) -> dict[int, str]:
    """从 appcache/appinfo.vdf 提取 {appid: 名称}。

    v0x29 需要传入 wanted（要查询的 appid 集合，按需定位记录）；
    旧 0x28 全量解析。返回的名称已过滤封面哈希等噪声。
    """
    if steam_dir is None:
        return {}
    path = Path(steam_dir) / "appcache" / "appinfo.vdf"
    try:
        data = path.read_bytes()
    except OSError:
        return {}
    if len(data) < 12:
        return {}
    magic = int.from_bytes(data[:4], "little")
    if magic == 0x07564429:
        return _names_v29(data, wanted or set())
    if magic == 0x07564428:
        return _names_v28(data)
    return {}


_HEX40 = re.compile(r"[0-9a-f]{40}(_[0-9a-zA-Z]+)?\Z")  # 封面哈希（含 _thumb 等变体）
# 记录头：appid u32 之后紧跟 00 03 00 00 00（随后是首条目的 0x01 类型字节）
_V29_RECORD_HEAD = b"\x00\x03\x00\x00\x00"


def _names_v29(data: bytes, wanted: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    for appid in sorted(wanted):
        pattern = struct.pack("<I", appid) + _V29_RECORD_HEAD
        pos = data.find(pattern)
        for _try in range(3):  # 极少数情况下模式会撞进其他数据，换下一个出现位置
            if pos < 0:
                break
            name = _v29_name_from_record(data, pos + len(pattern))
            if name is not None:
                names[appid] = name
                break
            pos = data.find(pattern, pos + 1)
    return names


def _v29_name_from_record(data: bytes, pos: int) -> str | None:
    """收集记录内非哈希字符串条目，返回字符串表索引最小者。

    显示名在字符串表中占据最小槽位（真机验证 10/10：TF2/CS2/Dota2/TV2 等
    名称条目 idx 均为 4，而封面哈希/URL slug 是 300+ 的大索引）。
    """
    n = len(data)
    candidates: list[tuple[int, str]] = []
    entries = 0
    while pos + 5 <= n and entries < 24:
        if data[pos] != 0x01:
            break  # 非字符串条目 → 条目序列结束
        index = int.from_bytes(data[pos + 1 : pos + 5], "little")
        end = data.find(b"\x00", pos + 5, pos + 5 + 4096)
        if end < 0:
            break
        text = data[pos + 5 : end].decode("utf-8", "replace")
        pos = end + 1
        entries += 1
        if text and not _HEX40.match(text):
            candidates.append((index, text))
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[0])[1]


def _names_v28(data: bytes) -> dict[int, str]:
    """旧 0x28 格式：完整解析，只取 common 段的 name。"""
    pos = 8  # magic + universe
    total = len(data)
    names: dict[int, str] = {}
    while pos + 8 <= total:
        appid = int.from_bytes(data[pos : pos + 4], "little")
        if appid == 0:
            break
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body_start = pos + 8
        body_end = min(body_start + size, total)
        if body_end <= body_start:
            break
        name = _appinfo_record_name(data, body_start, body_end)
        if name:
            names[appid] = name
        pos = body_end
    return names


def _appinfo_record_name(data: bytes, start: int, end: int) -> str | None:
    """在单条 0x28 记录里找 common 段的 name；失败返回 None。"""
    pos = start + 4 + 8 + 8  # 跳过 state(u32) / last_updated(u64) / token(u64)
    try:
        while pos < end:
            section_type = data[pos]
            pos += 1
            if section_type == 0x08:
                break
            name_end = data.find(b"\x00", pos, end)
            if name_end < 0:
                return None
            pos = name_end + 1
            if section_type == 0x00:
                value, pos = _binary_vdf_dict(data, pos, end, key=b"name")
                return value
            pos = _skip_binary_vdf(data, pos, end)
        return None
    except (IndexError, ValueError):
        return None


def _binary_vdf_dict(data: bytes, pos: int, end: int, key: bytes | None = None):
    """解析二进制 VDF 字典。

    key=None 时返回完整 dict；给定 key 时找到即返回对应字符串（提前退出）。
    """
    result: dict[str, object] = {}
    while pos < end:
        field_type = data[pos]
        pos += 1
        if field_type == 0x08:
            return result, pos
        key_end = data.find(b"\x00", pos, end)
        if key_end < 0:
            raise ValueError("unterminated key")
        field_name = data[pos:key_end]
        pos = key_end + 1
        if field_type == 0x00:  # 子字典
            value, pos = _binary_vdf_dict(data, pos, end, key=key if key else None)
            if key is None:
                result[field_name.decode("utf-8", "replace")] = value
            elif field_name == key:
                return value, pos
        elif field_type == 0x01:  # 字符串
            str_end = data.find(b"\x00", pos, end)
            if str_end < 0:
                raise ValueError("unterminated string")
            if key is None:
                result[field_name.decode("utf-8", "replace")] = data[pos:str_end].decode(
                    "utf-8", "replace"
                )
            elif field_name == key:
                return data[pos:str_end].decode("utf-8", "replace"), str_end + 1
            pos = str_end + 1
        elif field_type == 0x02:  # u32
            if key is None:
                result[field_name.decode("utf-8", "replace")] = int.from_bytes(
                    data[pos : pos + 4], "little"
                )
            pos += 4
        else:  # 未知类型，放弃该记录的精细解析
            raise ValueError(f"unknown b-vdf type {field_type}")
    raise ValueError("unterminated dict")


def _skip_binary_vdf(data: bytes, pos: int, end: int) -> int:
    """跳过一个二进制 VDF 值（非 common 段时使用），返回新位置。"""
    while pos < end:
        field_type = data[pos]
        pos += 1
        if field_type == 0x08:
            return pos
        key_end = data.find(b"\x00", pos, end)
        if key_end < 0:
            raise ValueError("unterminated key")
        pos = key_end + 1
        if field_type == 0x00:
            pos = _skip_binary_vdf(data, pos, end)
        elif field_type == 0x01:
            str_end = data.find(b"\x00", pos, end)
            if str_end < 0:
                raise ValueError("unterminated string")
            pos = str_end + 1
        elif field_type == 0x02:
            pos += 4
        elif field_type == 0x03:
            pos += 4
        else:
            raise ValueError(f"unknown b-vdf type {field_type}")
    raise ValueError("unterminated dict")


# ---- 所有权扫描与成就支持扫描 ----------------------------------------------


def scan_owned(bindings_factory, max_appid: int = DEFAULT_MAX_APPID,
               progress=None) -> list[int]:
    """BIsSubscribedApp 全区间扫描，返回拥有的 AppID（升序）。

    在 AppID 480（Spacewar）上下文中执行一次 init，整个扫描复用同一会话。
    """
    bindings = bindings_factory(_default_dll_or_error())
    owned: list[int] = []
    try:
        with steamapi.appid_cwd(SCAN_APPID):
            bindings.init()
            try:
                if bindings.is_subscribed(SCAN_APPID) is None:
                    raise LibraryError(
                        "该 DLL 不支持 ISteamApps/BIsSubscribedApp，无法做所有权扫描；"
                        "请改用 --mode webapi 或 installed"
                    )
                total = max_appid + 1
                for appid in range(0, total):
                    if bindings.is_subscribed(appid):
                        owned.append(appid)
                    if progress is not None and appid % 20000 == 0:
                        progress(appid, total)
            finally:
                bindings.shutdown()
    except steamapi.SteamApiError as exc:
        raise LibraryError(str(exc)) from exc
    return owned


def scan_achievements(appids: list[int], progress=None, on_result=None,
                      dll: str | None = None, runner=None) -> dict[int, int | None]:
    """逐个应用查询成就数量，返回 {appid: 数量}（查询失败为 None）。

    每个应用一个独立子进程：同进程内重复 SteamAPI_Init 切换 appid 后，
    ISteamUserStats 会继续返回第一个应用的 schema（真机实测，见开发清单坑 AA）。
    `runner(appid) -> int|None` 供测试注入。
    """
    query = runner or (lambda appid: count_achievements_subprocess(appid, dll))
    results: dict[int, int | None] = {}
    total = len(appids)
    for index, appid in enumerate(appids, start=1):
        try:
            count = query(appid)
        except Exception:
            count = None
        results[appid] = count
        if on_result is not None:
            on_result(appid, count)
        if progress is not None:
            progress(index, total)
    return results


def scan_owned_subprocess(max_appid: int = DEFAULT_MAX_APPID,
                          dll: str | None = None) -> list[int]:
    """独立子进程执行所有权扫描（GUI 进程内绝不 SteamAPI_Init，避免 schema 串台）。"""
    args = ["_scan-owned", "--max-appid", str(max_appid)]
    if dll:
        args = ["--dll", dll, *args]
    data = run_sau_json(args)
    if data is None or data.get("error"):
        raise LibraryError(
            f"所有权扫描子进程失败：{(data or {}).get('error') or '无 JSON 结果'}"
        )
    return [int(x) for x in data.get("owned") or []]


def steam_id_subprocess(dll: str | None = None) -> int | None:
    """独立子进程读取当前账户 SteamID；失败返回 None。"""
    args = ["_steam-id"]
    if dll:
        args = ["--dll", dll, *args]
    data = run_sau_json(args)
    if data is None or data.get("error"):
        return None
    return data.get("steam_id")


# ---- 缩略图：本地 librarycache → Steam CDN → PNG 缓存 -----------------------
#
# Tk 8.6 的 PhotoImage 只认 PNG/GIF，而 Steam 的封面是 JPG——转换/缩放用
# PowerShell System.Drawing 批量完成（Windows 自带，无需 Pillow）。


THUMB_WIDTH, THUMB_HEIGHT = 92, 43  # header.jpg 460x215 的 1/5，保持比例


def iter_local_thumbnails(steam_dir: Path | None, appids) -> dict[int, Path]:
    """在 appcache/librarycache/<appid>/ 里找头图（优先 library_header.jpg）。"""
    result: dict[int, Path] = {}
    if steam_dir is None:
        return result
    lib = Path(steam_dir) / "appcache" / "librarycache"
    if not lib.is_dir():
        return result
    for appid in appids:
        folder = lib / str(appid)
        if not folder.is_dir():
            continue
        for name in ("library_header.jpg", "header.jpg"):
            candidate = folder / name
            if candidate.exists():
                result[appid] = candidate
                break
        else:
            for candidate in sorted(folder.glob("*.jpg")):
                result[appid] = candidate
                break
    return result


def build_thumbnail_cache(steam_dir: Path | None, appids, dest_dir=None,
                          cdn_appids=(), progress=None, http_get=None) -> dict[int, Path]:
    """生成交付用缩略图 PNG 到 dest_dir（默认 sdk/thumbs），返回 {appid: png_path}。

    来源：本地 librarycache 封面 → cdn_appids 里仍缺失的走 Steam CDN header.jpg。
    """
    if steam_dir is None:
        steam_dir = find_steam_dir()
    dest = Path(dest_dir) if dest_dir else app_root() / "sdk" / "thumbs"
    dest.mkdir(parents=True, exist_ok=True)
    appids = list(appids)
    existing = {a: dest / f"{a}.png" for a in appids if (dest / f"{a}.png").exists()}
    todo = dict(iter_local_thumbnails(steam_dir, appids))
    cdn_set = set(cdn_appids)
    cdn_done = 0
    for appid in appids:
        if appid in todo or appid in existing or appid not in cdn_set:
            continue
        if http_get is None and cdn_done >= 400:  # CDN 兜底量上限，防止全库下载
            break
        dest_jpg = dest / f"{appid}_cdn.jpg"
        try:
            getter = http_get or _http_get
            payload = getter(
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
                timeout=30,
            )
        except OSError:
            continue
        cdn_done += 1
        if len(payload) > 1024:
            dest_jpg.write_bytes(payload)
            todo[appid] = dest_jpg
        if progress is not None:
            progress(len(todo), len(appids))
    jobs = [
        (str(src), str(dest / f"{appid}.png"))
        for appid, src in todo.items()
        if not (dest / f"{appid}.png").exists()
    ]
    _convert_png_batch(jobs, dest)
    return {appid: dest / f"{appid}.png" for appid in todo if (dest / f"{appid}.png").exists()}


def _convert_png_batch(jobs, dest_dir: Path) -> int:
    """PowerShell System.Drawing 批量把 JPG 缩放为 PNG。"""
    if not jobs:
        return 0
    import subprocess as sp

    ps1 = dest_dir / "_convert.ps1"
    lines = [
        "Add-Type -AssemblyName System.Drawing",
        "$ok = 0",
    ]
    for src, dst in jobs:
        src_e = str(src).replace("'", "''")
        dst_e = str(dst).replace("'", "''")
        lines.append(
            "try {"
            f"\n  $img = [System.Drawing.Image]::FromFile('{src_e}')"
            f"\n  $thumb = New-Object System.Drawing.Bitmap {THUMB_WIDTH},{THUMB_HEIGHT}"
            "\n  $g = [System.Drawing.Graphics]::FromImage($thumb)"
            "\n  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic"
            f"\n  $g.DrawImage($img, 0, 0, {THUMB_WIDTH}, {THUMB_HEIGHT})"
            "\n  $g.Dispose(); $img.Dispose()"
            f"\n  $thumb.Save('{dst_e}', [System.Drawing.Imaging.ImageFormat]::Png)"
            "\n  $thumb.Dispose(); $ok++"
            "\n } catch { Write-Output ('FAIL ' + $_.Exception.Message) }"
        )
    lines.append("Write-Output ('OKCOUNT ' + $ok)")
    ps1.write_text("\n".join(lines), encoding="utf-8-sig")
    try:
        # 不用 text=True：PowerShell 输出按控制台代码页（GBK）编码，按字节拿回来
        # 再容错解码，避免 UnicodeDecodeError 把整个转换结果吞掉。
        proc = sp.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, timeout=900,
        )
    except (OSError, sp.TimeoutExpired):
        return 0
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    for line in stdout.splitlines():
        if line.startswith("FAIL"):
            print(line, file=sys.stderr)
    match = re.search(r"OKCOUNT (\d+)", stdout)
    return int(match.group(1)) if match else 0


def _default_dll_or_error():
    dll = steamapi.default_dll_path()
    if dll is None:
        raise LibraryError(
            "未找到 steam_api DLL（--dll / SAU_STEAM_API_DLL / sdk/steam_api64.dll）"
        )
    return dll


# ---- Web API（需用户自己的 key） -------------------------------------------


def webapi_owned_games(api_key: str, steam_id: int, http_get=None) -> dict[int, str]:
    """IPlayerService/GetOwnedGames：返回 {appid: name}（最权威的全库来源）。"""
    query = urllib.parse.urlencode({
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
    })
    getter = http_get or _http_get
    try:
        data = json.loads(getter(f"{OWNED_GAMES_URL}?{query}", timeout=30))
    except (OSError, ValueError) as exc:
        raise LibraryError(f"Web API 请求失败：{exc}") from exc
    response = data.get("response") or {}
    if not response and isinstance(data.get("error"), str):
        raise LibraryError(f"Web API 拒绝请求：{data['error']}")
    games = response.get("games") or []
    return {int(g["appid"]): str(g.get("name") or f"AppID {g['appid']}") for g in games}


# ---- 汇总 ------------------------------------------------------------------


def build_library(
    mode: str,
    steam_dir: Path | None = None,
    api_key: str | None = None,
    steam_id: int | None = None,
    with_achievements: bool = False,
    refresh: bool = False,
    max_appid: int = DEFAULT_MAX_APPID,
    include_system_apps: bool = False,
    progress=None,
    dll: str | None = None,
) -> tuple[list[LibraryEntry], LibraryCache]:
    """按 mode 构建游戏库条目列表，并同步维护缓存。

    所有 Steam 交互都通过子进程完成（GUI 进程内不做任何 SteamAPI_Init，
    避免同进程切换 AppID 的 schema 串台问题）。
    progress(done, total) 用于 CLI/GUI 进度显示；不同阶段调用时 total 语义不同。
    """
    steam_dir = steam_dir if steam_dir is not None else find_steam_dir()
    installed = installed_apps(steam_dir)
    cache = load_library_cache()

    if mode == "installed":
        entries = [
            LibraryEntry(appid=appid, name=name, installed=True, owned=None)
            for appid, name in sorted(installed.items())
        ]
    elif mode == "webapi":
        if not api_key:
            raise LibraryError("webapi 模式需要 --api-key（在 steamcommunity.com/dev/api-key 免费申请）")
        if not steam_id:
            steam_id = steam_id_subprocess(dll=dll)
            if not steam_id:
                raise LibraryError("无法获取 SteamID（该 DLL 缺少 ISteamUser/GetSteamID）")
        names = webapi_owned_games(api_key, steam_id)
        cache.steam_id, cache.scanned_at = steam_id, time.time()
        cache.owned = sorted(names)
        cache.names.update({appid: name for appid, name in names.items()})
        save_library_cache(cache)
        entries = [
            LibraryEntry(appid=appid, name=names.get(appid, f"AppID {appid}"),
                         installed=appid in installed, owned=True)
            for appid in sorted(names)
        ]

    elif mode == "owned":
        cached_owned = cache.owned if (cache.owned and cache.steam_id and not refresh) else None
        if cached_owned:
            owned = cached_owned
        else:
            owned = scan_owned_subprocess(max_appid=max_appid, dll=dll)
            cache.owned = owned
            cache.scanned_at = time.time()
            cache.steam_id = steam_id_subprocess(dll=dll)
        # 名称链（按优先级）：本地 appmanifest → appinfo.vdf（离线，全库覆盖）→
        # GetAppList（Valve 已下线该接口，仅存量缓存可用）→ "AppID xxx" 兜底
        cache.names.update({appid: name for appid, name in installed.items() if appid in owned})
        try:
            info_names = parse_appinfo_names(steam_dir, set(owned))
            cache.names.update({a: n for a, n in info_names.items() if a in owned and n})
        except Exception:
            pass  # 名称解析失败不影响游戏库本身
        missing_names = [a for a in owned if a not in cache.names]
        if refresh or missing_names:
            try:
                app_names = fetch_app_names(refresh=refresh)
                cache.names.update({a: app_names[a] for a in owned if a in app_names})
            except LibraryError:
                pass  # 名称拿不全时退回 AppID 显示
        save_library_cache(cache)
        entries = [
            LibraryEntry(
                appid=appid,
                name=cache.names.get(appid) or installed.get(appid) or f"AppID {appid}",
                installed=appid in installed,
                owned=True,
            )
            for appid in owned
            if include_system_apps or appid >= MIN_MEANINGFUL_APPID
        ]

    else:
        raise LibraryError(f"未知的游戏库模式：{mode}")

    _apply_cached_fields(entries, cache)  # 先应用缓存，成就扫描只补缺失的
    if with_achievements:
        _scan_missing_achievements(entries, cache, progress, dll=dll, refresh=refresh)
        save_library_cache(cache)
    return entries, cache


def _scan_missing_achievements(entries, cache: LibraryCache, progress=None,
                               dll: str | None = None, refresh: bool = False) -> None:
    if refresh:  # 显式刷新时，连之前查询失败的条目也重试
        for appid in [a for a, v in cache.achievements.items() if v < 0]:
            del cache.achievements[appid]
    # todo = 从未查询过的；-1（查询失败）默认跳过——多数是 DLC/组件类条目，
    # 每次重试要浪费约 30 分钟，只有 --refresh 才重试。
    todo = [e.appid for e in entries if e.appid not in cache.achievements]
    if not todo:
        return
    counts = scan_achievements(
        todo, progress=progress,
        on_result=lambda appid, count: _save_achievement_result(cache, appid, count),
        dll=dll,
    )
    for appid, count in counts.items():
        cache.achievements[appid] = count if count is not None else -1
    for entry in entries:
        cached = cache.achievements.get(entry.appid)
        if cached is not None:
            entry.achievements = None if cached < 0 else cached


def _save_achievement_result(cache: LibraryCache, appid: int, count: int | None) -> None:
    """增量落盘：全库成就扫描可达数十分钟，中断不应丢失已扫结果。"""
    cache.achievements[appid] = count if count is not None else -1
    save_library_cache(cache)


def _apply_cached_fields(entries: list[LibraryEntry], cache: LibraryCache) -> None:
    for entry in entries:
        cached = cache.achievements.get(entry.appid)
        if cached is not None:
            entry.achievements = None if cached < 0 else cached
        cached_name = cache.names.get(entry.appid)
        if cached_name:
            entry.name = cached_name


def _detect_steam_id(factory) -> int:
    """在 480 上下文中读取当前账户 SteamID。"""
    bindings = factory(_default_dll_or_error())
    try:
        with steamapi.appid_cwd(SCAN_APPID):
            bindings.init()
            try:
                steam_id = bindings.steam_id()
            finally:
                bindings.shutdown()
    except steamapi.SteamApiError as exc:
        raise LibraryError(str(exc)) from exc
    if not steam_id:
        raise LibraryError("无法获取 SteamID（该 DLL 缺少 ISteamUser/GetSteamID）")
    return steam_id


def _bindings_factory_or_error():
    def factory(dll_path):
        return steamapi.FlatBindings.load(dll_path)

    return factory


def games_with_achievements(entries: list[LibraryEntry]) -> list[LibraryEntry]:
    return [e for e in entries if (e.achievements or 0) > 0]
