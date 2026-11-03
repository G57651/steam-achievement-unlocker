"""GUI 展示层纯逻辑（不依赖 tkinter，可直接单元测试）。"""

from __future__ import annotations

from .models import Achievement, UnlockReport

# 勾选符号（Windows 默认字体可渲染）；已锁定的行不可再勾选，渲染为圆点。
SEL_ON = "☑"
SEL_OFF = "☐"
SEL_DISABLED = "·"


def selection_symbol(achievement: Achievement, selected: bool) -> str:
    if achievement.achieved:
        return SEL_DISABLED
    return SEL_ON if selected else SEL_OFF


def build_rows(achievements: list[Achievement], selected: set[str]) -> list[tuple]:
    """构造 Treeview 行：勾选、状态、API 名称、显示名称、描述。"""
    return [
        (
            selection_symbol(a, a.api_name in selected),
            "已解锁" if a.achieved else "未解锁",
            a.api_name,
            a.display_name,
            a.description,
        )
        for a in achievements
    ]


def selectable_names(achievements: list[Achievement]) -> list[str]:
    """可勾选（未解锁）的成就 API 名称。"""
    return [a.api_name for a in achievements if not a.achieved]


def summarize_report(report: UnlockReport) -> str:
    """把解锁结果整理成面向用户的摘要文本。"""
    if report.dry_run:
        lines = [f"DRY-RUN 演示（未实际改动）：将解锁 {len(report.newly_unlocked)} 个"]
    else:
        lines = [f"新解锁 {len(report.newly_unlocked)} 个"]
    if report.newly_unlocked:
        lines.append("  " + ", ".join(report.newly_unlocked))
    if report.already_unlocked:
        lines.append(f"已是解锁状态（跳过）{len(report.already_unlocked)} 个")
    if report.unknown:
        lines.append(f"未识别 {len(report.unknown)} 个：{', '.join(report.unknown)}")
        lines.append("  （请核对 API 名称，区分大小写）")
    if report.failed:
        lines.append(f"解锁失败 {len(report.failed)} 个：{', '.join(report.failed)}")
    if not report.dry_run and report.newly_unlocked:
        lines.append("统计同步：成功" if report.stored else "统计同步：失败（可能未拥有该应用，或 Steam 未在线）")
    return "\n".join(lines)
