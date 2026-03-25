from src.notification_engine import build_notifier
from src.strategy_engine import StrategyRunner, PoolConfig

runner = StrategyRunner(
    strategy         = VolumeRebalanceStrategy(),
    position_manager = build_position_manager(),
    pool_config      = PoolConfig(pool_address="0x88e6..."),
    notifier         = build_notifier(pool_label="USDC/ETH 0.05%"),
)

# 先测试连通性
runner.notifier.test_connection()

runner.run_loop(interval_secs=3600)