from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func

# 链上 uint256 / int256 统一用 NUMERIC(78, 0)，PostgreSQL NUMERIC 是有符号的，
# 可以正确表示 Swap 事件中 int256 的负值。
_N78 = Numeric(precision=78, scale=0)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 区块时间戳持久化缓存
# ---------------------------------------------------------------------------

class Block(Base):
    """
    区块时间戳的持久化缓存表。
    每个区块只从链上查一次，后续采集直接走 DB，节省 RPC CU 消耗。
    使用 (chain_id, block_number) 联合主键，为多链扩展预留空间。
    """
    __tablename__ = "blocks"

    chain_id        = Column(Integer, nullable=False, default=1, primary_key=True)
    block_number    = Column(BigInteger, nullable=False, primary_key=True)
    block_timestamp = Column(DateTime, nullable=False)


# ---------------------------------------------------------------------------
# 第一层：token / pool 基础信息
# ---------------------------------------------------------------------------

class Token(Base):
    __tablename__ = "tokens"

    token_address = Column(String(42), primary_key=True)
    symbol        = Column(String(32))
    name          = Column(String(128))
    decimals      = Column(Integer, nullable=False)
    chain_id      = Column(Integer, nullable=False, default=1)
    created_at    = Column(DateTime, nullable=False, server_default=func.now())
    updated_at    = Column(DateTime, nullable=False, server_default=func.now(),
                           onupdate=func.now())

    pools_as_token0 = relationship(
        "Pool", foreign_keys="Pool.token0_address", back_populates="token0"
    )
    pools_as_token1 = relationship(
        "Pool", foreign_keys="Pool.token1_address", back_populates="token1"
    )


class Pool(Base):
    __tablename__ = "pools"

    pool_address    = Column(String(42), primary_key=True)
    chain_id        = Column(Integer, nullable=False, default=1)
    token0_address  = Column(
        String(42), ForeignKey("tokens.token_address"), nullable=False
    )
    token1_address  = Column(
        String(42), ForeignKey("tokens.token_address"), nullable=False
    )
    fee             = Column(Integer, nullable=False)
    tick_spacing    = Column(Integer, nullable=False)
    created_block   = Column(BigInteger, nullable=False)
    created_tx_hash = Column(String(66), nullable=False)
    created_at      = Column(DateTime, nullable=False, server_default=func.now())
    updated_at      = Column(DateTime, nullable=False, server_default=func.now(),
                             onupdate=func.now())

    token0 = relationship(
        "Token", foreign_keys=[token0_address], back_populates="pools_as_token0"
    )
    token1 = relationship(
        "Token", foreign_keys=[token1_address], back_populates="pools_as_token1"
    )

    __table_args__ = (
        Index("idx_pools_token0", "token0_address"),
        Index("idx_pools_token1", "token1_address"),
        Index("idx_pools_fee", "fee"),
        Index("idx_pools_token_pair_fee", "token0_address", "token1_address", "fee"),
    )


# ---------------------------------------------------------------------------
# 第二层：原始事件表（每张表都以 (tx_hash, log_index) 作唯一约束）
# ---------------------------------------------------------------------------

class Swap(Base):
    __tablename__ = "swaps"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    chain_id        = Column(Integer, nullable=False, default=1)
    pool_address    = Column(
        String(42), ForeignKey("pools.pool_address"), nullable=False
    )
    block_number    = Column(BigInteger, nullable=False)
    block_timestamp = Column(DateTime, nullable=False)
    tx_hash         = Column(String(66), nullable=False)
    log_index       = Column(Integer, nullable=False)

    sender          = Column(String(42), nullable=False)
    recipient       = Column(String(42), nullable=False)

    # int256 on-chain — NUMERIC(78,0) 可存负值
    amount0_raw     = Column(_N78, nullable=False)
    amount1_raw     = Column(_N78, nullable=False)

    sqrt_price_x96  = Column(_N78, nullable=False)
    liquidity       = Column(_N78, nullable=False)
    tick            = Column(Integer, nullable=False)

    created_at      = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_swaps_tx_log"),
        Index("idx_swaps_pool_address", "pool_address"),
        Index("idx_swaps_block_timestamp", "block_timestamp"),
        Index("idx_swaps_pool_time", "pool_address", "block_timestamp"),
        Index("idx_swaps_block_number", "block_number"),
    )


