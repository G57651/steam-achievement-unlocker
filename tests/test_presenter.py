"""presenter（GUI 展示层纯逻辑）测试。"""

import unittest

from sau import presenter
from sau.models import Achievement, UnlockReport


def ach(name, achieved=False, display="", desc=""):
    return Achievement(api_name=name, display_name=display, description=desc, achieved=achieved)


class BuildRowsTests(unittest.TestCase):
    def test_symbols(self):
        rows = presenter.build_rows(
            [ach("A", achieved=True), ach("B"), ach("C")],
            selected={"B"},
        )
        self.assertEqual(rows[0][0], presenter.SEL_DISABLED)  # 已解锁不可勾选
        self.assertEqual(rows[1][0], presenter.SEL_ON)
        self.assertEqual(rows[2][0], presenter.SEL_OFF)
        self.assertEqual(rows[0][1], "已解锁")
        self.assertEqual(rows[1][1], "未解锁")

    def test_selectable_names(self):
        names = presenter.selectable_names([ach("A", achieved=True), ach("B"), ach("C")])
        self.assertEqual(names, ["B", "C"])


class SummarizeReportTests(unittest.TestCase):
    def report(self, **kw) -> UnlockReport:
        base = dict(appid=400, requested=["A"])
        base.update(kw)
        return UnlockReport(**base)

    def test_dry_run_summary(self):
        text = presenter.summarize_report(self.report(newly_unlocked=["A"], dry_run=True))
        self.assertIn("DRY-RUN", text)
        self.assertIn("将解锁 1 个", text)
        self.assertNotIn("统计同步", text)

    def test_full_summary(self):
        text = presenter.summarize_report(
            self.report(
                newly_unlocked=["A"],
                already_unlocked=["B"],
                unknown=["X"],
                failed=[],
                stored=True,
            )
        )
        self.assertIn("新解锁 1 个", text)
        self.assertIn("已是解锁状态（跳过）1 个", text)
        self.assertIn("未识别 1 个：X", text)
        self.assertIn("统计同步：成功", text)

    def test_store_failure_hint(self):
        text = presenter.summarize_report(self.report(newly_unlocked=["A"], stored=False))
        self.assertIn("统计同步：失败", text)
        self.assertIn("未拥有该应用", text)


if __name__ == "__main__":
    unittest.main()
