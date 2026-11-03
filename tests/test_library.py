"""library（游戏库枚举）测试：全部用假绑定与临时文件，不碰真实 Steam/网络。"""

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sau import library, steamapi
from sau.library import LibraryError, LibraryEntry


class FakeLibUserStats:
    def __init__(self, state, appid):
        self._state = state
        self._appid = appid

    def get_num_achievements(self):
        return self._state.achievements.get(self._appid, 0)


class FakeLibBindings:
    """library 层用到的 FlatBindings 子集。"""

    def __init__(self, state):
        self._state = state
        self._appid = None
        self._init = False

    def init(self):
        if not self._state.running:
            raise steamapi.SteamNotRunning("fake: Steam 未运行")
        self._init = True
        appid_file = Path.cwd() / "steam_appid.txt"
        self._appid = int(appid_file.read_text(encoding="ascii").strip())

    def shutdown(self):
        self._init = False

    def is_subscribed(self, appid):
        return appid in self._state.owned

    def is_app_installed(self, appid):
        return appid in self._state.installed

    def steam_id(self):
        return self._state.steam_id

    def user_stats(self):
        return FakeLibUserStats(self._state, self._appid)


class FakeLibState:
    def __init__(self, owned=(), achievements=None, steam_id=76561198000000001):
        self.owned = set(owned)
        self.achievements = dict(achievements or {})
        self.installed = set()
        self.steam_id = steam_id
        self.running = True


def make_lib_factory(state):
    return lambda dll_path: FakeLibBindings(state)


class LibraryTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher_cache = mock.patch.object(library, "LIBRARY_CACHE", self.tmp / "library_cache.json")
        patcher_list = mock.patch.object(library, "APPLIST_CACHE", self.tmp / "applist_cache.json")
        patcher_cache.start()
        patcher_list.start()
        self.addCleanup(patcher_cache.stop)
        self.addCleanup(patcher_list.stop)

    def make_steam_dir(self, manifests: dict[int, str], extra_library=None) -> Path:
        """构造假的 Steam 目录：libraryfolders.vdf + appmanifest_*.acf。"""
        steam_dir = self.tmp / "steam"
        (steam_dir / "steamapps").mkdir(parents=True)
        lib2 = self.tmp / "library2"
        (lib2 / "steamapps").mkdir(parents=True)
        for appid, name in manifests.items():
            safe_name = name.replace('"', '\\"')
            (steam_dir / "steamapps" / f"appmanifest_{appid}.acf").write_text(
                f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{safe_name}"\n}}\n',
                encoding="utf-8",
            )
        (lib2 / "steamapps" / "appmanifest_602320.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"602320"\n\t"name"\t\t"Train Valley 2"\n}\n',
            encoding="utf-8",
        )
        vdf = (
            '"libraryfolders"\n{\n'
            f'\t"0"\n\t{{\n\t\t"path"\t\t"{steam_dir.as_posix().replace("/", "\\\\")}"\n\t}}\n'
            f'\t"1"\n\t{{\n\t\t"path"\t\t"{lib2.as_posix().replace("/", "\\\\")}"\n\t}}\n'
            "}\n"
        )
        (steam_dir / "steamapps" / "libraryfolders.vdf").write_text(vdf, encoding="utf-8")
        return steam_dir


class InstalledAppsTests(LibraryTestBase):
    def test_parse_manifests_across_libraries(self):
        steam_dir = self.make_steam_dir({440: "Team Fortress 2", 1273400: 'He said "hi"'})
        apps = library.installed_apps(steam_dir)
        self.assertEqual(apps[440], "Team Fortress 2")
        self.assertEqual(apps[602320], "Train Valley 2")
        self.assertEqual(apps[1273400], 'He said "hi"')

    def test_missing_dir_returns_empty(self):
        self.assertEqual(library.installed_apps(self.tmp / "nope"), {})