class Mint(Base):
    __tablename__ = "mints"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    chain_id         = Column(Integer, nullable=False, default=1)
    pool_address     = Column(
        String(42), ForeignKey("pools.pool_address"), nullable=False
    )
    block_number     = Column(BigInteger, nullable=False)
    block_timestamp  = Column(DateTime, nullable=False)
    tx_hash          = Column(String(66), nullable=False)
    log_index        = Column(Integer, nullable=False)

    sender           = Column(String(42), nullable=False)
    owner            = Column(String(42), nullable=False)
    tick_lower       = Column(Integer, nullable=False)
    tick_upper       = Column(Integer, nullable=False)

    amount_liquidity = Column(_N78, nullable=False)
    amount0_raw      = Column(_N78, nullable=False)
    amount1_raw      = Column(_N78, nullable=False)

    created_at       = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_mints_tx_log"),
        Index("idx_mints_pool_address", "pool_address"),
        Index("idx_mints_block_timestamp", "block_timestamp"),
        Index("idx_mints_pool_time", "pool_address", "block_timestamp"),
        Index("idx_mints_owner", "owner"),
    )


class Burn(Base):
    __tablename__ = "burns"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    chain_id         = Column(Integer, nullable=False, default=1)
    pool_address     = Column(
        String(42), ForeignKey("pools.pool_address"), nullable=False
    )
    block_number     = Column(BigInteger, nullable=False)
    block_timestamp  = Column(DateTime, nullable=False)
    tx_hash          = Column(String(66), nullable=False)
    log_index        = Column(Integer, nullable=False)

    # 修正：链上 Burn.owner 是 indexed address，不可为 NULL
    owner            = Column(String(42), nullable=False)
    tick_lower       = Column(Integer, nullable=False)
    tick_upper       = Column(Integer, nullable=False)

    amount_liquidity = Column(_N78, nullable=False)
    amount0_raw      = Column(_N78, nullable=False)
    amount1_raw      = Column(_N78, nullable=False)

    created_at       = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_burns_tx_log"),
        Index("idx_burns_pool_address", "pool_address"),
        Index("idx_burns_block_timestamp", "block_timestamp"),
        Index("idx_burns_pool_time", "pool_address", "block_timestamp"),
    )


class Collect(Base):
    __tablename__ = "collects"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    chain_id        = Column(Integer, nullable=False, default=1)
    pool_address    = Column(
        String(42), ForeignKey("pools.pool_address"), nullable=False
    )
    block_number    = Column(BigInteger, nullable=False)
    block_timestamp = Column(DateTime, nullable=False)
    tx_hash         = Column(String(66), nullable=False)
    log_index       = Column(Integer, nullable=False)

    owner           = Column(String(42), nullable=False)
    recipient       = Column(String(42), nullable=False)
    tick_lower      = Column(Integer, nullable=False)
    tick_upper      = Column(Integer, nullable=False)

    amount0_raw     = Column(_N78, nullable=False)
    amount1_raw     = Column(_N78, nullable=False)

    created_at      = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_collects_tx_log"),
        Index("idx_collects_pool_address", "pool_address"),
        Index("idx_collects_block_timestamp", "block_timestamp"),
        Index("idx_collects_owner", "owner"),
        Index("idx_collects_pool_time", "pool_address", "block_timestamp"),
    )


# ---------------------------------------------------------------------------
# 爬取进度表：记录每个 pool 已同步到的最新区块，支持断点续爬
# ---------------------------------------------------------------------------

class SyncCursor(Base):
    """
    记录每个 pool（或 factory）爬虫的进度。
    target_type: 'factory' | 'pool'
    target_address: factory 合约地址 或 pool 合约地址
    last_synced_block: 已成功写入数据库的最新区块号
    """
    __tablename__ = "sync_cursors"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    chain_id       = Column(Integer, nullable=False, default=1)
    target_type    = Column(String(16), nullable=False)
    target_address = Column(String(42), nullable=False)
    last_synced_block = Column(BigInteger, nullable=False)
    updated_at     = Column(DateTime, nullable=False, server_default=func.now(),
                            onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "chain_id", "target_type", "target_address",
            name="uq_sync_cursors_target"
        ),
    )


