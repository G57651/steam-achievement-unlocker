"""Tkinter 图形界面。

线程模型：SteamAPI 的 init/调用/shutdown 必须同线程，且 tkinter 只允许主线程
碰控件——所以所有 Steam 操作放进后台 worker 线程，结果/错误经队列由主线程的
pump（root.after）取出并处理。

测试注入口：`run_async`（把异步换成同步执行）、`askyesno`/`showerror`/`showinfo`
（对话框桩）、`service`（Fake 工厂构造的 UnlockService）。
"""

from __future__ import annotations

import io
import queue
import sys
import threading
import types

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import cli, library, presenter
from .models import Achievement, UnlockReport
from .service import (
    ServiceError,
    UnlockService,
    list_achievements_subprocess,
    unlock_one_subprocess,
)
from .steamapi import default_dll_path
from pathlib import Path

POLL_MS = 80


def _threaded_run_async(fn, on_ok, on_err, events: queue.Queue) -> None:
    def worker():
        try:
            events.put((on_ok, fn()))
        except Exception as exc:  # 后台线程兜底：任何异常都要回到主线程展示
            events.put((on_err, exc))

    threading.Thread(target=worker, daemon=True).start()


class AchievementGUI:
    def __init__(
        self,
        root: tk.Tk,
        service: UnlockService | None = None,
        askyesno=None,
        showerror=None,
        showinfo=None,
        doctor_fn=None,
        run_async=None,
        pick_dll=None,
    ):
        self.root = root
        root.title("sau — Steam 成就解锁（仅解锁成就）")
        root.minsize(880, 600)

        self.service = service or UnlockService()
        self._askyesno = askyesno or messagebox.askyesno
        self._showerror = showerror or messagebox.showerror
        self._showinfo = showinfo or messagebox.showinfo
        self._doctor_fn = doctor_fn
        self._pick_dll = pick_dll  # 测试注入；默认文件对话框
        self._events: queue.Queue = queue.Queue()
        if run_async is not None:
            self._run_async = lambda fn, on_ok, on_err: run_async(fn, on_ok, on_err, self._events)
        else:
            self._run_async = lambda fn, on_ok, on_err: _threaded_run_async(
                fn, on_ok, on_err, self._events
            )

        self.achievements: list[Achievement] = []
        self._selected: set[str] = set()
        self._busy = False
        self._library_dialog = None  # 打开中的游戏库对话框（防重复打开）
        self._library_steam_dir = None  # 测试注入；None → 注册表/常见路径
        self._names_json = None  # 旧 DLL 离线名称表（GUI 暂不暴露，CLI 可用）
        self._settle = 2.0
        self._build_widgets()
        self._log_line("提示：解锁会同步到 Steam 服务器且无法撤销；先用「仅演示」预览。")
        self._pump()

    # ---- 布局 -----------------------------------------------------------

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="AppID：").pack(side="left")
        self.appid_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.appid_var, width=12)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self.on_load())
        entry.focus_set()  # 启动即可直接输入 AppID
        self.btn_load = ttk.Button(top, text="加载成就", command=self.on_load)
        self.btn_load.pack(side="left", padx=6)
        self.btn_library = ttk.Button(top, text="加载游戏库", command=self.on_open_library)
        self.btn_library.pack(side="left")
        self.btn_doctor = ttk.Button(top, text="环境自检", command=self.on_doctor)
        self.btn_doctor.pack(side="left", padx=6)

        self.dll_var = tk.StringVar(value=str(default_dll_path() or "（未找到 DLL，请选择）"))
        dll_frame = ttk.Frame(self.root)
        dll_frame.pack(fill="x", **pad)
        ttk.Label(dll_frame, text="steam_api DLL：").pack(side="left")
        ttk.Label(dll_frame, textvariable=self.dll_var, foreground="#555").pack(
            side="left", fill="x", expand=True
        )
        self.btn_dll = ttk.Button(dll_frame, text="选择 DLL…", command=self.on_pick_dll)
        self.btn_dll.pack(side="left")

        columns = ("sel", "state", "api", "display", "desc")
        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=14)
        for col, text, width, anchor in (
            ("sel", "选", 40, "center"),
            ("state", "状态", 72, "center"),
            ("api", "API 名称", 280, "w"),
            ("display", "显示名称", 220, "w"),
            ("desc", "描述", 320, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "desc"))
        self.tree.column("desc", width=320, minwidth=200)
        self.tree.tag_configure("achieved", foreground="#888")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<ButtonRelease-1>", self._on_row_click)

        sel_bar = ttk.Frame(self.root)
        sel_bar.pack(fill="x", **pad)
        self.btn_sel_locked = ttk.Button(sel_bar, text="全选未解锁", command=self.select_all_selectable)
        self.btn_sel_locked.pack(side="left")
        self.btn_sel_all = ttk.Button(sel_bar, text="全选（含已解锁）", command=self.select_all)
        self.btn_sel_all.pack(side="left", padx=6)
        self.btn_sel_none = ttk.Button(sel_bar, text="取消全选", command=self.select_none)
        self.btn_sel_none.pack(side="left")
        self.count_var = tk.StringVar(value="未加载成就")
        ttk.Label(sel_bar, textvariable=self.count_var).pack(side="right")

        opt_bar = ttk.Frame(self.root)
        opt_bar.pack(fill="x", **pad)
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_bar, text="仅演示（dry-run，不实际解锁）", variable=self.dry_var
        ).pack(side="left")
        self.btn_unlock = ttk.Button(
            opt_bar, text="解锁选中成就", command=self.on_unlock
        )
        self.btn_unlock.pack(side="right")

        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", **pad)

        log_frame = ttk.LabelFrame(self.root, text="日志")
        log_frame.pack(fill="both", **pad)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=6, pady=(0, 6)
        )

    # ---- 工具 -----------------------------------------------------------

    def _log_line(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in (self.btn_load, self.btn_unlock, self.btn_doctor, self.btn_dll,
                    self.btn_library,
                    self.btn_sel_all, self.btn_sel_locked, self.btn_sel_none):
            btn.configure(state=state)
        if status:
            self.status_var.set(status)

    def _parse_appid(self) -> int | None:
        raw = self.appid_var.get().strip()
        if not raw.isdigit() or int(raw) <= 0:
            self._showerror("输入错误", "请输入有效的 AppID（正整数，可在 Steam 商店网址中查看）")
            return None
        return int(raw)

    def _drain_events(self):
        """处理当前积压的 UI 事件（主线程调用；测试里也可手动驱动）。"""
        while True:
            try:
                handler, payload = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                handler(payload)
            except Exception as exc:
                # 单个事件出错不能拖垮事件泵：否则后续所有异步结果永远
                # 无人处理，界面表现为"卡死在忙碌状态"（例如对话框已销毁
                # 后其加载回调触碰控件抛 TclError）。
                try:
                    self._log_line(f"内部错误：{exc!r}")
                except Exception:
                    pass

    def _pump(self):
        try:
            self._drain_events()
        finally:
            # 无论本次处理是否出错都要重新排队，保证事件泵永续
            self.root.after(POLL_MS, self._pump)

    # ---- 事件：加载 -----------------------------------------------------

    def on_load(self):
        if self._busy:
            return
        appid = self._parse_appid()
        if appid is None:
            return
        self._set_busy(True, f"正在加载应用 {appid} 的成就…")
        # 子进程加载：GUI 进程内绝不 SteamAPI_Init（schema 串台，见坑 AA/AE）
        self._run_async(
            lambda: list_achievements_subprocess(
                appid, dll=self.service.dll_path, names_json=self._names_json
            ),
            lambda rows: self._on_loaded(appid, rows),
            self._on_error,
        )

    def _on_loaded(self, appid: int, rows: list[Achievement]):
        self._set_busy(False, f"应用 {appid} 加载完成")
        self.achievements = rows
        self._selected = set(presenter.selectable_names(rows))  # 默认全选未解锁
        self._rebuild_tree()
        selectable = presenter.selectable_names(rows)
        self._log_line(f"应用 {appid}：共 {len(rows)} 个成就，未解锁 {len(selectable)} 个（已默认全选未解锁）")
        if not rows:
            self._showinfo("提示", f"应用 {appid} 没有枚举到任何成就。")

    def _rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        achieved_names = {a.api_name for a in self.achievements if a.achieved}
        for row in presenter.build_rows(self.achievements, self._selected):
            tags = ("achieved",) if row[2] in achieved_names else ()
            self.tree.insert("", "end", iid=row[2], values=row, tags=tags)
        selectable = len(presenter.selectable_names(self.achievements))
        self.count_var.set(f"已选 {len(self._selected)} / 可选 {selectable}（共 {len(self.achievements)}）")

    def _on_row_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item or self._busy:
            return
        achievement = next((a for a in self.achievements if a.api_name == item), None)
        if achievement is None or achievement.achieved:
            return
        if item in self._selected:
            self._selected.discard(item)
        else:
            self._selected.add(item)
        self.tree.set(item, "sel", presenter.selection_symbol(achievement, item in self._selected))
        self.count_var.set(
            f"已选 {len(self._selected)} / 可选 {len(presenter.selectable_names(self.achievements))}"
            f"（共 {len(self.achievements)}）"
        )

    def select_all_selectable(self):
        self._selected = set(presenter.selectable_names(self.achievements))
        self._rebuild_tree()

    def select_all(self):
        self._selected = {a.api_name for a in self.achievements}
        self._rebuild_tree()

    def select_none(self):
        self._selected.clear()
        self._rebuild_tree()

    # ---- 事件：解锁 -----------------------------------------------------

    def on_unlock(self):
        if self._busy:
            return
        appid = self._parse_appid()
        if appid is None:
            return
        order = {a.api_name: i for i, a in enumerate(self.achievements)}
        names = sorted(self._selected, key=lambda n: order.get(n, 1 << 30))
        if not names:
            self._showinfo("提示", "请先在列表中勾选要解锁的成就。")
            return
        dry_run = bool(self.dry_var.get())
        if not dry_run:
            confirmed = self._askyesno(
                "确认解锁",
                f"将对应用 {appid} 解锁 {len(names)} 个成就，并同步到 Steam 服务器。\n"
                "解锁无法撤销，确定继续？",
            )
            if not confirmed:
                self._log_line("已取消解锁。")
                return
        self._set_busy(True, "正在解锁…")
        self.progress.configure(maximum=len(names), value=0)
        self._run_async(
            lambda: unlock_one_subprocess(
                appid, names=names, dry_run=dry_run,
                settle=self._settle, dll=self.service.dll_path,
                all_achievements=False,
            ),
            self._on_unlocked,
            self._on_error,
        )

    def _on_unlocked(self, report: UnlockReport):
        self._set_busy(False, "解锁完成" if not report.dry_run else "dry-run 完成")
        summary = presenter.summarize_report(report)
        self._log_line(summary)
        self.progress.configure(value=self.progress["maximum"])
        if report.ok:
            self._showinfo("完成", summary)
        else:
            self._showerror("部分未完成", summary)
        if not report.dry_run and report.newly_unlocked:
            self.on_load()  # 解锁成功后自动刷新列表状态

    # ---- 事件：游戏库 ---------------------------------------------------

    def on_open_library(self):
        if self._busy:
            return
        existing = self._library_dialog
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_set()
            return  # 已有对话框在（可能正在扫描），不重复开第二个
        self._library_dialog = GameLibraryDialog(self.root, self)  # master 必须是 Tk 根窗口

    def run_batch_unlock(self, targets, dry_run: bool):
        """批量解锁：targets 为 library.LibraryEntry 列表，逐个游戏全部解锁。"""
        if self._busy or not targets:
            return
        if not dry_run:
            confirmed = self._askyesno(
                "批量解锁确认",
                f"将对 {len(targets)} 个游戏批量解锁全部未获得成就，并同步到 Steam 服务器。\n"
                "这是不可逆的大批量操作，确定继续？",
            )
            if not confirmed:
                self._log_line("已取消批量解锁。")
                return
        self._set_busy(True, "批量解锁中…")
        self.progress.configure(maximum=len(targets), value=0)

        def task():
            totals = {"games_ok": 0, "unlocked": 0, "already": 0, "unknown": 0, "failed": 0}
            for index, entry in enumerate(targets, start=1):
                try:
                    report = unlock_one_subprocess(
                        entry.appid, dry_run=dry_run, settle=1.0,
                        dll=self.service.dll_path,
                    )
                except ServiceError as exc:
                    self._events.put(
                        (self._on_batch_step, (index, len(targets), entry, None, str(exc)))
                    )
                    continue
                totals["unlocked"] += len(report.newly_unlocked)
                totals["already"] += len(report.already_unlocked)
                totals["unknown"] += len(report.unknown)
                totals["failed"] += len(report.failed)
                if report.ok or report.dry_run:
                    totals["games_ok"] += 1
                self._events.put(
                    (self._on_batch_step, (index, len(targets), entry, report, None))
                )
            return totals, len(targets), dry_run

        self._run_async(task, self._on_batch_done, self._on_error)

    def _on_batch_step(self, payload):
        index, total, entry, report, error = payload
        self.progress.configure(value=index)
        if report is None:
            self._log_line(f"[{index}/{total}] {entry.appid} {entry.name}: 出错（{error}）")
        else:
            verb = "将解锁" if report.dry_run else "新解锁"
            self._log_line(
                f"[{index}/{total}] {entry.appid} {entry.name}: {verb} "
                f"{len(report.newly_unlocked)}，跳过 {len(report.already_unlocked)}，"
                f"未识别 {len(report.unknown)}，失败 {len(report.failed)}"
            )
        self.status_var.set(f"批量解锁… {index}/{total}")

    def _on_batch_done(self, payload):
        totals, total, dry_run = payload
        self._set_busy(False, "批量解锁完成" if not dry_run else "批量演示完成")
        summary = (
            f"{'批量演示' if dry_run else '批量解锁'}完成：{totals['games_ok']}/{total} 个游戏成功，"
            f"{'将解锁' if dry_run else '新解锁'} {totals['unlocked']} 个成就，"
            f"跳过 {totals['already']}，未识别 {totals['unknown']}，失败 {totals['failed']}"
        )
        self._log_line(summary)
        if totals["failed"] or totals["unknown"]:
            self._showerror("批量解锁结束（部分未完成）", summary)
        else:
            self._showinfo("批量解锁结束", summary)

    # ---- 事件：其他 -----------------------------------------------------

    def on_doctor(self):
        if self._busy:
            return
        self._set_busy(True, "正在自检…")

        def doctor():
            fn = self._doctor_fn or _default_doctor
            return fn()

        self._run_async(
            doctor,
            lambda text: (self._set_busy(False, "自检完成"),
                          self._log_line(text),
                          self._showinfo("环境自检", text)),
            self._on_error,
        )

    def on_pick_dll(self):
        if self._busy:
            return
        path = (self._pick_dll or filedialog.askopenfilename)(
            filetypes=[("steam_api DLL", "*.dll"), ("所有文件", "*.*")]
        )
        if not path:
            return
        self.service = UnlockService(dll_path=path)
        self.dll_var.set(path)
        self._log_line(f"已切换 DLL：{path}")

    def _on_error(self, exc: Exception):
        self._set_busy(False, "出错")
        self.progress.configure(value=0)
        detail = str(exc) if isinstance(exc, ServiceError) else repr(exc)
        self._log_line(f"错误：{detail}")
        self._showerror("错误", detail)


def _default_doctor() -> str:
    out = io.StringIO()
    cli.cmd_doctor(types.SimpleNamespace(dll=default_dll_path()), out)
    return out.getvalue()


class GameLibraryDialog(tk.Toplevel):
    """游戏库选择：自动枚举账户游戏，选中后无需手输 AppID。"""

    def __init__(self, master: tk.Tk, app: "AchievementGUI"):
        super().__init__(master)
        self.app = app
        self.title("游戏库 — 选择要管理的游戏")
        self.geometry("760x560")
        self.transient(master)

        self.entries = []
        self._thumb_paths: dict = {}
        self._photo: dict = {}
        self._tree_gen = 0
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="来源：").pack(side="left")
        self.mode_var = tk.StringVar(value="installed")
        for mode, label in (
            ("installed", "已安装（离线）"),
            ("owned", "账户全部拥有（扫描约 10 秒）"),
            ("webapi", "Web API（需 key）"),
        ):
            ttk.Radiobutton(top, text=label, value=mode, variable=self.mode_var).pack(side="left")
        self.refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="忽略缓存", variable=self.refresh_var).pack(side="left", padx=6)

        filter_bar = ttk.Frame(self)
        filter_bar.pack(fill="x", padx=6)
        ttk.Label(filter_bar, text="展示：").pack(side="left")
        self.filter_var = tk.StringVar(value="all")
        ttk.Radiobutton(filter_bar, text="全部游戏", value="all",
                        variable=self.filter_var, command=self._apply_filter).pack(side="left")
        ttk.Radiobutton(filter_bar, text="有成就系统的游戏", value="with_achievements",
                        variable=self.filter_var, command=self._apply_filter).pack(side="left")
        ttk.Label(filter_bar, text="（切换保留已选状态）", foreground="#888").pack(side="left", padx=6)

        key_bar = ttk.Frame(self)
        key_bar.pack(fill="x", padx=6)
        ttk.Label(key_bar, text="Web API key（仅 webapi 需要）：").pack(side="left")
        self.key_var = tk.StringVar()
        ttk.Entry(key_bar, textvariable=self.key_var, show="*", width=40).pack(side="left")

        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill="x", padx=6, pady=4)
        self.btn_scan = ttk.Button(btn_bar, text="加载游戏库", command=self.on_scan)
        self.btn_scan.pack(side="left")
        ttk.Label(btn_bar, text="  选择：").pack(side="left")
        self.btn_sel_all = ttk.Button(btn_bar, text="全选", command=self.select_all, state="disabled")
        self.btn_sel_all.pack(side="left")
        self.btn_sel_invert = ttk.Button(btn_bar, text="反选", command=self.invert_selection, state="disabled")
        self.btn_sel_invert.pack(side="left")
        self.btn_sel_none = ttk.Button(btn_bar, text="取消全选", command=self.select_none, state="disabled")
        self.btn_sel_none.pack(side="left")

        act_bar = ttk.Frame(self)
        act_bar.pack(fill="x", padx=6, pady=4)
        self.btn_load_game = ttk.Button(
            act_bar, text="载入所选游戏的成就", command=self.on_pick_game, state="disabled"
        )
        self.btn_load_game.pack(side="left")
        self.btn_batch = ttk.Button(
            act_bar, text="批量解锁选中的游戏", command=self.on_batch, state="disabled"
        )
        self.btn_batch.pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="")
        ttk.Label(act_bar, textvariable=self.count_var).pack(side="right")

        columns = ("appid", "name", "ach", "note")
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=4)
        # 缩略图显示在 #0 树列（Treeview 的 image 只支持 #0 列）
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="tree headings", height=16,
            selectmode="extended",  # 支持 Ctrl/Shift 多选
        )
        self.tree.heading("#0", text="缩略图")
        self.tree.column("#0", width=96, minwidth=96, anchor="center", stretch=False)
        for col, text, width in (
            ("appid", "AppID", 80),
            ("name", "名称", 340),
            ("ach", "成就数", 70),
            ("note", "备注", 100),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w" if col == "name" else "center",
                             stretch=(col == "name"))
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_count())
        style = ttk.Style(self)
        style.configure("Library.Treeview", rowheight=48)  # 容纳 43px 缩略图
        self.tree.configure(style="Library.Treeview")

        self.status_var = tk.StringVar(value="选择来源后点击「加载游戏库」")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=6, pady=(0, 6))

    def on_scan(self):
        self.btn_scan.configure(state="disabled")
        self.btn_load_game.configure(state="disabled")
        self.btn_batch.configure(state="disabled")
        for btn in (self.btn_sel_all, self.btn_sel_invert, self.btn_sel_none):
            btn.configure(state="disabled")
        self.status_var.set("正在加载游戏库…（owned 模式首次含成就扫描，可能需要数分钟）")
        # Tk 变量只能在主线程读取：先取值，再派发 worker（Python 3.12 会强制检查）
        mode = self.mode_var.get()
        api_key = self.key_var.get().strip() or None
        refresh = self.refresh_var.get()

        def task():
            entries, cache = library.build_library(
                mode,
                steam_dir=self.app._library_steam_dir,
                api_key=api_key,
                with_achievements=True,
                refresh=refresh,
                progress=lambda done, total: None,
                dll=self.app.service.dll_path,
            )
            thumbs = {}
            try:  # 缩略图：本地 librarycache → 成就游戏的 CDN 兜底
                thumbs = library.build_thumbnail_cache(
                    steam_dir=self.app._library_steam_dir,
                    appids=[e.appid for e in entries],
                    cdn_appids=[e.appid for e in entries if (e.achievements or 0) > 0],
                )
            except Exception:
                thumbs = {}
            return entries, cache, thumbs

        self.app._run_async(task, self._on_loaded, self._on_error)

    def _visible_entries(self) -> list:
        """按展示选项过滤：with_achievements 只看有成就系统的游戏。"""
        if self.filter_var.get() == "with_achievements":
            return [e for e in self.entries if (e.achievements or 0) > 0]
        return list(self.entries)

    def _rebuild_tree(self, default_select: bool):
        """按当前展示选项重建列表行。

        default_select=False（切换过滤器）时保留原有已选状态；
        default_select=True（新扫描）时默认选中所有有成就的游戏。
        """
        previous = set(self.tree.selection())
        self._tree_gen += 1
        generation = self._tree_gen
        self.tree.delete(*self.tree.get_children())
        visible = self._visible_entries()
        achievement_ids = []
        ordered = []
        for entry in sorted(visible, key=lambda e: (-(e.achievements or 0), e.appid)):
            iid = str(entry.appid)
            note = "已安装" if entry.installed else ""
            self.tree.insert(
                "", "end", iid=iid, text="",
                image=self._photo.get(entry.appid, ""),
                values=(entry.appid, entry.name, "" if entry.achievements is None else entry.achievements, note),
            )
            ordered.append(iid)
            if (entry.achievements or 0) > 0:
                achievement_ids.append(iid)
        if default_select:
            if achievement_ids:
                self.tree.selection_set(achievement_ids)
        else:
            keep = [iid for iid in achievement_ids if iid in previous] or [
                iid for iid in previous if self.tree.exists(iid)
            ]
            if keep:
                self.tree.selection_set(keep)
        self.status_var.set(
            f"共 {len(self.entries)} 个应用，其中有成就 {len(achievement_ids)} 个，"
            f"当前显示 {len(visible)} 个"
        )
        for btn in (self.btn_scan, self.btn_load_game, self.btn_batch,
                    self.btn_sel_all, self.btn_sel_invert, self.btn_sel_none):
            btn.configure(state="normal")
        if not visible:
            for btn in (self.btn_load_game, self.btn_batch,
                        self.btn_sel_all, self.btn_sel_invert, self.btn_sel_none):
                btn.configure(state="disabled")
        self._update_count()
        # 缩略图分批附加（PhotoImage 只能主线程创建，分批避免界面卡顿）
        if self._thumb_paths:
            self.after(10, lambda: self._attach_thumbs(ordered, generation))

    def _attach_thumbs(self, iids, generation):
        if generation != self._tree_gen:
            return  # 列表已重建，这批过期
        batch, rest = iids[:60], iids[60:]
        for iid in batch:
            appid = int(iid)
            png = (self._thumb_paths or {}).get(appid)
            if not png or not Path(png).exists() or appid in self._photo:
                continue
            try:
                photo = tk.PhotoImage(file=str(png))
            except tk.TclError:
                continue
            self._photo[appid] = photo
            if self.tree.exists(iid):
                self.tree.item(iid, image=photo)
        if rest:
            self.after(10, lambda: self._attach_thumbs(rest, generation))

    def _apply_filter(self):
        if self.entries:
            self._rebuild_tree(default_select=False)

    def _on_loaded(self, result):
        if not self.winfo_exists():
            return  # 扫描期间对话框被关闭：结果无处展示，静默丢弃
        entries, _cache, thumbs = result
        self.entries = entries
        self._thumb_paths = thumbs or {}
        self._rebuild_tree(default_select=True)

    def _update_count(self):
        if not self.entries:
            self.count_var.set("")
            return
        selected = set(self.tree.selection())
        with_ach = sum(1 for e in self.entries
                       if str(e.appid) in selected and (e.achievements or 0) > 0)
        self.count_var.set(f"已选 {len(selected)}，其中有成就 {with_ach}")

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def invert_selection(self):
        self.tree.selection_toggle(self.tree.get_children())

    def select_none(self):
        self.tree.selection_set([])

    def _on_error(self, exc: Exception):
        if not self.winfo_exists():
            return  # 对话框已关闭
        self.btn_scan.configure(state="normal")
        self.status_var.set("加载失败")
        self.app._on_error(exc)

    def _selected_appid(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            self.app._showinfo("提示", "请先在列表中选中一个游戏。")
            return None
        return int(selection[0])

    def on_pick_game(self):
        appid = self._selected_appid()
        if appid is None:
            return
        self.destroy()
        self.app.appid_var.set(str(appid))
        self.app.on_load()

    def on_batch(self):
        selected = set(self.tree.selection())
        if not selected:
            self.app._showinfo("提示", "请先选择游戏（可用「全选 / 反选」，或 Ctrl/Shift 多选）。")
            return
        targets = [
            e for e in self.entries
            if str(e.appid) in selected and (e.achievements or 0) > 0
        ]
        if not targets:
            self.app._showinfo(
                "提示",
                "所选游戏中没有可解锁成就的游戏（成就数为 0 或未能查询）。\n"
                "成就扫描失败的条目可用 --refresh 重试。",
            )
            return
        dry_run = bool(self.app.dry_var.get())
        self.destroy()
        self.app.run_batch_unlock(targets, dry_run)


def launch() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法初始化 GUI（无显示环境？）：{exc}", file=sys.stderr)
        return 1
    AchievementGUI(root)
    root.mainloop()
    return 0
