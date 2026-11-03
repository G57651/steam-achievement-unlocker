"""GUI 层测试：不进 mainloop，用同步 run_async 与对话框桩驱动事件流。

需要 tkinter 与显示环境（Windows 桌面会话）；无头环境自动跳过。
"""

import unittest
from unittest import mock

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None

from sau import gui as gui_module
from sau import presenter
from sau.gui import AchievementGUI, GameLibraryDialog
from sau.models import Achievement, UnlockReport
from sau.service import ServiceError, UnlockService

SCHEMA = ["ACH_A", "ACH_B", "ACH_C"]


def sync_run_async(fn, on_ok, on_err, _events):
    """测试用：同步执行，立即回调，绕过线程与事件队列。"""
    try:
        on_ok(fn())
    except Exception as exc:  # noqa: BLE001
        on_err(exc)


def ach_rows(schema, achieved=()):
    """构造与子进程 _list-one 返回形态等价的成就行列表。"""
    return [
        Achievement(api_name=name, display_name=f"{name} 的名字",
                    description=f"{name} 的描述", achieved=name in achieved)
        for name in schema
    ]


@unittest.skipIf(tk is None, "需要 tkinter")
class GuiTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.dialogs = {"askyesno": [], "showerror": [], "showinfo": []}
        self.app = AchievementGUI(
            self.root,
            service=UnlockService(dll_path="fake/steam_api64.dll"),
            askyesno=lambda title, msg: (self.dialogs["askyesno"].append((title, msg)) or True),
            showerror=lambda title, msg: self.dialogs["showerror"].append((title, msg)),
            showinfo=lambda title, msg: self.dialogs["showinfo"].append((title, msg)),
            run_async=sync_run_async,
        )

    def tearDown(self):
        self.root.destroy()

    def patch_list(self, achieved=()):
        rows = ach_rows(SCHEMA, achieved)
        return mock.patch.object(gui_module, "list_achievements_subprocess",
                                 lambda appid, **kw: rows)

    def load(self, appid="400", achieved=()):
        with self.patch_list(achieved):
            self.app.appid_var.set(appid)
            self.app.on_load()


@unittest.skipIf(tk is None, "需要 tkinter")
class GuiLoadTests(GuiTestBase):
    def test_construction_builds_widgets(self):
        self.assertEqual(self.app.tree["columns"], ("sel", "state", "api", "display", "desc"))

    def test_load_populates_tree_and_autoselects_selectable(self):
        self.load()
        items = self.app.tree.get_children()
        self.assertEqual(list(items), SCHEMA)
        self.assertEqual(self.app._selected, {"ACH_A", "ACH_B", "ACH_C"})
        self.assertIn("共 3 个成就", self.app.log_text.get("1.0", "end"))

    def test_load_achieved_state_preselected_means_only_achieved_rows(self):
        self.load(achieved={"ACH_A"})
        self.assertEqual(self.app._selected, {"ACH_B", "ACH_C"})
        # 已解锁行渲染为不可勾选符号并带灰显 tag
        self.assertEqual(self.app.tree.set("ACH_A", "sel"), presenter.SEL_DISABLED)
        self.assertEqual(self.app.tree.item("ACH_A", "tags"), ("achieved",))

    def test_invalid_appid_shows_error_dialog(self):
        self.app.appid_var.set("abc")
        self.app.on_load()
        self.assertEqual(len(self.dialogs["showerror"]), 1)
        self.assertEqual(self.app.achievements, [])