# ---------------------------------------------------------------------------
# 第三层：快照与聚合表（Data Engine 中间表）
# ---------------------------------------------------------------------------

_N38_18 = Numeric(precision=38, scale=18)   # 人类可读的价格 / USD 金额
_N20_10 = Numeric(precision=20, scale=10)   # 比率 / 概率类指标


class PoolPriceSnapshot(Base):
    """
    价格快照：每个区块取该块最后一笔 Swap（log_index 最大）的状态。
    用途：OHLC 构建、波动率计算、TVL 估算。

    price_token0：1 个 token0 能换多少 token1（人类可读，已调整 decimals）
    price_token1：1 个 token1 能换多少 token0 = 1 / price_token0
    例如 USDC/WETH 池：price_token0 ≈ 0.0003，price_token1 ≈ 3000
    """
    __tablename__ = "pool_price_snapshots"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    pool_address    = Column(String(42), ForeignKey("pools.pool_address"), nullable=False)
    chain_id        = Column(Integer, nullable=False, default=1)
    block_number    = Column(BigInteger, nullable=False)
    block_timestamp = Column(DateTime, nullable=False)

    sqrt_price_x96  = Column(_N78, nullable=False)
    tick            = Column(Integer, nullable=False)
    liquidity       = Column(_N78, nullable=False)

    price_token0    = Column(_N38_18)
    price_token1    = Column(_N38_18)

    created_at      = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("pool_address", "block_number", name="uq_snapshots_pool_block"),
        Index("idx_snapshots_pool_time", "pool_address", "block_timestamp"),
        Index("idx_snapshots_block_time", "block_timestamp"),
    )


class PoolMetricsHourly(Base):
    """
    小时聚合指标。

    volume_usd / fee_usd 仅在稳定币配对池中填充
    （由 data_engine 判断 token0/token1 哪侧是稳定币）。
    fee_token0_raw / fee_token1_raw = volume_raw * fee_rate，近似值。
    """
    __tablename__ = "pool_metrics_hourly"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    pool_address    = Column(String(42), ForeignKey("pools.pool_address"), nullable=False)
    chain_id        = Column(Integer, nullable=False, default=1)
    metric_hour     = Column(DateTime, nullable=False)

    price_open      = Column(_N38_18)
    price_close     = Column(_N38_18)
    price_high      = Column(_N38_18)
    price_low       = Column(_N38_18)

    volume_token0_raw = Column(_N78, nullable=False, default=0)
    volume_token1_raw = Column(_N78, nullable=False, default=0)
    volume_usd        = Column(_N38_18)              # nullable：非稳定币对不填

    swap_count      = Column(Integer, nullable=False, default=0)
    mint_count      = Column(Integer, nullable=False, default=0)
    burn_count      = Column(Integer, nullable=False, default=0)
    collect_count   = Column(Integer, nullable=False, default=0)

    fee_token0_raw  = Column(_N78)
    fee_token1_raw  = Column(_N78)
    fee_usd         = Column(_N38_18)                # nullable：非稳定币对不填

    avg_liquidity   = Column(_N78)
    close_liquidity = Column(_N78)

    created_at      = Column(DateTime, nullable=False, server_default=func.now())
    updated_at      = Column(DateTime, nullable=False, server_default=func.now(),
                             onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("pool_address", "metric_hour", name="uq_hourly_pool_hour"),
        Index("idx_hourly_pool_time", "pool_address", "metric_hour"),
    )


