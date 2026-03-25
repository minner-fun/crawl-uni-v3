"""
backtesting_engine
==================
Uniswap V3 LP 策略历史回测引擎。

用法示例::

    from datetime import datetime
    from src.backtesting_engine import BacktestSimulator, BacktestConfig
    from src.strategy_engine.strategies import VolumeRebalanceStrategy
    from src.Constracts import UNISWAP_V3_USDC_ETH_POOL_ADDRESS

    simulator = BacktestSimulator(
        strategy = VolumeRebalanceStrategy(),
        config   = BacktestConfig(
            pool_address  = UNISWAP_V3_USDC_ETH_POOL_ADDRESS,
            from_dt       = datetime(2024, 1, 1),
            to_dt         = datetime(2024, 12, 31),
            initial_usdc  = 200.0,
        ),
    )

    result = simulator.run()
    result.print_report()

    # 可选：导出 DataFrame 进行可视化
    df = result.to_dataframe()
    df["portfolio_value_usdc"].plot(title="净值曲线")
"""

from src.backtesting_engine.simulator import BacktestConfig, BacktestSimulator
from src.backtesting_engine.metrics import BacktestResult

__all__ = [
    "BacktestSimulator",
    "BacktestConfig",
    "BacktestResult",
]