@unittest.skipIf(tk is None, "需要 tkinter")
class GuiUnlockTests(GuiTestBase):
    def test_unlock_selected_with_confirm(self):
        self.load()
        self.app.dry_var.set(False)
        self.app.select_none()
        self.app._selected.add("ACH_A")
        calls = []

        def fake_unlock(appid, **kw):
            calls.append((appid, kw))
            return UnlockReport(appid=appid, requested=["ACH_A"],
                                newly_unlocked=["ACH_A"], stored=True)

        with mock.patch.object(gui_module, "unlock_one_subprocess", fake_unlock):
            self.app.on_unlock()
        # 子进程收到选中的成就名（而非 --all）
        self.assertEqual(calls[0][0], 400)
        self.assertEqual(calls[0][1]["names"], ["ACH_A"])
        self.assertEqual(calls[0][1]["all_achievements"], False)
        # 确认框出现，且文案包含数量
        title, msg = self.dialogs["askyesno"][0]
        self.assertEqual(title, "确认解锁")
        self.assertIn("1 个成就", msg)
        self.assertIn("新解锁 1 个", self.app.log_text.get("1.0", "end"))

    def test_confirm_declined_unlocks_nothing(self):
        self.load()
        self.app.dry_var.set(False)
        self.app._askyesno = lambda t, m: False  # 模拟用户点"否"
        self.app.on_unlock()
        self.assertIn("已取消", self.app.log_text.get("1.0", "end"))

    def test_dry_run_skips_confirmation(self):
        self.load()
        self.app.dry_var.set(True)
        self.app.select_none()
        self.app._selected.add("ACH_B")
        calls = []

        def fake_unlock(appid, **kw):
            calls.append((appid, kw))
            return UnlockReport(appid=appid, requested=["ACH_B"],
                                newly_unlocked=["ACH_B"], dry_run=True)

        with mock.patch.object(gui_module, "unlock_one_subprocess", fake_unlock):
            self.app.on_unlock()
        self.assertEqual(self.dialogs["askyesno"], [])
        self.assertEqual(calls[0][1]["dry_run"], True)
        self.assertIn("DRY-RUN", self.app.log_text.get("1.0", "end"))

    def test_empty_selection_prompts(self):
        self.load()
        self.app.select_none()
        self.app.on_unlock()
        self.assertEqual(self.dialogs["showinfo"], [("提示", "请先在列表中勾选要解锁的成就。")])

    def test_service_error_shows_error_dialog(self):
        def boom(appid, **kw):
            raise ServiceError("Steam 未运行")

        with mock.patch.object(gui_module, "list_achievements_subprocess", boom):
            self.app.appid_var.set("400")
            self.app.on_load()
        self.assertEqual(len(self.dialogs["showerror"]), 1)
        title, msg = self.dialogs["showerror"][0]
        self.assertEqual(title, "错误")
        self.assertIn("Steam", msg)


@unittest.skipIf(tk is None, "需要 tkinter")
class GuiMiscTests(GuiTestBase):
    def test_doctor_uses_injected_fn(self):
        self.app._doctor_fn = lambda: "[OK] 假自检结果"
        self.app.on_doctor()
        self.assertEqual(self.dialogs["showinfo"][0][1], "[OK] 假自检结果")

    def test_pick_dll_swaps_service(self):
        service_before = self.app.service
        self.app._pick_dll = lambda **kw: "D:/some/steam_api64.dll"
        self.app.on_pick_dll()
        self.assertIsNot(self.app.service, service_before)
        self.assertEqual(self.app.dll_var.get(), "D:/some/steam_api64.dll")

    def test_busy_guard_blocks_reentry(self):
        self.load()
        self.app._set_busy(True)
        self.app.on_load()  # 忙碌时点击应被忽略
        self.assertEqual(len(self.dialogs["showerror"]), 0)

    def test_busy_disables_library_button_too(self):
        self.load()
        self.app._set_busy(True)
        self.assertEqual(str(self.app.btn_library["state"]), "disabled")
        self.app._set_busy(False)
        self.assertEqual(str(self.app.btn_library["state"]), "normal")

    def test_event_pump_survives_handler_error(self):
        # 单个事件回调抛异常不能拖垮事件泵（否则后续异步结果永远无人处理）
        self.load()
        processed = []
        self.app._events.put((lambda _p: 1 / 0, None))  # 会抛 ZeroDivisionError
        self.app._events.put((lambda _p: processed.append(True), None))
        self.app._drain_events()  # 不应向外抛异常
        self.assertEqual(processed, [True])
        self.assertIn("内部错误", self.app.log_text.get("1.0", "end"))

    def test_open_library_reuses_existing_dialog(self):
        self.app.on_open_library()
        first = self.app._library_dialog
        self.app.on_open_library()  # 第二次点击应聚焦已有对话框
        self.assertIs(self.app._library_dialog, first)
        first.destroy()  # root 销毁前先关掉，避免 cleanup 时报 application destroyed


