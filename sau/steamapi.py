"""steam_api(64).dll 的 ctypes 绑定（Steamworks flat C API）。

导出名与签名依据：
- Steamworks SDK steam_api_flat.h / steam_api.h / isteamuserstats.h（SDK 1.60 系）；
- 与真实 steam_api64.dll 的导出表逐项核对（sau.peinfo）。

兼容性设计（很重要，SDK 版本差异极大）：
- 初始化：依次探测 SteamAPI_InitFlat / SteamAPI_InitEx / SteamAPI_Init /
  SteamAPI_InitSafe；前两者返回 ESteamAPIInitResult（0=OK）并写 SteamErrMsg，
  后两者是旧式 bool。注意 SteamAPI_InitEx 在头文件中是 inline 包装、部分 DLL
  没有该导出，所以不能只认它。
- ISteamUserStats 获取：优先访问器 SteamAPI_ISteamUserStats()；很多 DLL 没有
  这个导出，改用 SteamInternal_FindOrCreateUserInterface(hUser, 版本串) 逐版本
  探测（当前 SDK 为 013，向下探到 010）。
- 除核心 6 个导出外全部视为可选，缺失时降级（例如 RequestCurrentStats 已废弃、
  旧接口没有 GetNumAchievements 枚举）。
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import tempfile
from ctypes import CDLL, POINTER, c_bool, c_char, c_char_p, c_int, c_uint32, c_uint64, c_void_p, create_string_buffer
from pathlib import Path

# isteamuserstats.h：当前 SDK 为 013（官方文档页面还写着 011，不能信文档）。
STEAMUSERSTATS_VERSIONS = [
    "STEAMUSERSTATS_INTERFACE_VERSION013",
    "STEAMUSERSTATS_INTERFACE_VERSION012",
    "STEAMUSERSTATS_INTERFACE_VERSION011",
    "STEAMUSERSTATS_INTERFACE_VERSION010",
]

# 账户身份与所有权（游戏库枚举用）。版本串以 DLL 内嵌字符串为准（SteamUser023、
# STEAMAPPS_INTERFACE_VERSION009），旧 DLL 逐级向下探测。
STEAMUSER_VERSIONS = [
    "SteamUser023", "SteamUser022", "SteamUser021", "SteamUser020",
    "SteamUser019", "SteamUser018", "SteamUser017", "SteamUser016",
    "SteamUser015", "SteamUser014", "SteamUser013",
    "STEAMUSER_INTERFACE_VERSION001",
]
STEAMAPPS_VERSIONS = [
    "STEAMAPPS_INTERFACE_VERSION009", "STEAMAPPS_INTERFACE_VERSION008",
    "STEAMAPPS_INTERFACE_VERSION007", "STEAMAPPS_INTERFACE_VERSION006",
    "STEAMAPPS_INTERFACE_VERSION005", "STEAMAPPS_INTERFACE_VERSION001",
]

# ESteamAPIInitResult（steam_api.h）
INIT_OK = 0
INIT_NO_STEAM_CLIENT = 2
INIT_RESULT_NAMES = {
    0: "OK",
    1: "FailedGeneric（通用失败，如未拥有该应用）",
    2: "NoSteamClient（Steam 未运行或未登录）",
    3: "VersionMismatch（Steam 客户端过旧）",
}

ERRMSG_SIZE = 1024

# 缺任何一个都无法完成"解锁成就"这一最小闭环。
REQUIRED_EXPORTS = (
    "SteamAPI_ISteamUserStats_GetAchievement",
    "SteamAPI_ISteamUserStats_SetAchievement",
    "SteamAPI_ISteamUserStats_StoreStats",
    "SteamAPI_Shutdown",
    "SteamAPI_RunCallbacks",
)

_OPTIONAL_EXPORTS = (
    "SteamAPI_InitFlat",
    "SteamAPI_InitEx",
    "SteamAPI_Init",
    "SteamAPI_InitSafe",
    "SteamInternal_SteamAPI_Init",
    "SteamAPI_ISteamUserStats",
    "SteamAPI_GetHSteamUser",
    "SteamInternal_FindOrCreateUserInterface",
    "SteamAPI_ISteamUserStats_RequestCurrentStats",
    "SteamAPI_ISteamUserStats_GetNumAchievements",
    "SteamAPI_ISteamUserStats_GetAchievementName",
    "SteamAPI_ISteamUserStats_GetAchievementDisplayAttribute",
    "SteamAPI_ISteamUser",
    "SteamAPI_ISteamUser_GetSteamID",
    "SteamAPI_ISteamApps",
    "SteamAPI_ISteamApps_BIsSubscribedApp",
    "SteamAPI_ISteamApps_BIsAppInstalled",
)

# restype, argtypes —— 与 flat 头逐条核对过的签名。
_SIGNATURES = {
    "SteamAPI_InitFlat": (c_int, [POINTER(c_char)]),
    "SteamAPI_InitEx": (c_int, [POINTER(c_char)]),
    "SteamAPI_Init": (c_bool, []),
    "SteamAPI_InitSafe": (c_bool, []),
    "SteamInternal_SteamAPI_Init": (c_int, [c_char_p, POINTER(c_char)]),
    "SteamAPI_Shutdown": (None, []),
    "SteamAPI_RunCallbacks": (None, []),
    "SteamAPI_GetHSteamUser": (c_int, []),
    "SteamInternal_FindOrCreateUserInterface": (c_void_p, [c_int, c_char_p]),
    "SteamAPI_ISteamUserStats": (c_void_p, []),
    "SteamAPI_ISteamUserStats_RequestCurrentStats": (c_bool, [c_void_p]),
    "SteamAPI_ISteamUserStats_GetNumAchievements": (c_int, [c_void_p]),
    "SteamAPI_ISteamUserStats_GetAchievementName": (c_char_p, [c_void_p, c_int]),
    "SteamAPI_ISteamUserStats_GetAchievement": (c_bool, [c_void_p, c_char_p, POINTER(c_bool)]),
    "SteamAPI_ISteamUserStats_SetAchievement": (c_bool, [c_void_p, c_char_p]),
    "SteamAPI_ISteamUserStats_StoreStats": (c_bool, [c_void_p]),
    "SteamAPI_ISteamUserStats_GetAchievementDisplayAttribute": (
        c_char_p,
        [c_void_p, c_char_p, c_char_p],
    ),
    "SteamAPI_ISteamUser": (c_void_p, []),
    "SteamAPI_ISteamUser_GetSteamID": (c_uint64, [c_void_p]),
    "SteamAPI_ISteamApps": (c_void_p, []),
    "SteamAPI_ISteamApps_BIsSubscribedApp": (c_bool, [c_void_p, c_uint32]),
    "SteamAPI_ISteamApps_BIsAppInstalled": (c_bool, [c_void_p, c_uint32]),
}


class SteamApiError(Exception):
    """steam_api 调用层错误。"""


class SteamNotRunning(SteamApiError):
    """Steam 客户端未运行 / 初始化失败。"""


class ExportMissing(SteamApiError):
    """DLL 缺少必需导出，不能用于解锁成就。"""


def _python_bits() -> int:
    import struct

    return struct.calcsize("P") * 8


def app_root() -> Path:
    """程序根目录：源码运行 = 项目根；PyInstaller 单文件 = exe 所在目录。

    sdk/（steam_api64.dll）与各类缓存都放在这个目录下，保证打包后可携带。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_dll_path() -> Path | None:
    """按优先级解析默认的 steam_api64.dll 路径。"""
    env = os.environ.get("SAU_STEAM_API_DLL")
    if env:
        return Path(env)
    local = app_root() / "sdk" / "steam_api64.dll"
    if local.exists():
        return local
    return None


