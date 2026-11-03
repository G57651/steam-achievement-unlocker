"""测试辅助：模拟 Steam 客户端行为的 Fake bindings。"""

from __future__ import annotations

from pathlib import Path

from sau import steamapi


class FakeState:
    """一次测试会话里的 Steam 状态。

    schema 为 None 表示模拟旧 DLL（不支持 GetNumAchievements 枚举）。
    """

    def __init__(self, schema=None, achieved=(), running=True, store_ok=True, fail_names=()):
        self.schema = schema
        self.achieved = set(achieved)
        self.running = running
        self.store_ok = store_ok
        self.fail_names = set(fail_names)
        self.store_calls = 0
        self.set_calls = []
        self.shutdown_calls = 0
        self.callbacks_calls = 0
        self.cwd_at_init = None
        self.appid_at_init = None
        self.display = {
            name: (f"{name} 的名字", f"{name} 的描述") for name in (schema or [])
        }


class FakeUserStats:
    def __init__(self, state: FakeState):
        self._state = state

    def request_current_stats(self):
        return True

    def get_num_achievements(self):
        if self._state.schema is None:
            return None
        return len(self._state.schema)

    def get_achievement_names(self):
        if self._state.schema is None:
            return None
        return list(self._state.schema)

    def get_achievement(self, name: str) -> bool:
        return name in self._state.achieved

    def set_achievement(self, name: str) -> bool:
        if name in self._state.fail_names:
            return False
        self._state.set_calls.append(name)
        self._state.achieved.add(name)
        return True

    def store_stats(self) -> bool:
        self._state.store_calls += 1
        return self._state.store_ok

    def display_attribute(self, name: str, key: str) -> str:
        display_name, desc = self._state.display.get(name, ("", ""))
        return display_name if key == "name" else desc


class FakeBindings:
    """与 FlatBindings 同调用面的假实现。"""

    def __init__(self, state: FakeState):
        self._state = state
        self._initialized = False

    @classmethod
    def load(cls, dll_path, loader=None, state=None):
        if state is None:
            raise RuntimeError("FakeBindings.load 需要 state 参数（测试约定）")
        return cls(state)

    def init(self):
        if not self._state.running:
            raise steamapi.SteamNotRunning(
                "SteamAPI 初始化失败：NoSteamClient（Steam 未运行或未登录）"
            )
        self._initialized = True
        appid_file = Path.cwd() / "steam_appid.txt"
        self._state.cwd_at_init = str(appid_file.parent)
        self._state.appid_at_init = (
            appid_file.read_text(encoding="ascii").strip() if appid_file.exists() else None
        )

    def shutdown(self):
        if self._initialized:
            self._state.shutdown_calls += 1
            self._initialized = False

    def run_callbacks(self):
        self._state.callbacks_calls += 1

    def user_stats(self):
        if not self._initialized:
            raise steamapi.SteamApiError("必须先成功 init() 再获取接口")
        return FakeUserStats(self._state)


def make_factory(state: FakeState):
    """生成 UnlockService(bindings_factory=...) 用的工厂。"""

    def factory(dll_path):
        return FakeBindings(state)

    return factory
