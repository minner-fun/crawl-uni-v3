from src.db.database import get_session, init_db
from datetime import date, datetime, timedelta
from src.data_engine import price_snapshot, hourly_metrics, daily_metrics
from src.data_engine.strategy_indicators import build_strategy_indicators

from src.Constracts import UNISWAP_V3_USDC_ETH_POOL_ADDRESS
init_db()

pool_address = UNISWAP_V3_USDC_ETH_POOL_ADDRESS


with get_session() as session:
    build_strategy_indicators(
        session,
        pool_address = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
        fee          = 500,
        symbol0      = "USDC",
        symbol1      = "WETH",
        decimals0    = 6,
        decimals1    = 18,
        metric_hour  = datetime(2024, 12, 29, 12, 0),
    )





# with get_session() as session:
#     # 1. 构建价格快照（每块最后一笔 Swap）
#     price_snapshot.build_price_snapshots(
#         session, pool_address, decimals0=6, decimals1=18,
#         from_block=24334542, to_block=24637017
#     )

#     # 2. 聚合小时指标（依赖 swaps + price_snapshots）
#     hourly_metrics.build_hourly_metrics(
#         session, pool_address, fee=500,
#         symbol0="USDC", symbol1="WETH",
#         decimals0=6, decimals1=18,
#         from_time=datetime(2026, 1, 28), to_time=datetime(2026, 3, 12)
#     )

#     # 3. 聚合日指标（依赖 hourly_metrics）
#     daily_metrics.build_daily_metrics(
#         session, pool_address, fee=500,
#         symbol0="USDC", symbol1="WETH",
#         decimals0=6, decimals1=18,
#         from_date=date(2026, 1, 28), to_date=date(2026, 3, 12)
#     )