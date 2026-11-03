"""CLI 层测试：exit code、输出文案、确认交互。"""

import io
import unittest
from unittest import mock

from sau import cli
from tests.fake_bindings import FakeState, make_factory

SCHEMA = ["ACH_A", "ACH_B", "ACH_C"]


def run_main(argv, state=None, input_fn=input):
    state = state or FakeState(SCHEMA)
    out = io.StringIO()
    code = cli.main(
        argv, factory=make_factory(state), input_fn=input_fn, out=out
    )
    return code, out.getvalue(), state


class UnlockCliTests(unittest.TestCase):
    def test_unlock_all_with_yes(self):
        code, text, state = run_main(
            ["--dll", "fake/steam_api64.dll", "unlock", "400", "--all", "--yes", "--settle", "0"]
        )
        self.assertEqual(code, 0)
        self.assertIn("新解锁 3 个", text)
        self.assertIn("ACH_A, ACH_B, ACH_C", text)
        self.assertEqual(state.set_calls, ["ACH_A", "ACH_B", "ACH_C"])

    def test_unlock_by_names(self):
        code, text, state = run_main(
            ["--dll", "fake", "unlock", "400", "ACH_A", "--settle", "0"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(state.set_calls, ["ACH_A"])

    def test_unknown_name_exit_1(self):
        code, text, _ = run_main(["--dll", "fake", "unlock", "400", "NOPE"])
        self.assertEqual(code, 1)
        self.assertIn("未识别 1 个：NOPE", text)

    def test_dry_run_exit_0_and_no_changes(self):
        state = FakeState(SCHEMA)
        code, text, state = run_main(
            ["--dll", "fake", "unlock", "400", "--all", "--yes", "--dry-run"], state=state
        )
        self.assertEqual(code, 0)
        self.assertIn("将解锁 3 个", text)
        self.assertIn("未执行（dry-run）", text)
        self.assertEqual(state.set_calls, [])

    def test_no_names_no_all_is_usage_error(self):
        code, text, _ = run_main(["--dll", "fake", "unlock", "400"])
        self.assertEqual(code, 2)
        self.assertIn("错误", text)

    def test_all_and_names_conflict(self):
        code, text, _ = run_main(["--dll", "fake", "unlock", "400", "--all", "ACH_A"])
        self.assertEqual(code, 2)

    def test_all_requires_confirmation_and_can_abort(self):
        state = FakeState(SCHEMA)
        code, text, state = run_main(
            ["--dll", "fake", "unlock", "400", "--all"],
            state=state,
            input_fn=lambda _prompt: "n",
        )
        self.assertEqual(code, 1)
        self.assertIn("已取消", text)
        self.assertEqual(state.set_calls, [])

    def test_all_confirmation_accepts_y(self):
        state = FakeState(SCHEMA)
        code, text, state = run_main(
            ["--dll", "fake", "unlock", "400", "--all"],
            state=state,
            input_fn=lambda _prompt: "y",
        )
        self.assertEqual(code, 0)
        self.assertEqual(state.set_calls, ["ACH_A", "ACH_B", "ACH_C"])

    def test_steam_not_running_exit_1(self):
        code, text, _ = run_main(
            ["--dll", "fake", "unlock", "400", "ACH_A"],
            state=FakeState(SCHEMA, running=False),
        )
        self.assertEqual(code, 1)
        self.assertIn("错误", text)

    def test_invalid_appid_exit_2(self):
        code, text, _ = run_main(["--dll", "fake", "unlock", "0", "--all", "--yes"])
        self.assertEqual(code, 2)


class ListCliTests(unittest.TestCase):
    def test_list_prints_table(self):
        code, text, _ = run_main(
            ["--dll", "fake", "list", "400"], state=FakeState(SCHEMA, achieved={"ACH_A"})
        )
        self.assertEqual(code, 0)
        self.assertIn("共 3，已解锁 1", text)
        self.assertIn("[x] ACH_A", text)
        self.assertIn("[ ] ACH_B", text)
        self.assertIn("ACH_A 的名字", text)


class DoctorCliTests(unittest.TestCase):
    def test_doctor_reports_missing_everything_gracefully(self):
        original = cli.find_steam_dir
        cli.find_steam_dir = lambda: None
        try:
            out = io.StringIO()
            code = cli.main(["--dll", "Z:/nope/steam_api64.dll", "doctor"], out=out)
            self.assertEqual(code, 1)
            self.assertIn("未找到 Steam 安装目录", out.getvalue())
            self.assertIn("找不到 DLL 文件", out.getvalue())
        finally:
            cli.find_steam_dir = original

    def test_doctor_arch_mismatch_reported(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".dll", delete=False) as fh:
            dll_path = fh.name
        fake_pe = {"machine_name": "x86", "bits": 32, "exports": []}
        original_peinfo = cli.peinfo.inspect
        original_steam = cli.find_steam_dir
        cli.peinfo.inspect = lambda p: fake_pe
        cli.find_steam_dir = lambda: None
        try:
            out = io.StringIO()
            code = cli.main(["--dll", dll_path, "doctor"], out=out)
            self.assertIn("架构不匹配", out.getvalue())
            self.assertEqual(code, 1)
        finally:
            cli.peinfo.inspect = original_peinfo
            cli.find_steam_dir = original_steam
            os.unlink(dll_path)


class GamesCliTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        steam_dir = Path(tmp.name) / "steam"
        (steam_dir / "steamapps").mkdir(parents=True)
        (steam_dir / "steamapps" / "appmanifest_440.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"440"\n\t"name"\t\t"Team Fortress 2"\n}\n',
            encoding="utf-8",
        )
        self.steam_dir = steam_dir
        # 隔离缓存文件，避免读到真实扫描结果
        from sau import library

        patcher = unittest.mock.patch.multiple(
            library,
            LIBRARY_CACHE=Path(tmp.name) / "lib.json",
            APPLIST_CACHE=Path(tmp.name) / "apps.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_games_installed_mode(self):
        out = io.StringIO()
        code = cli.main(
            ["games", "--mode", "installed", "--steam-dir", str(self.steam_dir)], out=out
        )
        self.assertEqual(code, 0)
        self.assertIn("Team Fortress 2", out.getvalue())
        self.assertIn("[已安装]", out.getvalue())

    def test_games_owned_sorted_by_achievements(self):
        from sau.library import LibraryCache, LibraryEntry

        entries = [
            LibraryEntry(440, "TF2", owned=True, achievements=520),
            LibraryEntry(500, "NoAch", owned=True, achievements=0),
        ]
        fake = lambda *a, **kw: (entries, LibraryCache())  # noqa: E731
        with unittest.mock.patch.object(cli, "build_library", fake):
            out = io.StringIO()
            code = cli.main(["games", "--mode", "owned"], out=out)
        self.assertEqual(code, 0)
        self.assertIn("成就 520", out.getvalue())
        self.assertIn("TF2", out.getvalue())
        # 成就多的排前面
        self.assertLess(
            out.getvalue().index("TF2"), out.getvalue().index("NoAch")
        )

    def test_unlock_library_dry_run(self):
        from sau.library import LibraryCache, LibraryEntry
        from sau.models import UnlockReport

        entries = [LibraryEntry(400, "Game A", owned=True, achievements=3)]
        fake_build = lambda *a, **kw: (entries, LibraryCache())  # noqa: E731
        fake_unlock = lambda appid, **kw: UnlockReport(  # noqa: E731
            appid=appid, requested=["A1", "A2", "A3"],
            newly_unlocked=["A1", "A2", "A3"], dry_run=True,
        )
        with unittest.mock.patch.object(cli, "build_library", fake_build), \
             unittest.mock.patch.object(cli, "unlock_one_subprocess", fake_unlock):
            out = io.StringIO()
            code = cli.main(["unlock-library", "--dry-run", "--yes"], out=out)
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN 演示：1 个有成就的游戏", out.getvalue())
        self.assertIn("[1/1] 400 Game A: 将解锁 3", out.getvalue())
        self.assertIn("完成：1/1", out.getvalue())

    def test_unlock_library_aborts_without_confirm(self):
        from sau.library import LibraryCache, LibraryEntry

        entries = [LibraryEntry(400, "Game A", owned=True, achievements=3)]
        with unittest.mock.patch.object(
            cli, "build_library", lambda *a, **kw: (entries, LibraryCache())
        ):
            out = io.StringIO()
            code = cli.main(["unlock-library"], out=out, input_fn=lambda _p: "n")
        self.assertEqual(code, 1)
        self.assertIn("已取消", out.getvalue())


if __name__ == "__main__":
    unittest.main()