class PoolMetricsDaily(Base):
    """
    日聚合指标，策略模块的主要数据源。

    il_estimate_fullrange_1d：按全范围（V2 式）假设计算的 IL，
        仅作参考，窄范围仓位的真实 IL 会更大。
    tvl_estimate_usd：基于 mint/burn 累计净值估算，仅对稳定币对有效。
    volatility_1d：当日 24 根小时 K 线收盘价的对数收益率标准差。
    """
    __tablename__ = "pool_metrics_daily"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    pool_address    = Column(String(42), ForeignKey("pools.pool_address"), nullable=False)
    chain_id        = Column(Integer, nullable=False, default=1)
    metric_date     = Column(Date, nullable=False)

    price_open      = Column(_N38_18)
    price_close     = Column(_N38_18)
    price_high      = Column(_N38_18)
    price_low       = Column(_N38_18)

    volume_token0_raw  = Column(_N78, nullable=False, default=0)
    volume_token1_raw  = Column(_N78, nullable=False, default=0)
    volume_usd         = Column(_N38_18)
    fee_usd            = Column(_N38_18)
    tvl_estimate_usd   = Column(_N38_18)

    swap_count      = Column(Integer, nullable=False, default=0)
    mint_count      = Column(Integer, nullable=False, default=0)
    burn_count      = Column(Integer, nullable=False, default=0)
    collect_count   = Column(Integer, nullable=False, default=0)

    volatility_1d              = Column(_N20_10)
    volume_tvl_ratio           = Column(_N20_10)
    fee_apr                    = Column(_N20_10)
    il_estimate_fullrange_1d   = Column(_N20_10)

    created_at      = Column(DateTime, nullable=False, server_default=func.now())
    updated_at      = Column(DateTime, nullable=False, server_default=func.now(),
                             onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("pool_address", "metric_date", name="uq_daily_pool_date"),
        Index("idx_daily_pool_date", "pool_address", "metric_date"),
    )


# ---------------------------------------------------------------------------
# 第四层：策略执行状态表
# ---------------------------------------------------------------------------

class LpPosition(Base):
    """
    记录策略开出的每一个 LP 仓位。
    position_id = str(NFT tokenId)，全局唯一。
    """
    __tablename__ = "lp_positions"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    position_id   = Column(String(128), unique=True, nullable=False)
    pool_address  = Column(String(42), ForeignKey("pools.pool_address"), nullable=False)
    owner_address = Column(String(42), nullable=False)
    tick_lower    = Column(Integer, nullable=False)
    tick_upper    = Column(Integer, nullable=False)
    liquidity     = Column(Numeric(78, 0), nullable=False, default=0)
    opened_at     = Column(DateTime, nullable=False)
    closed_at     = Column(DateTime)
    status        = Column(String(32), nullable=False, default="OPEN")
    created_at    = Column(DateTime, nullable=False, server_default=func.now())
    updated_at    = Column(DateTime, nullable=False, server_default=func.now(),
                           onupdate=func.now())

    __table_args__ = (
        Index("idx_lp_positions_pool",   "pool_address"),
        Index("idx_lp_positions_status", "status"),
    )


class LpPositionAction(Base):
    """
    记录每个仓位的生命周期动作：OPEN / COLLECT / REBALANCE_CLOSE / REBALANCE_OPEN / CLOSE。
    """
    __tablename__ = "lp_position_actions"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    position_id  = Column(String(128), nullable=False)
    action_type  = Column(String(32),  nullable=False)
    tx_hash      = Column(String(66))
    block_number = Column(BigInteger)
    action_time  = Column(DateTime, nullable=False)
    metadata     = Column(JSONB)
    created_at   = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_lp_actions_position_id", "position_id"),
        Index("idx_lp_actions_type",        "action_type"),
    )


class StrategySignal(Base):
    """
    策略每次运行的决策快照，用于审计与离线回测。
    signal_type: OPEN | HOLD | REBALANCE | CLOSE
    signal_score: 触发信号的量化分数（如 avg volume/tvl）
    """
    __tablename__ = "strategy_signals"

    id                      = Column(BigInteger, primary_key=True, autoincrement=True)
    pool_address            = Column(String(42), ForeignKey("pools.pool_address"), nullable=False)
    chain_id                = Column(Integer, nullable=False, default=1)
    signal_time             = Column(DateTime, nullable=False)
    signal_type             = Column(String(64), nullable=False)
    signal_score            = Column(Numeric(20, 10))
    recommended_lower_price = Column(Numeric(38, 18))
    recommended_upper_price = Column(Numeric(38, 18))
    expected_fee_apr        = Column(Numeric(20, 10))
    expected_il             = Column(Numeric(20, 10))
    expected_net_apr        = Column(Numeric(20, 10))
    reason                  = Column(JSONB)
    created_at              = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_strategy_signals_pool_time", "pool_address", "signal_time"),
        Index("idx_strategy_signals_type",      "signal_type"),
    )
