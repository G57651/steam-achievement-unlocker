"""sau — Steam Achievement Unlocker（Steam 成就解锁工具，仅解锁成就）。

通过 Steamworks flat C API（steam_api64.dll）与正在运行的 Steam 客户端通信，
以 steam_appid.txt 方式声明目标应用，调用 ISteamUserStats::SetAchievement +
StoreStats 完成成就解锁并同步到 Steam 服务器。

范围约定：本工具只提供"解锁成就"，不提供锁定/清除/统计篡改等功能。
"""

__version__ = "0.4.2"