class AppInfoParserTests(LibraryTestBase):
    def build_old_format_appinfo(self) -> bytes:
        """手工构造 0x28 旧格式 appinfo（一条记录）。"""

        def cstr(s: bytes) -> bytes:
            return s + b"\x00"

        common = (
            b"\x01" + cstr(b"name") + cstr("Team Fortress 2".encode())
            + b"\x02" + cstr(b"appid") + struct.pack("<I", 440)
            + b"\x08"                     # dict 结束
        )
        sections = (
            b"\x00" + cstr(b"common") + common   # common 段（值直接是字典内容）
            + b"\x00" + cstr(b"other") + b"\x02" + cstr(b"n") + struct.pack("<I", 7) + b"\x08"
            + b"\x08"                     # 段结束
        )
        body = (
            struct.pack("<I", 440)          # appid
            + struct.pack("<I", len(sections))  # size
            + struct.pack("<I", 1)          # state
            + struct.pack("<Q", 1700000000)  # last_updated
            + struct.pack("<Q", 0)          # token
            + sections
        )
        return (
            struct.pack("<II", 0x07564428, 1)
            + body
            + struct.pack("<I", 0)          # 终止记录 appid=0
        )

    def test_parse_old_format(self):
        steam_dir = self.tmp / "steam"
        (steam_dir / "appcache").mkdir(parents=True)
        (steam_dir / "appcache" / "appinfo.vdf").write_bytes(self.build_old_format_appinfo())
        names = library.parse_appinfo_names(steam_dir)
        self.assertEqual(names, {440: "Team Fortress 2"})

    def test_parse_v29_format(self):
        """现行 v0x29 格式：记录 = appid + 00 03 00 00 00 + (01, u32索引, cstring)*。

        名称 = 第一条非封面哈希的内联字符串（440 前两条是 librarycache 封面哈希）。
        """
        steam_dir = self.tmp / "steam"
        (steam_dir / "appcache").mkdir(parents=True)

        def entry(index: int, value: bytes) -> bytes:
            return b"\x01" + struct.pack("<I", index) + value + b"\x00"

        hash1 = b"a" * 40
        hash2 = b"b" * 40 + b"_thumb"
        record_440 = (
            struct.pack("<I", 440) + b"\x00\x03\x00\x00\x00"
            + entry(367, hash1) + entry(368, hash2) + entry(369, hash2)
            + entry(370, b"pc/teamfortress2")            # URL slug（大索引）
            + entry(4, "Team Fortress 2".encode())        # 显示名（最小索引）
        )
        record_602320 = (
            struct.pack("<I", 602320) + b"\x00\x03\x00\x00\x00"
            + entry(4, "Train Valley 2".encode())
        )
        payload = (
            struct.pack("<II", 0x07564429, 1)
            + record_440 + record_602320
        )
        (steam_dir / "appcache" / "appinfo.vdf").write_bytes(payload)
        names = library.parse_appinfo_names(steam_dir, wanted={440, 602320, 999999})
        self.assertEqual(names, {440: "Team Fortress 2", 602320: "Train Valley 2"})

    def test_v29_garbage_returns_empty(self):
        steam_dir = self.tmp / "steam"
        (steam_dir / "appcache").mkdir(parents=True)
        payload = struct.pack("<II", 0x07564429, 1) + b"\x12\x34\x56\x78" + b"\xff" * 32
        (steam_dir / "appcache" / "appinfo.vdf").write_bytes(payload)
        self.assertEqual(
            library.parse_appinfo_names(steam_dir, wanted={1, 2, 3}), {}
        )

    def test_build_thumbnail_cache_converts_and_downloads(self):
        """本地 librarycache 的图 → PowerShell 转 PNG；缺失的走 CDN（注入 HTTP）。"""
        import base64

        # 1x1 PNG（.NET 按内容识别，扩展名 .jpg 也能读）
        png_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
            "h6FO1AAAAABJRU5ErkJggg=="
        )
        steam_dir = self.tmp / "steam"
        folder = steam_dir / "appcache" / "librarycache" / "440"
        folder.mkdir(parents=True)
        (folder / "library_header.jpg").write_bytes(png_1x1)
        dest = self.tmp / "thumbs"

        cdn_payload = png_1x1 * 40  # >1024 字节的"下载"
        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            return cdn_payload

        thumbs = library.build_thumbnail_cache(
            steam_dir, [440, 999999], dest_dir=dest,
            cdn_appids=[440, 999999], http_get=fake_get,
        )
        self.assertIn(440, thumbs)
        self.assertTrue(thumbs[440].exists())
        self.assertTrue((dest / "999999_cdn.jpg").exists())  # CDN 已下载
        self.assertIn("steam/apps/999999/header.jpg", calls[0])
        self.assertTrue(thumbs[999999].exists())

    def test_build_thumbnail_cache_cdn_cap(self):
        """CDN 兜底有上限，避免全库下载。"""
        steam_dir = self.tmp / "steam"  # 无本地缓存
        (steam_dir / "appcache" / "librarycache").mkdir(parents=True)
        dest = self.tmp / "thumbs"

        def fake_get(url, timeout):
            return b"x" * 2048

        appids = list(range(1000, 1000 + 50))
        library.build_thumbnail_cache(
            steam_dir, appids, dest_dir=dest,
            cdn_appids=appids, http_get=fake_get,
        )
        cdn_files = list(dest.glob("*_cdn.jpg"))
        self.assertLessEqual(len(cdn_files), 400)

    def test_rejects_new_format_and_garbage(self):
        steam_dir = self.tmp / "steam"
        (steam_dir / "appcache").mkdir(parents=True)
        path = steam_dir / "appcache" / "appinfo.vdf"
        path.write_bytes(struct.pack("<II", 0x07564429, 1) + b"\x00" * 64)  # v29 新格式
        self.assertEqual(library.parse_appinfo_names(steam_dir), {})
        path.write_bytes(b"not a pe..")
        self.assertEqual(library.parse_appinfo_names(steam_dir), {})
        self.assertEqual(library.parse_appinfo_names(self.tmp / "nope"), {})


