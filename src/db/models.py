from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func

# 链上 uint256 / int256 统一用 NUMERIC(78, 0)，PostgreSQL NUMERIC 是有符号的，
# 可以正确表示 Swap 事件中 int256 的负值。
_N78 = Numeric(precision=78, scale=0)


class Base(DeclarativeBase):
    pass


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