@unittest.skipIf(tk is None, "需要 tkinter")
class LibraryDialogTests(GuiTestBase):
    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path

        from sau import library

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        steam_dir = Path(tmp.name) / "steam"
        (steam_dir / "steamapps").mkdir(parents=True)
        (steam_dir / "steamapps" / "appmanifest_440.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"440"\n\t"name"\t\t"Team Fortress 2"\n}\n',
            encoding="utf-8",
        )
        patcher = mock.patch.multiple(
            library,
            LIBRARY_CACHE=Path(tmp.name) / "lib.json",
            APPLIST_CACHE=Path(tmp.name) / "apps.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app._library_steam_dir = steam_dir
        self.dialog = GameLibraryDialog(self.root, self.app)

    def tearDown(self):
        if self.dialog.winfo_exists():
            self.dialog.destroy()
        super().tearDown()

    def test_dialog_installed_scan_and_pick(self):
        self.dialog.mode_var.set("installed")
        self.dialog.on_scan()  # 同步执行
        items = list(self.dialog.tree.get_children())
        self.assertIn("440", items)
        row = self.dialog.tree.item("440", "values")
        self.assertEqual(row[1], "Team Fortress 2")  # values 不含 #0 缩略图列
        self.assertEqual(row[3], "已安装")
        # 载入选中游戏 → 主窗口通过子进程加载该 AppID 的成就
        with mock.patch.object(
            gui_module, "list_achievements_subprocess",
            lambda appid, **kw: ach_rows(SCHEMA),
        ):
            self.dialog.tree.selection_set("440")
            self.dialog.on_pick_game()
        self.assertFalse(self.dialog.winfo_exists())
        self.assertEqual(self.app.appid_var.get(), "440")
        self.assertEqual([a.api_name for a in self.app.achievements], SCHEMA)

    def test_dialog_batch_uses_dry_run_flag(self):
        from sau.library import LibraryCache, LibraryEntry

        entries = [
            LibraryEntry(440, "TF2", owned=True, achievements=520),
            LibraryEntry(500, "NoAch", owned=True, achievements=0),
            LibraryEntry(700, "GameB", owned=True, achievements=30),
        ]
        self.dialog._on_loaded((entries, LibraryCache(), {}))
        self.app.dry_var.set(True)
        self.dialog.select_all()
        captured = []
        with mock.patch.object(self.app, "run_batch_unlock",
                               lambda targets, dry: captured.append((targets, dry))):
            self.dialog.on_batch()
        self.assertFalse(self.dialog.winfo_exists())
        # 只有"有成就"的选中项进入批量（500 成就数为 0 被排除）
        self.assertEqual([e.appid for e in captured[0][0]], [440, 700])
        self.assertTrue(captured[0][1])

    def test_dialog_selection_buttons_and_counter(self):
        from sau.library import LibraryCache, LibraryEntry

        entries = [
            LibraryEntry(440, "TF2", owned=True, achievements=520),
            LibraryEntry(500, "NoAch", owned=True, achievements=0),
            LibraryEntry(600, "Unknown", owned=True, achievements=None),
        ]
        self.dialog._on_loaded((entries, LibraryCache(), {}))
        # 有成就的默认被选中
        self.assertEqual(set(self.dialog.tree.selection()), {"440"})
        self.assertIn("已选 1，其中有成就 1", self.dialog.count_var.get())
        # 全选
        self.dialog.select_all()
        self.root.update()  # 派发 <<TreeviewSelect>> 事件
        self.assertEqual(set(self.dialog.tree.selection()), {"440", "500", "600"})
        self.assertIn("已选 3", self.dialog.count_var.get())
        # 反选（全选 → 空）
        self.dialog.invert_selection()
        self.root.update()
        self.assertEqual(set(self.dialog.tree.selection()), set())
        # 再反选（空 → 全部）
        self.dialog.invert_selection()
        self.root.update()
        self.assertEqual(set(self.dialog.tree.selection()), {"440", "500", "600"})
        # 取消全选
        self.dialog.select_none()
        self.root.update()
        self.assertEqual(set(self.dialog.tree.selection()), set())

    def test_dialog_batch_without_selection_prompts(self):
        from sau.library import LibraryCache, LibraryEntry

        entries = [LibraryEntry(440, "TF2", owned=True, achievements=520)]
        self.dialog._on_loaded((entries, LibraryCache(), {}))
        self.dialog.select_none()
        self.dialog.on_batch()
        self.assertTrue(self.dialog.winfo_exists())  # 未关闭
        self.assertEqual(
            self.dialogs["showinfo"][-1][1],
            "请先选择游戏（可用「全选 / 反选」，或 Ctrl/Shift 多选）。",
        )

    def test_dialog_display_filter(self):
        from sau.library import LibraryCache, LibraryEntry

        entries = [
            LibraryEntry(440, "TF2", owned=True, achievements=520),
            LibraryEntry(500, "NoAch", owned=True, achievements=0),
            LibraryEntry(600, "Unknown", owned=True, achievements=None),
            LibraryEntry(700, "GameB", owned=True, achievements=30),
        ]
        self.dialog._on_loaded((entries, LibraryCache(), {}))
        self.assertEqual(set(self.dialog.tree.get_children()), {"440", "500", "600", "700"})
        # 切到"有成就系统的游戏"：只剩成就数 > 0 的
        self.dialog.filter_var.set("with_achievements")
        self.dialog._apply_filter()
        self.assertEqual(set(self.dialog.tree.get_children()), {"440", "700"})
        self.assertIn("当前显示 2 个", self.dialog.status_var.get())
        # 切换保留已选状态（440 是默认选中的）
        self.assertIn("440", self.dialog.tree.selection())
        # 回到全部游戏
        self.dialog.filter_var.set("all")
        self.dialog._apply_filter()
        self.assertEqual(set(self.dialog.tree.get_children()), {"440", "500", "600", "700"})
        # 过滤视图下"全选"只作用于可见行
        self.dialog.filter_var.set("with_achievements")
        self.dialog._apply_filter()
        self.dialog.select_all()
        self.assertEqual(set(self.dialog.tree.selection()), {"440", "700"})

    def test_dialog_batch_skips_games_without_achievements(self):
        from sau.library import LibraryCache, LibraryEntry

        entries = [LibraryEntry(500, "NoAch", owned=True, achievements=0)]
        self.dialog._on_loaded((entries, LibraryCache(), {}))
        self.dialog.select_all()
        self.dialog.on_batch()
        self.assertTrue(self.dialog.winfo_exists())
        self.assertIn("没有可解锁成就的游戏", self.dialogs["showinfo"][-1][1])

    def test_dialog_callbacks_after_destroy_are_ignored(self):
        # 扫描期间用户关掉对话框：加载/错误回调到达时不应触碰已销毁的控件
        from sau.library import LibraryCache, LibraryEntry

        self.dialog.destroy()
        self.dialog._on_loaded(([LibraryEntry(440, "TF2", owned=True)], LibraryCache(), {}))
        self.dialog._on_error(ServiceError("Steam 未运行"))  # 都不应抛 TclError


@unittest.skipIf(tk is None, "需要 tkinter")
class BatchUnlockTests(GuiTestBase):
    def test_batch_dry_run_no_changes(self):
        from sau.library import LibraryEntry
        from sau.models import UnlockReport

        calls = []

        def fake_unlock(appid, **kw):
            calls.append((appid, kw))
            return UnlockReport(appid=appid, requested=["A1"], newly_unlocked=["A1"],
                                dry_run=True)

        with mock.patch.object(gui_module, "unlock_one_subprocess", fake_unlock):
            self.app.run_batch_unlock([LibraryEntry(400, "Game A", achievements=3)], dry_run=True)
            self.app._drain_events()
        self.assertEqual(calls[0][1]["dry_run"], True)
        self.assertEqual(calls[0][1]["dll"], "fake/steam_api64.dll")
        log = self.app.log_text.get("1.0", "end")
        self.assertIn("[1/1] 400 Game A", log)
        self.assertIn("批量演示完成", log)

    def test_batch_with_confirm_unlocks(self):
        from sau.library import LibraryEntry
        from sau.models import UnlockReport

        def fake_unlock(appid, **kw):
            return UnlockReport(appid=appid, requested=["A1"], newly_unlocked=["A1"],
                                stored=True)

        with mock.patch.object(gui_module, "unlock_one_subprocess", fake_unlock):
            self.app.run_batch_unlock(
                [LibraryEntry(400, "Game A", achievements=3)], dry_run=False
            )
            self.app._drain_events()
        title, msg = self.dialogs["askyesno"][0]
        self.assertEqual(title, "批量解锁确认")
        self.assertIn("1 个游戏", msg)
        self.assertIn("批量解锁完成", self.app.log_text.get("1.0", "end"))

    def test_batch_declined(self):
        from sau.library import LibraryEntry

        self.app._askyesno = lambda t, m: False
        self.app.run_batch_unlock(
            [LibraryEntry(400, "Game A", achievements=3)], dry_run=False
        )
        self.assertIn("已取消批量解锁", self.app.log_text.get("1.0", "end"))

    def test_batch_service_error_continues(self):
        from sau.library import LibraryEntry
        from sau.service import ServiceError

        def boom(appid, **kw):
            raise ServiceError("Steam 未运行")

        with mock.patch.object(gui_module, "unlock_one_subprocess", boom):
            self.app.run_batch_unlock(
                [LibraryEntry(400, "Bad Game", achievements=3)], dry_run=True
            )
            self.app._drain_events()
        log = self.app.log_text.get("1.0", "end")
        self.assertIn("出错", log)


if __name__ == "__main__":
    unittest.main()