class OwnedScanTests(LibraryTestBase):
    def test_scan_owned(self):
        state = FakeLibState(owned={0, 440, 480, 602320, 400000})
        factory = make_lib_factory(state)
        # scan_owned 不过滤内部 AppID（过滤在 build_library 层）；602320 超出区间
        # 假绑定从不加载 DLL：patch 掉默认路径检查，使本测试不依赖机器上是否
        # 存在 sdk/steam_api64.dll（纯源码环境下此前会误报 LibraryError）
        with mock.patch.object(steamapi, "default_dll_path", return_value=Path("fake.dll")):
            owned = library.scan_owned(factory, max_appid=1000)
        self.assertEqual(owned, [0, 440, 480])

    def test_scan_achievements_uses_injected_runner(self):
        counts_map = {440: 520, 480: 1}
        saved = []
        counts = library.scan_achievements(
            [440, 480, 999],
            runner=lambda appid: counts_map.get(appid, 0),
            on_result=lambda appid, count: saved.append((appid, count)),
        )
        self.assertEqual(counts, {440: 520, 480: 1, 999: 0})
        self.assertEqual(saved, [(440, 520), (480, 1), (999, 0)])

    def test_scan_achievements_runner_exception_becomes_none(self):
        def boom(appid):
            raise RuntimeError("子进程崩溃")

        self.assertEqual(library.scan_achievements([7], runner=boom), {7: None})

    def test_scan_requires_apps_interface(self):
        class NoApps(FakeLibBindings):
            def is_subscribed(self, appid):
                return None

        with mock.patch.object(steamapi, "default_dll_path", return_value=Path("fake.dll")):
            with self.assertRaises(LibraryError):
                library.scan_owned(lambda p: NoApps(FakeLibState()), max_appid=10)

    def test_steam_not_running_raises_library_error(self):
        state = FakeLibState()
        state.running = False
        with mock.patch.object(steamapi, "default_dll_path", return_value=Path("fake.dll")):
            with self.assertRaises(LibraryError):
                library.scan_owned(make_lib_factory(state), max_appid=10)


class BuildLibraryTests(LibraryTestBase):
    def test_installed_mode(self):
        steam_dir = self.make_steam_dir({440: "Team Fortress 2"})
        entries, _ = library.build_library(
            "installed", steam_dir=steam_dir
        )
        self.assertEqual([(e.appid, e.name) for e in entries], [(440, "Team Fortress 2"), (602320, "Train Valley 2")])

    def test_owned_mode_with_names_and_achievements(self):
        steam_dir = self.make_steam_dir({})
        owned_ids = [0, 440, 602320]  # 区间过滤在子命令层，这里拿到的是扫描结果
        achievements = {440: 520, 602320: 77}
        with mock.patch.object(library, "fetch_app_names", return_value={440: "Team Fortress 2"}), \
             mock.patch.object(library, "scan_owned_subprocess", return_value=owned_ids), \
             mock.patch.object(library, "steam_id_subprocess", return_value=76561198000000001), \
             mock.patch.object(library, "count_achievements_subprocess",
                               lambda appid, dll=None: achievements.get(appid, 0)):
            entries, cache = library.build_library(
                "owned",
                steam_dir=steam_dir,
                with_achievements=True,
            )
        self.assertEqual([e.appid for e in entries], [440, 602320])  # 0 被过滤，999999 超区间
        by_id = {e.appid: e for e in entries}
        self.assertEqual(by_id[440].name, "Team Fortress 2")
        self.assertEqual(by_id[440].achievements, 520)
        self.assertEqual(by_id[602320].achievements, 77)
        self.assertEqual(cache.owned, [0, 440, 602320])
        self.assertEqual(cache.steam_id, 76561198000000001)

    def test_owned_mode_uses_cache_without_rescan(self):
        cache = library.LibraryCache(
            steam_id=123, scanned_at=1.0, owned=[440], names={440: "TF2 (cached)"}
        )
        library.save_library_cache(cache)

        def must_not_scan(max_appid=None, dll=None):
            raise AssertionError("缓存命中时不应重新扫描")

        with mock.patch.object(library, "scan_owned_subprocess", must_not_scan):
            entries, got = library.build_library(
                "owned", steam_dir=self.tmp / "no-steam"
            )
        self.assertEqual([e.appid for e in entries], [440])
        self.assertEqual(entries[0].name, "TF2 (cached)")

    def test_webapi_mode(self):
        payload = json.dumps({
            "response": {"games": [
                {"appid": 440, "name": "Team Fortress 2"},
                {"appid": 730, "name": "Counter-Strike 2"},
            ]}
        }).encode()
        names = library.webapi_owned_games("KEY", 76561198000000001, http_get=lambda url, timeout: payload)
        self.assertEqual(names, {440: "Team Fortress 2", 730: "Counter-Strike 2"})

    def test_games_with_achievements_filter(self):
        entries = [
            LibraryEntry(440, "A", achievements=520),
            LibraryEntry(500, "B", achievements=0),
            LibraryEntry(600, "C", achievements=None),
        ]
        self.assertEqual([e.appid for e in library.games_with_achievements(entries)], [440])