class FlatBindings:
    """加载一个 steam_api DLL 并暴露本工具用到的 flat C API。

    `load()` 的 loader 参数可注入（测试用假 DLL），调用面与服务层解耦。
    """

    def __init__(self, exports: dict):
        self._exports = exports
        self._initialized = False

    @classmethod
    def load(cls, dll_path, loader=None) -> "FlatBindings":
        path = Path(dll_path)
        if loader is None:
            # 只对真实加载检查存在性；注入的测试 loader 自行决定如何处理路径。
            if not path.exists():
                raise SteamApiError(f"找不到 steam_api DLL：{path}")
        loader = loader or _default_loader
        try:
            dll = loader(str(path))
        except OSError as exc:
            raise SteamApiError(
                f"无法加载 {path}（{exc}）。最常见原因是 Python（{_python_bits()} 位）"
                f"与 DLL 位数不一致，请改用匹配位数的 DLL 或 Python。"
            ) from exc
        available = {}
        missing = [name for name in REQUIRED_EXPORTS if getattr(dll, name, None) is None]
        if missing:
            raise ExportMissing(
                f"{path.name} 缺少必需导出：{', '.join(missing)}。"
                f"该 DLL 可能过旧（需要带 flat C API 的 Steamworks SDK 1.46+）。"
            )
        for name in REQUIRED_EXPORTS + _OPTIONAL_EXPORTS:
            fn = getattr(dll, name, None)
            if fn is None:
                continue
            signature = _SIGNATURES.get(name)
            if signature:
                fn.restype, fn.argtypes = signature
            available[name] = fn
        if not any(available.get(n) for n in ("SteamAPI_InitFlat", "SteamAPI_InitEx", "SteamAPI_Init", "SteamAPI_InitSafe", "SteamInternal_SteamAPI_Init")):
            raise ExportMissing(f"{path.name} 没有任何可用的初始化导出（InitFlat/InitEx/Init/InitSafe）")
        return cls(available)

    # ---- 生命周期 -------------------------------------------------------

    def init(self) -> None:
        """初始化 SteamAPI。必须在包含 steam_appid.txt 的工作目录中调用。"""
        if self._initialized:
            return
        msg = create_string_buffer(ERRMSG_SIZE)

        init_flat = self._exports.get("SteamAPI_InitFlat")
        init_ex = self._exports.get("SteamAPI_InitEx")
        init_legacy = self._exports.get("SteamAPI_Init")
        init_safe = self._exports.get("SteamAPI_InitSafe")
        init_internal = self._exports.get("SteamInternal_SteamAPI_Init")

        if init_flat is not None or init_ex is not None:
            fn = init_flat or init_ex
            rc = fn(msg)
            if rc != INIT_OK:
                raise SteamNotRunning(self._init_error(rc, msg))
            self._initialized = True
            return
        if init_internal is not None:
            versions = "\n".join(STEAMUSERSTATS_VERSIONS).encode("ascii")
            rc = init_internal(versions, msg)
            if rc != INIT_OK:
                raise SteamNotRunning(self._init_error(rc, msg))
            self._initialized = True
            return
        errors = []
        for label, legacy in (("SteamAPI_Init", init_legacy), ("SteamAPI_InitSafe", init_safe)):
            if legacy is None:
                continue
            if legacy():
                self._initialized = True
                return
            errors.append(f"{label} 返回失败")
        if errors:
            raise SteamNotRunning(
                "；".join(errors)
                + "。Steam 未运行/未登录，或当前工作目录缺少 steam_appid.txt"
            )
        raise SteamApiError("DLL 没有可用的初始化函数")

    @staticmethod
    def _init_error(rc: int, msg) -> str:
        detail = msg.value.decode("utf-8", "replace").strip()
        name = INIT_RESULT_NAMES.get(rc, f"rc={rc}")
        return f"SteamAPI 初始化失败：{name}；详情：{detail or '（无错误信息）'}"

    def shutdown(self) -> None:
        if self._initialized:
            self._exports["SteamAPI_Shutdown"]()
            self._initialized = False

    def run_callbacks(self) -> None:
        self._exports["SteamAPI_RunCallbacks"]()

    # ---- 接口获取 -------------------------------------------------------

    def _interface(self, accessor_name: str, versions: list[str]):
        """获取用户接口指针：优先访问器导出，缺失时按版本串探测。

        返回 (指针, 实际使用的版本串)；都失败返回 (None, None)。
        必须在成功 init() 之后调用（HSteamUser 句柄存于 TLS）。
        """
        accessor = self._exports.get(accessor_name)
        if accessor is not None:
            p = accessor()
            if p:
                return p, None
        get_user = self._exports.get("SteamAPI_GetHSteamUser")
        find = self._exports.get("SteamInternal_FindOrCreateUserInterface")
        if get_user is None or find is None:
            return None, None
        huser = get_user()
        for version in versions:
            p = find(huser, version.encode("ascii"))
            if p:
                return p, version
        return None, None

    def user_stats(self) -> "UserStats":
        if not self._initialized:
            raise SteamApiError("必须先成功 init() 再获取接口")
        iface, version = self._interface("SteamAPI_ISteamUserStats", STEAMUSERSTATS_VERSIONS)
        if not iface:
            raise SteamApiError(
                "无法获取 ISteamUserStats 接口（版本探测 "
                f"{STEAMUSERSTATS_VERSIONS} 均失败）"
            )
        return UserStats(iface, self._exports, version)

    def _apps_interface(self):
        if not self._initialized:
            raise SteamApiError("必须先成功 init() 再获取接口")
        return self._interface("SteamAPI_ISteamApps", STEAMAPPS_VERSIONS)

    def steam_id(self) -> int | None:
        """当前登录账户的 SteamID64；接口不可用时返回 None。"""
        get_id = self._exports.get("SteamAPI_ISteamUser_GetSteamID")
        if get_id is None:
            return None
        iface, _ = self._interface("SteamAPI_ISteamUser", STEAMUSER_VERSIONS)
        if not iface:
            return None
        return int(get_id(iface))

    def is_subscribed(self, appid: int) -> bool | None:
        """账户是否拥有该应用；ISteamApps 不可用时返回 None。"""
        fn = self._exports.get("SteamAPI_ISteamApps_BIsSubscribedApp")
        if fn is None:
            return None
        iface, _ = self._apps_interface()
        if not iface:
            return None
        return bool(fn(iface, appid))

    def is_app_installed(self, appid: int) -> bool | None:
        """该应用是否已安装；ISteamApps 不可用时返回 None。"""
        fn = self._exports.get("SteamAPI_ISteamApps_BIsAppInstalled")
        if fn is None:
            return None
        iface, _ = self._apps_interface()
        if not iface:
            return None
        return bool(fn(iface, appid))


