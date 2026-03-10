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

from src.db.models import Burn, Collect, Mint, Pool, Swap, SyncCursor, Token


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
