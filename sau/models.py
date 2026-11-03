"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Achievement:
    """一条成就的完整信息。

    api_name 是 Steamworks 接口使用的名称（区分大小写，调用 SetAchievement
    必须用它），display_name/description 是玩家可见的本地化文案。
    """

    api_name: str
    display_name: str
    description: str
    achieved: bool


@dataclass
class UnlockReport:
    """一次解锁操作的完整结果。"""

    appid: int
    requested: list[str]
    newly_unlocked: list[str] = field(default_factory=list)
    already_unlocked: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    stored: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.unknown and not self.failed and (self.dry_run or self.stored)
