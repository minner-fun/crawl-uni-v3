"""
数据访问层（Repository）

所有写操作均使用 PostgreSQL 的 INSERT ... ON CONFLICT 语义，保证幂等性：
- 重复运行爬虫不会产生重复数据，也不会抛异常。
- 调用方只需传入从链上解析得到的字段字典，无需手动处理冲突。

调用示例：
    from src.db.database import get_session
    from src.db import repository as repo

    with get_session() as session:
        repo.upsert_token(session, {
            "token_address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "symbol": "USDC",
            "name": "USD Coin",
            "decimals": 6,
            "chain_id": 1,
        })
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.db.models import (
    Block, Burn, Collect, Mint, Pool, Swap, SyncCursor, Token,
    PoolPriceSnapshot, PoolMetricsHourly, PoolMetricsDaily,
    PoolStrategyIndicators,
    LpPosition, LpPositionAction, StrategySignal,
)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def upsert_token(session: Session, data: dict) -> None:
    """
    插入 token，若 token_address 已存在则更新 symbol / name / decimals。
    token_address 必须已经 checksum 化（大小写规范）。
    """
    stmt = (
        pg_insert(Token)
        .values(**data)
        .on_conflict_do_update(
            index_elements=["token_address"],
            set_={
                "symbol":     pg_insert(Token).excluded.symbol,
                "name":       pg_insert(Token).excluded.name,
                "decimals":   pg_insert(Token).excluded.decimals,
                "updated_at": datetime.utcnow(),
            },
        )
    )
    session.execute(stmt)


def get_token(session: Session, token_address: str) -> Optional[Token]:
    return session.get(Token, token_address)


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

def upsert_pool(session: Session, data: dict) -> None:
    """
    插入 pool。pool 一旦创建链上不可变，冲突时忽略（DO NOTHING）。
    """
    stmt = (
        pg_insert(Pool)
        .values(**data)
        .on_conflict_do_nothing(index_elements=["pool_address"])
    )
    session.execute(stmt)


def get_pool(session: Session, pool_address: str) -> Optional[Pool]:
    return session.get(Pool, pool_address)


def pool_exists(session: Session, pool_address: str) -> bool:
    return session.get(Pool, pool_address) is not None


# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------

def insert_swap(session: Session, data: dict) -> bool:
    """
    插入一条 Swap 事件。
    返回 True 表示成功插入，False 表示 (tx_hash, log_index) 已存在（跳过）。
    """
    stmt = (
        pg_insert(Swap)
        .values(**data)
        .on_conflict_do_nothing(constraint="uq_swaps_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount > 0


def bulk_insert_swaps(session: Session, data_list: list[dict]) -> int:
    """批量插入 Swap 事件，跳过重复项，返回实际插入的行数。"""
    if not data_list:
        return 0
    stmt = (
        pg_insert(Swap)
        .values(data_list)
        .on_conflict_do_nothing(constraint="uq_swaps_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------

def insert_mint(session: Session, data: dict) -> bool:
    stmt = (
        pg_insert(Mint)
        .values(**data)
        .on_conflict_do_nothing(constraint="uq_mints_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount > 0


def bulk_insert_mints(session: Session, data_list: list[dict]) -> int:
    if not data_list:
        return 0
    stmt = (
        pg_insert(Mint)
        .values(data_list)
        .on_conflict_do_nothing(constraint="uq_mints_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# Burn
# ---------------------------------------------------------------------------

def insert_burn(session: Session, data: dict) -> bool:
    stmt = (
        pg_insert(Burn)
        .values(**data)
        .on_conflict_do_nothing(constraint="uq_burns_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount > 0


def bulk_insert_burns(session: Session, data_list: list[dict]) -> int:
    if not data_list:
        return 0
    stmt = (
        pg_insert(Burn)
        .values(data_list)
        .on_conflict_do_nothing(constraint="uq_burns_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------

def insert_collect(session: Session, data: dict) -> bool:
    stmt = (
        pg_insert(Collect)
        .values(**data)
        .on_conflict_do_nothing(constraint="uq_collects_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount > 0


def bulk_insert_collects(session: Session, data_list: list[dict]) -> int:
    if not data_list:
        return 0
    stmt = (
        pg_insert(Collect)
        .values(data_list)
        .on_conflict_do_nothing(constraint="uq_collects_tx_log")
    )
    result = session.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# Block（区块时间戳持久化缓存）
# ---------------------------------------------------------------------------

def get_block_timestamp(
    session: Session,
    chain_id: int,
    block_number: int,
) -> Optional[datetime]:
    """
    从 DB 中读取区块时间戳。
    未命中返回 None，调用方负责从链上获取后调用 upsert_block 写入。
    """
    row = session.get(Block, (chain_id, block_number))
    return row.block_timestamp if row else None


def upsert_block(
    session: Session,
    chain_id: int,
    block_number: int,
    block_timestamp: datetime,
) -> None:
    """写入区块时间戳，已存在则忽略（区块时间戳链上不可变）。"""
    stmt = (
        pg_insert(Block)
        .values(
            chain_id        = chain_id,
            block_number    = block_number,
            block_timestamp = block_timestamp,
        )
        .on_conflict_do_nothing()
    )
    session.execute(stmt)


def get_or_fetch_block_timestamps(
    session: Session,
    chain_id: int,
    block_numbers: set[int],
    rpc_fetcher,          # Callable[[int], datetime]，由调用方传入，避免 repo 依赖 web3
) -> dict[int, datetime]:
    """
    批量获取区块时间戳，DB 优先，缺失的调用 rpc_fetcher 从链上补充后持久化。

    参数：
        rpc_fetcher: 接受 block_number(int)，返回 datetime 的函数，
                     例如：lambda bn: datetime.utcfromtimestamp(w3.eth.get_block(bn)["timestamp"])

    返回：
        {block_number: datetime} 的完整映射

    示例：
        ts_map = repo.get_or_fetch_block_timestamps(
            session, CHAIN_ID, unique_blocks,
            rpc_fetcher=lambda bn: datetime.utcfromtimestamp(w3.eth.get_block(bn)["timestamp"])
        )
    """
    ts_map: dict[int, datetime] = {}
    missing: list[int] = []

    for bn in block_numbers:
        cached = get_block_timestamp(session, chain_id, bn)
        if cached is not None:
            ts_map[bn] = cached
        else:
            missing.append(bn)

    if missing:
        missing.sort()
        for bn in missing:
            ts = rpc_fetcher(bn)
            upsert_block(session, chain_id, bn, ts)
            ts_map[bn] = ts

    return ts_map


# ---------------------------------------------------------------------------
# SyncCursor（爬取进度）
# ---------------------------------------------------------------------------

def get_sync_cursor(
    session: Session,
    chain_id: int,
    target_type: str,
    target_address: str,
) -> Optional[int]:
    """
    获取某个 target 的最新已同步区块号。
    若从未同步过，返回 None。
    """
    row = (
        session.query(SyncCursor)
        .filter_by(
            chain_id=chain_id,
            target_type=target_type,
            target_address=target_address,
        )
        .one_or_none()
    )
    return row.last_synced_block if row else None


def update_sync_cursor(
    session: Session,
    chain_id: int,
    target_type: str,
    target_address: str,
    last_synced_block: int,
) -> None:
    """
    更新（或新建）某个 target 的爬取进度。
    通常在每批区块数据写入数据库之后调用，与业务写入在同一个 session / 事务中。
    """
    stmt = (
        pg_insert(SyncCursor)
        .values(
            chain_id=chain_id,
            target_type=target_type,
            target_address=target_address,
            last_synced_block=last_synced_block,
        )
        .on_conflict_do_update(
            constraint="uq_sync_cursors_target",
            set_={
                "last_synced_block": last_synced_block,
                "updated_at": datetime.utcnow(),
            },
        )
    )
    session.execute(stmt)


# ---------------------------------------------------------------------------
# 聚合表通用 upsert 辅助
# ---------------------------------------------------------------------------

def _upsert_agg(session: Session, model, data: dict, constraint: str) -> None:
    """
    通用聚合表 upsert：冲突时更新除主键和 created_at 之外的所有字段。
    供 price_snapshot / hourly / daily 三张表复用。
    """
    insert_stmt = pg_insert(model).values(**data)
    skip = {"id", "created_at"}
    update_cols = {
        col.name: insert_stmt.excluded[col.name]
        for col in model.__table__.columns
        if col.name not in skip and col.name in data
    }
    stmt = insert_stmt.on_conflict_do_update(
        constraint=constraint,
        set_=update_cols,
    )
    session.execute(stmt)


# ---------------------------------------------------------------------------
# PoolPriceSnapshot
# ---------------------------------------------------------------------------

def bulk_upsert_price_snapshots(session: Session, data_list: list[dict]) -> int:
    """批量写入价格快照，冲突（相同 pool + block）时更新价格字段。"""
    if not data_list:
        return 0
    insert_stmt = pg_insert(PoolPriceSnapshot).values(data_list)
    update_cols = {
        col.name: insert_stmt.excluded[col.name]
        for col in PoolPriceSnapshot.__table__.columns
        if col.name not in {"id", "created_at", "pool_address", "chain_id", "block_number"}
    }
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_snapshots_pool_block",
        set_=update_cols,
    )
    result = session.execute(stmt)
    return result.rowcount


def get_last_snapshot_block(
    session: Session, pool_address: str, chain_id: int = 1
) -> Optional[int]:
    """获取该 pool 已有快照的最大区块号，用于增量构建。"""
    from sqlalchemy import select, func as sa_func
    result = session.execute(
        select(sa_func.max(PoolPriceSnapshot.block_number)).where(
            PoolPriceSnapshot.pool_address == pool_address,
            PoolPriceSnapshot.chain_id == chain_id,
        )
    ).scalar()
    return result


# ---------------------------------------------------------------------------
# PoolMetricsHourly
# ---------------------------------------------------------------------------

def upsert_hourly_metrics(session: Session, data: dict) -> None:
    """写入或更新小时指标（冲突键：pool_address + metric_hour）。"""
    _upsert_agg(session, PoolMetricsHourly, data, "uq_hourly_pool_hour")


def get_last_hourly_metric_time(
    session: Session, pool_address: str, chain_id: int = 1
) -> Optional[datetime]:
    """获取该 pool 已有小时指标的最大 metric_hour，用于增量构建。"""
    from sqlalchemy import select, func as sa_func
    result = session.execute(
        select(sa_func.max(PoolMetricsHourly.metric_hour)).where(
            PoolMetricsHourly.pool_address == pool_address,
            PoolMetricsHourly.chain_id == chain_id,
        )
    ).scalar()
    return result


# ---------------------------------------------------------------------------
# PoolMetricsDaily
# ---------------------------------------------------------------------------

def upsert_daily_metrics(session: Session, data: dict) -> None:
    """写入或更新日指标（冲突键：pool_address + metric_date）。"""
    _upsert_agg(session, PoolMetricsDaily, data, "uq_daily_pool_date")


def get_last_daily_metric_date(
    session: Session, pool_address: str, chain_id: int = 1
) -> Optional[datetime]:
    """获取该 pool 已有日指标的最大 metric_date，用于增量构建。"""
    from sqlalchemy import select, func as sa_func
    result = session.execute(
        select(sa_func.max(PoolMetricsDaily.metric_date)).where(
            PoolMetricsDaily.pool_address == pool_address,
            PoolMetricsDaily.chain_id == chain_id,
        )
    ).scalar()
    return result


# ---------------------------------------------------------------------------
# 策略层专用查询
# ---------------------------------------------------------------------------

def get_latest_price_snapshot(
    session: Session, pool_address: str, chain_id: int = 1
) -> Optional[PoolPriceSnapshot]:
    """获取该 pool 最新的价格快照（block_number 最大的一条）。"""
    from sqlalchemy import select
    return session.execute(
        select(PoolPriceSnapshot)
        .where(
            PoolPriceSnapshot.pool_address == pool_address,
            PoolPriceSnapshot.chain_id == chain_id,
        )
        .order_by(PoolPriceSnapshot.block_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_recent_daily_metrics(
    session: Session, pool_address: str, n_days: int, chain_id: int = 1
) -> list[PoolMetricsDaily]:
    """
    获取该 pool 最近 n_days 天的日指标，按日期倒序。
    以当前 UTC 日期为基准，取 [today - n_days, today] 内的记录。
    """
    from sqlalchemy import select
    from datetime import timedelta, date
    cutoff = date.today() - timedelta(days=n_days)
    return list(
        session.execute(
            select(PoolMetricsDaily)
            .where(
                PoolMetricsDaily.pool_address == pool_address,
                PoolMetricsDaily.chain_id == chain_id,
                PoolMetricsDaily.metric_date >= cutoff,
            )
            .order_by(PoolMetricsDaily.metric_date.desc())
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# PoolStrategyIndicators
# ---------------------------------------------------------------------------

def upsert_strategy_indicators(session: Session, data: dict) -> None:
    """写入或更新策略指标（冲突键：pool_address + metric_hour）。"""
    _upsert_agg(session, PoolStrategyIndicators, data, "uq_strategy_indicators_pool_hour")


def get_latest_strategy_indicators(
    session: Session, pool_address: str, chain_id: int = 1
) -> Optional[PoolStrategyIndicators]:
    """获取该 pool 最新的策略指标记录（metric_hour 最大的一条）。"""
    from sqlalchemy import select
    return session.execute(
        select(PoolStrategyIndicators)
        .where(
            PoolStrategyIndicators.pool_address == pool_address,
            PoolStrategyIndicators.chain_id == chain_id,
        )
        .order_by(PoolStrategyIndicators.metric_hour.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_recent_strategy_indicators(
    session: Session, pool_address: str, n_hours: int, chain_id: int = 1
) -> list[PoolStrategyIndicators]:
    """获取该 pool 最近 n_hours 条策略指标，按时间倒序。"""
    from sqlalchemy import select
    return list(
        session.execute(
            select(PoolStrategyIndicators)
            .where(
                PoolStrategyIndicators.pool_address == pool_address,
                PoolStrategyIndicators.chain_id == chain_id,
            )
            .order_by(PoolStrategyIndicators.metric_hour.desc())
            .limit(n_hours)
        ).scalars().all()
    )


def get_last_strategy_indicators_hour(
    session: Session, pool_address: str, chain_id: int = 1
) -> Optional[datetime]:
    """获取已计算的最大 metric_hour，用于增量计算。"""
    from sqlalchemy import select, func as sa_func
    result = session.execute(
        select(sa_func.max(PoolStrategyIndicators.metric_hour)).where(
            PoolStrategyIndicators.pool_address == pool_address,
            PoolStrategyIndicators.chain_id == chain_id,
        )
    ).scalar()
    return result


# ---------------------------------------------------------------------------
# LpPosition
# ---------------------------------------------------------------------------

def create_lp_position(session: Session, data: dict) -> LpPosition:
    """
    创建新仓位记录。
    data 必须包含：position_id, pool_address, owner_address,
                   tick_lower, tick_upper, liquidity, opened_at。
    """
    pos = LpPosition(**data)
    session.add(pos)
    session.flush()
    return pos


def get_active_lp_position(
    session: Session, pool_address: str
) -> Optional[LpPosition]:
    """
    获取该 pool 当前 OPEN 状态的仓位（按创建时间取最新一条）。
    正常情况下同一 pool 同时只有一个 OPEN 仓位。
    """
    from sqlalchemy import select
    return session.execute(
        select(LpPosition)
        .where(
            LpPosition.pool_address == pool_address,
            LpPosition.status == "OPEN",
        )
        .order_by(LpPosition.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def close_lp_position(
    session: Session, position_id: str, closed_at: datetime
) -> None:
    """将仓位状态更新为 CLOSED，记录关仓时间。"""
    from sqlalchemy import update
    session.execute(
        update(LpPosition)
        .where(LpPosition.position_id == position_id)
        .values(status="CLOSED", closed_at=closed_at, updated_at=datetime.utcnow())
    )


# ---------------------------------------------------------------------------
# LpPositionAction
# ---------------------------------------------------------------------------

def create_lp_position_action(session: Session, data: dict) -> LpPositionAction:
    """
    记录仓位动作。
    data 必须包含：position_id, action_type, action_time。
    可选：tx_hash, block_number, action_metadata。
    """
    action = LpPositionAction(**data)
    session.add(action)
    session.flush()
    return action


# ---------------------------------------------------------------------------
# StrategySignal
# ---------------------------------------------------------------------------

def create_strategy_signal(session: Session, data: dict) -> StrategySignal:
    """
    写入一条策略信号记录。
    data 必须包含：pool_address, chain_id, signal_time, signal_type。
    """
    signal = StrategySignal(**data)
    session.add(signal)
    session.flush()
    return signal