class UserStats:
    """ISteamUserStats 的常用方法包装。所有方法调用必须与 init 同线程。"""

    def __init__(self, iface, exports: dict, version: str | None = None):
        self._iface = iface
        self._exports = exports
        self.version = version

    def request_current_stats(self) -> bool | None:
        """新版 SDK 中已废弃（自动完成）；仅当导出存在时调用。"""
        fn = self._exports.get("SteamAPI_ISteamUserStats_RequestCurrentStats")
        if fn is None:
            return None
        return bool(fn(self._iface))

    def get_num_achievements(self) -> int | None:
        """返回成就数量；DLL 过旧不支持枚举时返回 None。"""
        fn = self._exports.get("SteamAPI_ISteamUserStats_GetNumAchievements")
        if fn is None:
            return None
        return int(fn(self._iface))

    def get_achievement_names(self) -> list[str] | None:
        """返回全部成就 API 名称；不支持枚举时返回 None。"""
        num = self.get_num_achievements()
        get_name = self._exports.get("SteamAPI_ISteamUserStats_GetAchievementName")
        if num is None or get_name is None:
            return None
        names = []
        for i in range(num):
            raw = get_name(self._iface, i)
            names.append(raw.decode("utf-8", "replace") if raw else "")
        return names

    def get_achievement(self, name: str) -> bool:
        """查询成就当前状态；名称未知时返回 False。"""
        achieved = c_bool(False)
        ok = self._exports["SteamAPI_ISteamUserStats_GetAchievement"](
            self._iface, name.encode("utf-8"), ctypes.byref(achieved)
        )
        return bool(ok and achieved.value)

    def set_achievement(self, name: str) -> bool:
        """解锁成就（只改内存状态，必须再 store_stats 才会同步）。"""
        return bool(
            self._exports["SteamAPI_ISteamUserStats_SetAchievement"](
                self._iface, name.encode("utf-8")
            )
        )

    def store_stats(self) -> bool:
        """把内存中的成就/统计状态持久化并触发上传。"""
        return bool(self._exports["SteamAPI_ISteamUserStats_StoreStats"](self._iface))

    def display_attribute(self, name: str, key: str) -> str:
        """key 取 'name' / 'desc'，返回本地化文案（不支持时为空串）。"""
        fn = self._exports.get("SteamAPI_ISteamUserStats_GetAchievementDisplayAttribute")
        if fn is None:
            return ""
        raw = fn(self._iface, name.encode("utf-8"), key.encode("utf-8"))
        return raw.decode("utf-8", "replace") if raw else ""


def _default_loader(path: str):
    # steam_api 的导出是 cdecl（S_CALLTYPE），x64 下只有一种调用约定；
    # 统一用 CDLL。先注册 DLL 所在目录，保证依赖解析不出问题。
    try:
        os.add_dll_directory(str(Path(path).resolve().parent))
    except (AttributeError, OSError):
        pass
    return CDLL(path)


@contextlib.contextmanager
def appid_cwd(appid: int):
    """steam_appid.txt 必须位于进程当前工作目录：进入临时目录执行，结束后还原。"""
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="sau_") as tmp:
        (Path(tmp) / "steam_appid.txt").write_text(str(appid), encoding="ascii")
        os.chdir(tmp)
        try:
            yield
        finally:
            os.chdir(old_cwd)
