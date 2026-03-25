"""
notification_engine
===================
仓位操作 Telegram 通知引擎。

用法::

    from src.notification_engine import build_notifier, TelegramNotifier

    # 从环境变量自动初始化（推荐）
    notifier = build_notifier(pool_label="USDC/ETH 0.05%")

    # 或手动初始化
    notifier = TelegramNotifier(
        token      = "your_bot_token",
        chat_id    = "your_chat_id",
        pool_label = "USDC/ETH 0.05%",
    )

    # 测试连通性
    notifier.test_connection()

与 StrategyRunner 集成::

    from src.execution_engine import build_position_manager
    from src.strategy_engine import StrategyRunner, PoolConfig
    from src.strategy_engine.strategies import VolumeRebalanceStrategy
    from src.notification_engine import build_notifier

    runner = StrategyRunner(
        strategy         = VolumeRebalanceStrategy(),
        position_manager = build_position_manager(),
        pool_config      = PoolConfig(pool_address="0x88e6..."),
        notifier         = build_notifier(pool_label="USDC/ETH 0.05%"),
    )
    runner.run_loop(interval_secs=3600)
"""

from src.notification_engine.telegram import TelegramNotifier, build_from_env as build_notifier

__all__ = ["TelegramNotifier", "build_notifier"]
