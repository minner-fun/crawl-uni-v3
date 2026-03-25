"""
strategy_engine
===============
策略层公共入口，供外部直接导入核心组件。

典型用法::

    from src.execution_engine import build_position_manager
    from src.strategy_engine import StrategyRunner, PoolConfig
    from src.strategy_engine.strategies import VolumeRebalanceStrategy
    from src.Constracts import UNISWAP_V3_USDC_ETH_POOL_ADDRESS

    pm = build_position_manager()
    runner = StrategyRunner(
        strategy         = VolumeRebalanceStrategy(),
        position_manager = pm,
        pool_config      = PoolConfig(pool_address=UNISWAP_V3_USDC_ETH_POOL_ADDRESS),
    )
    runner.run_once()
    # 或定时循环：runner.run_loop(interval_secs=3600)
"""

from src.strategy_engine.base import BaseStrategy, Decision, StrategyDecision
from src.strategy_engine.context import ActivePosition, MarketContext, build_context, get_active_position
from src.strategy_engine.runner import PoolConfig, StrategyRunner

__all__ = [
    "StrategyRunner",
    "PoolConfig",
    "BaseStrategy",
    "Decision",
    "StrategyDecision",
    "MarketContext",
    "ActivePosition",
    "build_context",
    "get_active_position",
]