class CacheTests(LibraryTestBase):
    def test_roundtrip(self):
        cache = library.LibraryCache(
            steam_id=42, scanned_at=7.0, owned=[440, 480],
            achievements={440: 520, 480: -1}, names={440: "TF2"},
        )
        library.save_library_cache(cache)
        got = library.load_library_cache()
        self.assertEqual(got.steam_id, 42)
        self.assertEqual(got.owned, [440, 480])
        self.assertEqual(got.achievements, {440: 520, 480: -1})
        self.assertEqual(got.names, {440: "TF2"})

    def test_missing_cache_file(self):
        self.assertEqual(library.load_library_cache().owned, [])


class FetchAppNamesTests(LibraryTestBase):
    def test_success_writes_cache_and_reuses(self):
        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            return json.dumps({"applist": {"apps": [
                {"appid": 440, "name": "Team Fortress 2"},
                {"appid": 0, "name": ""},
            ]}}).encode()

        names = library.fetch_app_names(http_get=fake_get)
        self.assertEqual(names, {440: "Team Fortress 2"})
        names2 = library.fetch_app_names(http_get=fake_get)
        self.assertEqual(names2, {440: "Team Fortress 2"})
        self.assertEqual(len(calls), 1)  # 第二次走缓存

    def test_failure_with_cache_falls_back(self):
        import time as _time
        library.APPLIST_CACHE.write_text(
            json.dumps({"fetched_at": _time.time(), "names": {"440": "Old TF2"}}), encoding="utf-8"
        )

        def boom(url, timeout):
            raise OSError("down")

        self.assertEqual(library.fetch_app_names(http_get=boom), {440: "Old TF2"})
        library.APPLIST_CACHE.unlink()
        with self.assertRaises(LibraryError):
            library.fetch_app_names(http_get=boom)

    def test_failure_cooldown_skips_network(self):
        import time as _time
        library.APPLIST_CACHE.write_text(
            json.dumps({"fetched_at": _time.time() - 40 * 24 * 3600,  # 缓存已过期
                        "last_failed_at": _time.time(),               # 刚失败过
                        "names": {"440": "Old TF2"}}),
            encoding="utf-8",
        )
        calls = []

        def must_not_fetch(url, timeout):
            calls.append(url)
            raise OSError("down")

        # 冷却期内：不撞网络，直接用旧（过期）缓存
        self.assertEqual(library.fetch_app_names(http_get=must_not_fetch), {440: "Old TF2"})
        self.assertEqual(calls, [])
        # --refresh 绕过冷却期：真的撞网络（3 次重试），失败后仍回退旧缓存
        self.assertEqual(library.fetch_app_names(refresh=True, http_get=must_not_fetch),
                         {440: "Old TF2"})
        self.assertEqual(len(calls), 3)


class WebApiTests(LibraryTestBase):
    def test_error_response_raises(self):
        payload = json.dumps({"error": "Forbidden", "detail": "bad key"}).encode()
        with self.assertRaises(LibraryError):
            library.webapi_owned_games("BAD", 1, http_get=lambda url, timeout: payload)


if __name__ == "__main__":
    unittest.main()
