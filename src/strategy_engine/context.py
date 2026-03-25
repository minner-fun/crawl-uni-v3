"""
市场上下文构建
==============

MarketContext  : 策略评估所需的所有只读数据
ActivePosition : DB 中记录的当前活跃仓位快照

build_context()       : 从 DB 读取价格快照 + 近期日指标，组装 MarketContext
get_active_position() : 从 lp_positions 读取 OPEN 状态仓位
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from src.db import repository as repo


@dataclass
class MarketContext:
    """策略评估所需的市场快照，来自 DB（price_snapshots + pool_metrics_daily + pools）。"""

    pool_address: str
    chain_id:     int

    # 来自最新 pool_price_snapshots
    current_tick:    int
    sqrt_price_x96:  int
    current_liquidity: int
    price_token0:    Optional[Decimal]   # 1 token0 = X token1（人类可读）
    price_token1:    Optional[Decimal]   # 1 token1 = X token0（人类可读）

    # 来自 pools + tokens
    tick_spacing: int
    fee:          int
    token0:       str
    token1:       str
    decimals0:    int
    decimals1:    int

    # 来自近 n_days 天 pool_metrics_daily 的均值
    avg_volume_tvl_ratio: Optional[Decimal]
    latest_fee_apr:       Optional[Decimal]
    latest_tvl_usd:       Optional[Decimal]
    n_days:               int


@dataclass
class ActivePosition:
    """DB 中 lp_positions 记录的活跃仓位，供策略层只读使用。"""

    position_id: str   # str(NFT tokenId)
    token_id:    int
    tick_lower:  int
    tick_upper:  int
    liquidity:   int
    status:      str


def build_context(
    session: Session,
    pool_address: str,
    n_days: int = 3,
    chain_id: int = 1,
) -> MarketContext:
    """
    从 DB 组装 MarketContext。

    依赖：
        - pools 表已有该 pool 的记录
        - pool_price_snapshots 表已有至少一条快照
        - pool_metrics_daily 最近 n_days 天有数据（无数据时 avg_volume_tvl_ratio=None）

    Raises
    ------
    ValueError
        pool 不存在或无任何价格快照时抛出。
    """
    pool = repo.get_pool(session, pool_address)
    if pool is None:
        raise ValueError(f"Pool {pool_address} 不存在于 DB，请先运行爬虫。")

    snapshot = repo.get_latest_price_snapshot(session, pool_address, chain_id)
    if snapshot is None:
        raise ValueError(f"Pool {pool_address} 暂无价格快照，请先运行 price_snapshot 构建。")

    # 代币 decimals
    token0_obj = repo.get_token(session, pool.token0_address)
    token1_obj = repo.get_token(session, pool.token1_address)
    decimals0 = token0_obj.decimals if token0_obj else 6
    decimals1 = token1_obj.decimals if token1_obj else 18

    # 近 n_days 日指标
    daily_rows = repo.get_recent_daily_metrics(session, pool_address, n_days, chain_id)

    vtv_vals = [
        Decimal(str(r.volume_tvl_ratio))
        for r in daily_rows
        if r.volume_tvl_ratio is not None
    ]
    avg_vtv = sum(vtv_vals) / len(vtv_vals) if vtv_vals else None

    latest = daily_rows[0] if daily_rows else None

    return MarketContext(
        pool_address=pool_address,
        chain_id=chain_id,
        current_tick=int(snapshot.tick),
        sqrt_price_x96=int(snapshot.sqrt_price_x96),
        current_liquidity=int(snapshot.liquidity),
        price_token0=Decimal(str(snapshot.price_token0)) if snapshot.price_token0 else None,
        price_token1=Decimal(str(snapshot.price_token1)) if snapshot.price_token1 else None,
        tick_spacing=pool.tick_spacing,
        fee=pool.fee,
        token0=pool.token0_address,
        token1=pool.token1_address,
        decimals0=decimals0,
        decimals1=decimals1,
        avg_volume_tvl_ratio=avg_vtv,
        latest_fee_apr=Decimal(str(latest.fee_apr)) if (latest and latest.fee_apr) else None,
        latest_tvl_usd=Decimal(str(latest.tvl_estimate_usd)) if (latest and latest.tvl_estimate_usd) else None,
        n_days=n_days,
    )


def get_active_position(
    session: Session,
    pool_address: str,
    chain_id: int = 1,   # 保留参数，便于未来多链扩展
) -> Optional[ActivePosition]:
    """
    从 lp_positions 读取当前 OPEN 状态仓位。
    同一时刻同一 pool 只存在一个 OPEN 仓位（策略保证）。
    无仓位时返回 None。
    """
    lp = repo.get_active_lp_position(session, pool_address)
    if lp is None:
        return None
    return ActivePosition(
        position_id=lp.position_id,
        token_id=int(lp.position_id),
        tick_lower=lp.tick_lower,
        tick_upper=lp.tick_upper,
        liquidity=int(lp.liquidity),
        status=lp.status,
    )
