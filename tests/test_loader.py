"""FlatBindings 加载 / 初始化 / 接口获取逻辑的测试。

用两种假 DLL 驱动：
1. 系统 kernel32.dll —— 验证"缺少 Steam 导出"时的报错路径（真实 PE）；
2. FakeCtypesDll —— 模拟导出齐全的 steam_api64.dll，验证探测与调用编排。
"""

import os
import tempfile
import unittest
from pathlib import Path

from sau import steamapi
from sau.steamapi import ExportMissing, FlatBindings, SteamApiError

KERNEL32 = Path(os.environ.get("WINDIR", r"C:\Windows"), "System32", "kernel32.dll")


class FakeCtypesDll:
    """模拟 ctypes.WinDLL：访问导出返回可调用对象，缺失抛 AttributeError。"""

    def __init__(self, exports: dict):
        self._exports = exports

    def __getattr__(self, name):
        try:
            return self._exports[name]
        except KeyError:
            raise AttributeError(name) from None


def make_full_fake_dll(calls: dict) -> FakeCtypesDll:
    exports = {
        "SteamAPI_InitFlat": lambda msg: 0,
        "SteamAPI_Shutdown": lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1),
        "SteamAPI_RunCallbacks": lambda: calls.__setitem__("callbacks", calls["callbacks"] + 1),
        "SteamAPI_ISteamUserStats": lambda: 0,  # 访问器返回空 → 走版本探测
        "SteamAPI_GetHSteamUser": lambda: 3,
        "SteamInternal_FindOrCreateUserInterface": (
            lambda huser, version: 777
            if version == b"STEAMUSERSTATS_INTERFACE_VERSION012"
            else 0
        ),
        "SteamAPI_ISteamUserStats_GetNumAchievements": lambda iface: 2,
        "SteamAPI_ISteamUserStats_GetAchievementName": (
            lambda iface, i: [b"ACH_A", b"ACH_B"][i]
        ),
        "SteamAPI_ISteamUserStats_GetAchievement": lambda iface, name, achieved: True,
        "SteamAPI_ISteamUserStats_SetAchievement": (
            lambda iface, name: calls["set"].append(name.decode()) or True
        ),
        "SteamAPI_ISteamUserStats_StoreStats": (
            lambda iface: calls.__setitem__("store", calls["store"] + 1) or True
        ),
        "SteamAPI_ISteamUserStats_GetAchievementDisplayAttribute": (
            lambda iface, name, key: (b"Display" if key == b"name" else b"Desc")
        ),
    }
    return FakeCtypesDll(exports)


class LoaderTests(unittest.TestCase):
    @unittest.skipUnless(KERNEL32.exists(), "需要 Windows 系统 DLL 作样本")
    def test_kernel32_rejected_for_missing_exports(self):
        with self.assertRaises(ExportMissing) as ctx:
            FlatBindings.load(KERNEL32)
        self.assertIn("SteamAPI_ISteamUserStats_SetAchievement", str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(SteamApiError):
            FlatBindings.load("Z:/nope/steam_api64.dll")

    def test_loader_oserror_mentions_bitness(self):
        def boom(_path):
            raise OSError(193, "%1 不是有效的 Win32 应用程序")

        with tempfile.NamedTemporaryFile(suffix=".dll", delete=False) as fh:
            path = fh.name
        try:
            with self.assertRaises(SteamApiError) as ctx:
                FlatBindings.load(path, loader=boom)
            self.assertIn("位数", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_no_init_export_rejected(self):
        exports = {
            name: (lambda *a: True)
            for name in (
                "SteamAPI_ISteamUserStats_GetAchievement",
                "SteamAPI_ISteamUserStats_SetAchievement",
                "SteamAPI_ISteamUserStats_StoreStats",
                "SteamAPI_Shutdown",
                "SteamAPI_RunCallbacks",
            )
        }
        with tempfile.NamedTemporaryFile(suffix=".dll", delete=False) as fh:
            path = fh.name
        try:
            with self.assertRaises(ExportMissing) as ctx:
                FlatBindings.load(path, loader=lambda p: FakeCtypesDll(exports))
            self.assertIn("初始化", str(ctx.exception))
        finally:
            os.unlink(path)


class FakeDllFlowTests(unittest.TestCase):
    def setUp(self):
        self.calls = {"shutdown": 0, "callbacks": 0, "store": 0, "set": []}
        self.bindings = FlatBindings.load(
            "fake/steam_api64.dll", loader=lambda p: make_full_fake_dll(self.calls)
        )

    def test_full_unlock_flow(self):
        self.bindings.init()
        us = self.bindings.user_stats()
        self.assertEqual(us.version, "STEAMUSERSTATS_INTERFACE_VERSION012")  # 探测跳过 013
        self.assertEqual(us.get_achievement_names(), ["ACH_A", "ACH_B"])
        self.assertTrue(us.set_achievement("ACH_A"))
        self.assertEqual(self.calls["set"], ["ACH_A"])
        self.assertTrue(us.store_stats())
        self.assertEqual(self.calls["store"], 1)
        self.bindings.run_callbacks()
        self.assertEqual(self.calls["callbacks"], 1)
        self.bindings.shutdown()
        self.assertEqual(self.calls["shutdown"], 1)

    def test_init_requires_before_user_stats(self):
        with self.assertRaises(SteamApiError):
            self.bindings.user_stats()

    def test_shutdown_only_after_successful_init(self):
        self.bindings.shutdown()
        self.assertEqual(self.calls["shutdown"], 0)
        self.bindings.init()
        self.bindings.shutdown()
        self.assertEqual(self.calls["shutdown"], 1)

    def test_double_init_is_noop(self):
        self.bindings.init()
        self.bindings.init()
        self.bindings.shutdown()
        self.assertEqual(self.calls["shutdown"], 1)


class LegacyBoolInitTests(unittest.TestCase):
    def _dll(self, exports):
        base = {
            "SteamAPI_ISteamUserStats_GetAchievement": lambda *a: True,
            "SteamAPI_ISteamUserStats_SetAchievement": lambda *a: True,
            "SteamAPI_ISteamUserStats_StoreStats": lambda *a: True,
            "SteamAPI_Shutdown": lambda: None,
            "SteamAPI_RunCallbacks": lambda: None,
        }
        base.update(exports)
        return FakeCtypesDll(base)

    def test_legacy_init_false_raises(self):
        bindings = FlatBindings.load(
            "x.dll", loader=lambda p: self._dll({"SteamAPI_Init": lambda: False})
        )
        with self.assertRaises(steamapi.SteamNotRunning):
            bindings.init()

    def test_init_fails_then_init_safe_used(self):
        bindings = FlatBindings.load(
            "x.dll",
            loader=lambda p: self._dll(
                {"SteamAPI_Init": lambda: False, "SteamAPI_InitSafe": lambda: True}
            ),
        )
        bindings.init()  # 不应抛异常：Init 失败后回退 InitSafe
        self.bindings = bindings
        bindings.shutdown()


if __name__ == "__main__":
    unittest.main()
