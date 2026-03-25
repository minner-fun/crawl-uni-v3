from datetime import datetime
from src.backtesting_engine import BacktestSimulator, BacktestConfig
from src.strategy_engine.strategies import VolumeRebalanceStrategy
from src.Constracts import UNISWAP_V3_USDC_ETH_POOL_ADDRESS

result = BacktestSimulator(
    strategy = VolumeRebalanceStrategy(),
    config   = BacktestConfig(
        pool_address = UNISWAP_V3_USDC_ETH_POOL_ADDRESS,
        from_dt      = datetime(2024, 1, 1),
        to_dt        = datetime(2024, 12, 31),
    ),
).run()

result.print_report()
df = result.to_dataframe()   # 导出为 pandas DataFrame 做可视化