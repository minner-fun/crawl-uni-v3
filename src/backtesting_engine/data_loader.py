"""
历史数据加载器
==============

从 DB 读取回测所需的两类数据：
    HourlyBar  : pool_metrics_hourly 的逐行快照（价格、volume、流动性）
    DailyVTV   : pool_metrics_daily  的 volume/tvl_ratio（供策略层判断开/平仓信号）

辅助函数：
    price_close_to_tick : human-readable price → V3 tick
    load_pool_meta      : 读取 tick_spacing / fee_tier / decimals
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Pool, PoolMetricsDaily, PoolMetricsHourly, Token


# ---------------------------------------------------------------------------
# HourlyBar
# ---------------------------------------------------------------------------

@dataclass
class HourlyBar:
    metric_hour:          datetime
    price_open:           float    # 1 token0 = X token1（人类可读）
    price_close:          float
    price_high:           float
    price_low:            float
    volume_token0_raw:    int      # 绝对值（swap volume，6 decimals for USDC）
    volume_token1_raw:    int      # 绝对值（18 decimals for WETH）
    fee_token0_raw:       int      # 全池手续费 = volume × fee_rate（已存 DB）
    fee_token1_raw:       int
    pool_close_liquidity: int      # 小时末全池活跃流动性（L 单位）
    eth_price_usdc:       float    # 1 token1 in token0 terms = 1/price_close（ETH 价格）


def load_hourly_bars(
    session: Session,
    pool_address: str,
    from_dt: datetime,
    to_dt: datetime,
    chain_id: int = 1,
) -> list[HourlyBar]:
    """
    加载指定时间范围内的小时 K 线，按时间升序排列。
    跳过 price_close 为 NULL 或零的行（Data Engine 尚未处理的区块）。
    """
    rows = session.execute(
        select(PoolMetricsHourly)
        .where(
            PoolMetricsHourly.pool_address == pool_address,
            PoolMetricsHourly.chain_id     == chain_id,
            PoolMetricsHourly.metric_hour  >= from_dt,
            PoolMetricsHourly.metric_hour  <= to_dt,
        )
        .order_by(PoolMetricsHourly.metric_hour.asc())
    ).scalars().all()

    bars: list[HourlyBar] = []
    for r in rows:
        price_close = float(r.price_close) if r.price_close else None
        if not price_close or price_close <= 0:
            continue

        price_open = float(r.price_open) if r.price_open else price_close

        bars.append(HourlyBar(
            metric_hour          = r.metric_hour,
            price_open           = price_open,
            price_close          = price_close,
            price_high           = float(r.price_high) if r.price_high else price_close,
            price_low            = float(r.price_low)  if r.price_low  else price_close,
            volume_token0_raw    = abs(int(r.volume_token0_raw or 0)),
            volume_token1_raw    = abs(int(r.volume_token1_raw or 0)),
            fee_token0_raw       = abs(int(r.fee_token0_raw    or 0)),
            fee_token1_raw       = abs(int(r.fee_token1_raw    or 0)),
            pool_close_liquidity = int(r.close_liquidity or 0),
            eth_price_usdc       = 1.0 / price_close,   # price_token1 = 1/price_token0
        ))

    return bars


# ---------------------------------------------------------------------------
# Daily VTV
# ---------------------------------------------------------------------------

def load_daily_vtv(
    session: Session,
    pool_address: str,
    from_date: date,
    to_date: date,
    chain_id: int = 1,
) -> dict[date, float]:
    """
    加载 [from_date, to_date] 范围内的日 volume/tvl_ratio。
    返回 {metric_date: ratio}，NULL 值跳过。
    """
    rows = session.execute(
        select(PoolMetricsDaily.metric_date, PoolMetricsDaily.volume_tvl_ratio)
        .where(
            PoolMetricsDaily.pool_address == pool_address,
            PoolMetricsDaily.chain_id     == chain_id,
            PoolMetricsDaily.metric_date  >= from_date,
            PoolMetricsDaily.metric_date  <= to_date,
        )
        .order_by(PoolMetricsDaily.metric_date.asc())
    ).all()

    return {
        row.metric_date: float(row.volume_tvl_ratio)
        for row in rows
        if row.volume_tvl_ratio is not None
    }


# ---------------------------------------------------------------------------
# Pool 元数据
# ---------------------------------------------------------------------------

@dataclass
class PoolMeta:
    tick_spacing: int
    fee_tier:     int    # e.g. 500 (0.05%), 3000 (0.3%)
    fee_rate:     float  # fee_tier / 1_000_000
    decimals0:    int
    decimals1:    int
    token0:       str
    token1:       str


def load_pool_meta(session: Session, pool_address: str) -> PoolMeta:
    """读取 pool + token 元数据。"""
    pool = session.execute(
        select(Pool).where(Pool.pool_address == pool_address)
    ).scalar_one_or_none()

    if pool is None:
        raise ValueError(f"Pool {pool_address} 不存在于 DB，请先运行爬虫。")

    token0 = session.execute(
        select(Token).where(Token.token_address == pool.token0_address)
    ).scalar_one_or_none()

    token1 = session.execute(
        select(Token).where(Token.token_address == pool.token1_address)
    ).scalar_one_or_none()

    return PoolMeta(
        tick_spacing = pool.tick_spacing,
        fee_tier     = pool.fee,
        fee_rate     = pool.fee / 1_000_000,
        decimals0    = token0.decimals if token0 else 6,
        decimals1    = token1.decimals if token1 else 18,
        token0       = pool.token0_address,
        token1       = pool.token1_address,
    )


# ---------------------------------------------------------------------------
# Tick 转换工具
# ---------------------------------------------------------------------------

def price_close_to_tick(price_close: float, decimals0: int, decimals1: int) -> int:
    """
    将人类可读价格（1 token0 = X token1）转换为 Uniswap V3 tick。

    推导：
        price_raw = price_close * 10^(decimals1 - decimals0)
            对于 USDC(6)/WETH(18)：price_raw = price_close * 10^12
            若 price_close ≈ 0.000333 → price_raw ≈ 3.33e8
        tick = floor(log(price_raw) / log(1.0001))
            ≈ floor(19.624 / 9.9995e-5) ≈ 196,250  (ETH ≈ $3000)
    """
    if price_close <= 0:
        return 0
    price_raw = price_close * (10 ** (decimals1 - decimals0))
    return math.floor(math.log(price_raw) / math.log(1.0001))


def tick_to_sqrt_price(tick: int) -> float:
    """tick → sqrtPrice（浮点数，用于 V3 仓位计算）。"""
    return math.sqrt(1.0001 ** tick)
