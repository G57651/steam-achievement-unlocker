"""与真实 Steamworks redist DLL（sdk/steam_api64.dll）的集成核对测试。

该 DLL 来自 rlabrecque/Steamworks.NET 仓库（Steamworks SDK redist，x64），
sha256 = e6d9bafb9a41e42fba7b21553db49f8719027af3f0deb23ff86cfc44d60e776d。
没有运行中的 Steam 时无法做正向初始化，这里验证到"导出齐全、可加载"为止。
"""

import unittest
from pathlib import Path

from sau import peinfo, steamapi
from sau.steamapi import REQUIRED_EXPORTS, FlatBindings, SteamApiError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_DLL = PROJECT_ROOT / "sdk" / "steam_api64.dll"


@unittest.skipUnless(SDK_DLL.exists(), "需要 sdk/steam_api64.dll（scripts/fetch_sdk.py 可下载）")
class RealDllExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info = peinfo.inspect(SDK_DLL)
        cls.exports = set(cls.info["exports"] or [])

    def test_is_x64(self):
        self.assertEqual(self.info["machine_name"], "x64")

    def test_required_exports_present(self):
        missing = [name for name in REQUIRED_EXPORTS if name not in self.exports]
        self.assertEqual(missing, [], f"必需导出缺失: {missing}")

    def test_at_least_one_init_strategy_present(self):
        inits = {"SteamAPI_InitFlat", "SteamAPI_InitEx", "SteamAPI_Init", "SteamAPI_InitSafe"}
        self.assertTrue(inits & self.exports, "没有任何初始化导出")

    def test_interface_acquisition_path_present(self):
        accessor = "SteamAPI_ISteamUserStats" in self.exports
        find_or_create = (
            "SteamInternal_FindOrCreateUserInterface" in self.exports
            and "SteamAPI_GetHSteamUser" in self.exports
        )
        self.assertTrue(accessor or find_or_create)

    def test_bindings_load_succeeds(self):
        bindings = FlatBindings.load(SDK_DLL)  # 只加载探测，不 init
        self.assertFalse(bindings._initialized)

    def test_enumeration_exports(self):
        # 这个版本的 DLL 有成就枚举导出（GetNumAchievements / GetAchievementName）
        self.assertIn("SteamAPI_ISteamUserStats_GetNumAchievements", self.exports)
        self.assertIn("SteamAPI_ISteamUserStats_GetAchievementName", self.exports)


if __name__ == "__main__":
    unittest.main()
