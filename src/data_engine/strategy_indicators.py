"""
策略指标计算器
==============

独立于 data_engine 的中间聚合表（pool_metrics_hourly / pool_metrics_daily），
直接从原始事件表（swaps）和基础信息表（pools）计算策略所需的指标，
写入 pool_strategy_indicators 表。

每小时调用一次即可保持数据最新。

TVL 计算方式
------------
优先通过链上 IERC20.balanceOf(pool_address) 读取两个代币的真实余额，
这是最准确的方式（DeFiLlama / Uniswap Analytics 的标准做法），
包含所有在范围/出范围的 LP 头寸以及未收取的手续费。
当 RPC 不可用时，自动回退到基于 sqrtPriceX96 + liquidity 的虚拟流动性估算。

指标列表（参见 doc/指标计算.md）
--------------------------------
1. TVL（USD）        ：balanceOf 读取真实余额，换算为 USD
2. Volume 24H（USD） ：过去 24H 稳定币侧绝对交易量之和（swaps 表）
3. Vol/TVL           ：资金利用率
4. Fee APR           ：volume_24h × fee_rate / TVL × 365
5. Price Volatility  ：过去 24H 小时级收盘价的对数收益率标准差
6. 无常损失（IL）    ：基于 24H 价格变化的全范围 IL 估算

用法
----
    from src.data_engine.strategy_indicators import build_strategy_indicators
    from src.db.database import get_session

    with get_session() as session:
        build_strategy_indicators(
            session,
            pool_address = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
            fee          = 500,
            symbol0      = "USDC",
            symbol1      = "WETH",
            decimals0    = 6,
            decimals1    = 18,
            metric_hour  = datetime(2024, 1, 1, 12, 0),
        )
"""

import json
import logging
import math
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session
from web3 import Web3

from src.db import repository as repo
from src.data_engine.utils import (
    calc_il_fullrange,
    calc_log_return_volatility,
    get_stablecoin_side,
    raw_to_human,
    sqrt_price_x96_to_prices,
)

logger = logging.getLogger(__name__)

getcontext().prec = 40

_Q96 = 2 ** 96
_TVL_RANGE_FALLBACK = 0.10   # 虚拟流动性兜底：假设 ±10% 活跃范围

# 仅需 balanceOf 的最小 ERC-20 ABI
_IERC20_BALANCE_ABI = json.loads("""[
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")

# 模块级 Web3 单例（延迟初始化，避免每次调用都建连接）
_w3: Optional[Web3] = None


def _get_w3() -> Web3:
    """获取或初始化 Web3 HTTP 连接（模块级单例）。"""
    global _w3
    if _w3 is None or not _w3.is_connected():
        from src.Constracts import MAINNET_RPC_URL
        _w3 = Web3(Web3.HTTPProvider(MAINNET_RPC_URL))
    return _w3


# ---------------------------------------------------------------------------
# 核心计算入口
# ---------------------------------------------------------------------------

def build_strategy_indicators(
    session: Session,
    pool_address: str,
    fee: int,
    symbol0: Optional[str],
    symbol1: Optional[str],
    decimals0: int,
    decimals1: int,
    metric_hour: datetime,
    chain_id: int = 1,
) -> bool:
    """
    计算并写入一个小时的策略指标。

    TVL 优先通过 balanceOf 链上读取；RPC 失败时自动回退到虚拟流动性估算。

    Parameters
    ----------
    fee         : 池费率（pips），如 500 = 0.05%
    metric_hour : 目标小时（UTC，应已对齐到整点）

    Returns
    -------
    True  表示成功写入，False 表示该小时内无 swap 数据跳过。
    """
    fee_rate    = Decimal(fee) / Decimal(1_000_000)
    stable_side = get_stablecoin_side(symbol0, symbol1)
    stable_decs = (
        decimals0 if stable_side == 0
        else decimals1 if stable_side == 1
        else None
    )
    hour_start       = metric_hour
    hour_end         = metric_hour + timedelta(hours=1)
    window_24h_start = metric_hour - timedelta(hours=24)

    # ── ① 当前小时最后一笔 swap（价格基准）───────────────────────────────────
    latest_swap = _get_latest_swap_in_hour(session, pool_address, hour_start, hour_end)
    if latest_swap is None:
        return False

    sqrt_px96 = int(latest_swap.sqrt_price_x96)
    liquidity = int(latest_swap.liquidity)

    price_token0, price_token1 = sqrt_price_x96_to_prices(sqrt_px96, decimals0, decimals1)
    price_current = price_token1   # e.g., 对于 USDC/WETH 池 = ETH 的 USDC 价格

    # ── ② 24H 前的参考价格（用于 IL 计算）──────────────────────────────────
    price_24h_ago = _get_close_price_at(
        session, pool_address, window_24h_start, hour_start, decimals0, decimals1
    )

    # ── ③ TVL：优先 balanceOf 链上读取，失败则用虚拟流动性兜底 ──────────────
    pool = repo.get_pool(session, pool_address)
    tvl_usd = None
    if pool is not None:
        tvl_usd = _fetch_tvl_onchain(
            pool_address      = pool_address,
            token0_address    = pool.token0_address,
            token1_address    = pool.token1_address,
            decimals0         = decimals0,
            decimals1         = decimals1,
            price_token1      = price_token1,
            stable_side       = stable_side,
        )

    if tvl_usd is None:
        # RPC 不可用或非稳定币对，回退到 sqrtPriceX96 + liquidity 估算
        tvl_usd = _estimate_tvl_fallback(
            sqrt_price_x96 = sqrt_px96,
            liquidity      = liquidity,
            price_token1   = price_token1,
            stable_side    = stable_side,
            decimals0      = decimals0,
            decimals1      = decimals1,
        )

    # ── ④ Volume 24H（稳定币侧绝对量之和）──────────────────────────────────
    volume_24h_usd = _calc_volume_24h_usd(
        session, pool_address, window_24h_start, hour_end, stable_side, stable_decs
    )

    # ── ⑤ Vol/TVL 资金利用率 ────────────────────────────────────────────────
    volume_tvl_ratio: Optional[Decimal] = None
    if tvl_usd and tvl_usd > 0 and volume_24h_usd is not None:
        volume_tvl_ratio = volume_24h_usd / tvl_usd

    # ── ⑥ Fee APR ───────────────────────────────────────────────────────────
    fee_apr: Optional[Decimal] = None
    if tvl_usd and tvl_usd > 0 and volume_24h_usd is not None:
        fee_apr = volume_24h_usd * fee_rate / tvl_usd * Decimal(365)

    # ── ⑦ 价格波动率（过去 24H 小时级对数收益率标准差）────────────────────
    hourly_prices = _get_hourly_close_prices(
        session, pool_address, window_24h_start, hour_end, decimals0, decimals1
    )
    volatility_raw = calc_log_return_volatility([float(p) for p in hourly_prices])
    price_volatility_24h = (
        Decimal(str(volatility_raw)) if volatility_raw is not None else None
    )

    # ── ⑧ 无常损失估算 ─────────────────────────────────────────────────────
    il_estimate: Optional[Decimal] = None
    if price_24h_ago and price_24h_ago > 0 and price_current and price_current > 0:
        il_raw = calc_il_fullrange(float(price_current / price_24h_ago))
        if il_raw is not None:
            il_estimate = Decimal(str(il_raw))

    # ── ⑨ 写入 DB ───────────────────────────────────────────────────────────
    repo.upsert_strategy_indicators(session, {
        "pool_address":          pool_address,
        "chain_id":              chain_id,
        "metric_hour":           metric_hour,
        "computed_at":           datetime.utcnow(),
        "price_current":         price_current,
        "price_24h_ago":         price_24h_ago,
        "tvl_usd":               tvl_usd,
        "volume_24h_usd":        volume_24h_usd,
        "volume_tvl_ratio":      volume_tvl_ratio,
        "fee_rate":              fee_rate,
        "fee_apr":               fee_apr,
        "price_volatility_24h":  price_volatility_24h,
        "il_estimate":           il_estimate,
    })
    return True


# ---------------------------------------------------------------------------
# 批量运行入口（增量，从上次计算的位置继续）
# ---------------------------------------------------------------------------

def run_incremental(
    session: Session,
    pool_address: str,
    fee: int,
    symbol0: Optional[str],
    symbol1: Optional[str],
    decimals0: int,
    decimals1: int,
    chain_id: int = 1,
) -> int:
    """
    从上次已计算的 metric_hour 开始，追算到当前 UTC 整点（不含当前未完整的小时）。

    注意：TVL 由 balanceOf 获取的是**当前链上状态**，对历史小时记录的 TVL
    反映的是运行时刻的池余额，而非当时的历史余额。对于 Volume、波动率、IL
    等其他指标，使用 swaps 表中的历史数据，结果是准确的。

    Returns
    -------
    本次写入的小时记录数。
    """
    last_hour = repo.get_last_strategy_indicators_hour(session, pool_address, chain_id)

    if last_hour is None:
        row = session.execute(text(
            "SELECT DATE_TRUNC('hour', MIN(block_timestamp)) AS first_hour "
            "FROM swaps WHERE pool_address = :addr"
        ), {"addr": pool_address}).fetchone()
        if row is None or row.first_hour is None:
            return 0
        start_hour = row.first_hour
    else:
        start_hour = last_hour + timedelta(hours=1)

    now_truncated = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

    count = 0
    cur_hour = start_hour
    while cur_hour < now_truncated:
        ok = build_strategy_indicators(
            session, pool_address, fee, symbol0, symbol1,
            decimals0, decimals1, cur_hour, chain_id,
        )
        if ok:
            count += 1
        cur_hour += timedelta(hours=1)

    return count


# ---------------------------------------------------------------------------
# TVL：链上 balanceOf 读取（精确）
# ---------------------------------------------------------------------------

def _fetch_tvl_onchain(
    pool_address: str,
    token0_address: str,
    token1_address: str,
    decimals0: int,
    decimals1: int,
    price_token1: Optional[Decimal],
    stable_side: Optional[int],
) -> Optional[Decimal]:
    """
    通过 IERC20.balanceOf(pool_address) 读取池子真实持有的两种代币数量，
    换算为 USD 后相加得到 TVL。

    这是最准确的 TVL 计算方式：
    - 包含所有在范围和出范围的 LP 头寸
    - 包含已赚取但未收取的手续费
    - 与 DeFiLlama / Uniswap Analytics 使用相同的方法

    Parameters
    ----------
    price_token1 : 1 个 token1 值多少 token0（人类可读）
                   对于 USDC/WETH 池，即 ETH 的 USDC 价格
    stable_side  : 0 = token0 是稳定币，1 = token1 是稳定币，None = 无稳定币

    Returns
    -------
    TVL in USD，RPC 调用失败或非稳定币对时返回 None。
    """
    if stable_side is None or price_token1 is None or price_token1 == 0:
        return None

    try:
        w3 = _get_w3()
        pool_cs    = Web3.to_checksum_address(pool_address)
        token0_cs  = Web3.to_checksum_address(token0_address)
        token1_cs  = Web3.to_checksum_address(token1_address)

        token0_contract = w3.eth.contract(address=token0_cs, abi=_IERC20_BALANCE_ABI)
        token1_contract = w3.eth.contract(address=token1_cs, abi=_IERC20_BALANCE_ABI)

        balance0_raw: int = token0_contract.functions.balanceOf(pool_cs).call()
        balance1_raw: int = token1_contract.functions.balanceOf(pool_cs).call()

        balance0_human = Decimal(balance0_raw) / Decimal(10 ** decimals0)
        balance1_human = Decimal(balance1_raw) / Decimal(10 ** decimals1)

        if stable_side == 0:
            # token0 = USDC（≈ $1），token1 = WETH（按 price_token1 换算为 USD）
            tvl = balance0_human + balance1_human * price_token1
        else:
            # token1 = 稳定币（≈ $1），token0 按 1/price_token1 换算
            tvl = balance0_human / price_token1 + balance1_human

        logger.debug(
            "[tvl_onchain] pool=%s  balance0=%s  balance1=%s  tvl_usd=%.2f",
            pool_address, balance0_raw, balance1_raw, float(tvl),
        )
        return tvl if tvl > 0 else None

    except Exception as exc:
        logger.warning(
            "[tvl_onchain] balanceOf 调用失败，将使用虚拟流动性兜底估算。"
            "pool=%s  error=%s",
            pool_address, exc,
        )
        return None


# ---------------------------------------------------------------------------
# TVL：虚拟流动性估算（兜底回退，仅在 RPC 不可用时使用）
# ---------------------------------------------------------------------------

def _estimate_tvl_fallback(
    sqrt_price_x96: int,
    liquidity: int,
    price_token1: Optional[Decimal],
    stable_side: Optional[int],
    decimals0: int,
    decimals1: int,
) -> Optional[Decimal]:
    """
    当 balanceOf 调用失败时的兜底 TVL 估算。

    仅计算当前 tick 处活跃流动性在 ±_TVL_RANGE_FALLBACK（默认 ±10%）
    虚拟价格范围内的等效代币数量，会明显低估真实 TVL（出范围头寸不计入）。

    公式（Uniswap V3 虚拟流动性）：
        sqrtP  = sqrt_price_x96 / 2^96
        amount0_raw = L × (1/sqrtP − 1/sqrtP_upper)
        amount1_raw = L × (sqrtP − sqrtP_lower)
    """
    if stable_side is None or price_token1 is None or liquidity == 0 or sqrt_price_x96 == 0:
        return None

    sqrtP       = sqrt_price_x96 / _Q96
    sqrtP_lower = sqrtP * math.sqrt(1 - _TVL_RANGE_FALLBACK)
    sqrtP_upper = sqrtP * math.sqrt(1 + _TVL_RANGE_FALLBACK)

    token0_raw  = liquidity * (1.0 / sqrtP - 1.0 / sqrtP_upper)
    token1_raw  = liquidity * (sqrtP - sqrtP_lower)

    token0_human = Decimal(str(max(token0_raw, 0))) / Decimal(10 ** decimals0)
    token1_human = Decimal(str(max(token1_raw, 0))) / Decimal(10 ** decimals1)

    if stable_side == 0:
        tvl = token0_human + token1_human * price_token1
    else:
        tvl = token0_human / price_token1 + token1_human

    return tvl if tvl > 0 else None


# ---------------------------------------------------------------------------
# 辅助：从 swaps 表读取价格 / 成交量数据
# ---------------------------------------------------------------------------

def _get_latest_swap_in_hour(
    session: Session,
    pool_address: str,
    hour_start: datetime,
    hour_end: datetime,
):
    """返回指定小时内 block_number + log_index 最大的 swap 行（含 sqrtPriceX96, liquidity）。"""
    return session.execute(text("""
        SELECT sqrt_price_x96, liquidity, tick
        FROM swaps
        WHERE pool_address = :addr
          AND block_timestamp >= :t0
          AND block_timestamp <  :t1
        ORDER BY block_number DESC, log_index DESC
        LIMIT 1
    """), {"addr": pool_address, "t0": hour_start, "t1": hour_end}).fetchone()


def _get_close_price_at(
    session: Session,
    pool_address: str,
    window_start: datetime,
    window_end: datetime,
    decimals0: int,
    decimals1: int,
) -> Optional[Decimal]:
    """获取指定时间窗口内最后一笔 swap 对应的 price_token1。"""
    row = session.execute(text("""
        SELECT sqrt_price_x96
        FROM swaps
        WHERE pool_address = :addr
          AND block_timestamp >= :t0
          AND block_timestamp <  :t1
        ORDER BY block_number DESC, log_index DESC
        LIMIT 1
    """), {"addr": pool_address, "t0": window_start, "t1": window_end}).fetchone()
    if row is None:
        return None
    _, p1 = sqrt_price_x96_to_prices(int(row.sqrt_price_x96), decimals0, decimals1)
    return p1


def _get_hourly_close_prices(
    session: Session,
    pool_address: str,
    window_start: datetime,
    window_end: datetime,
    decimals0: int,
    decimals1: int,
) -> list[Decimal]:
    """
    获取时间窗口内每小时最后一笔 swap 的 price_token1（用于计算波动率）。
    返回按时间升序排列的价格列表。
    """
    rows = session.execute(text("""
        SELECT DISTINCT ON (DATE_TRUNC('hour', block_timestamp))
            DATE_TRUNC('hour', block_timestamp) AS h,
            sqrt_price_x96
        FROM swaps
        WHERE pool_address = :addr
          AND block_timestamp >= :t0
          AND block_timestamp <  :t1
        ORDER BY DATE_TRUNC('hour', block_timestamp), block_number DESC, log_index DESC
    """), {"addr": pool_address, "t0": window_start, "t1": window_end}).fetchall()

    prices = []
    for row in rows:
        _, p1 = sqrt_price_x96_to_prices(int(row.sqrt_price_x96), decimals0, decimals1)
        if p1 is not None and p1 > 0:
            prices.append(p1)
    return prices


def _calc_volume_24h_usd(
    session: Session,
    pool_address: str,
    window_start: datetime,
    window_end: datetime,
    stable_side: Optional[int],
    stable_decs: Optional[int],
) -> Optional[Decimal]:
    """
    计算指定时间窗口内的 USD 成交量（稳定币侧绝对值之和）。
    非稳定币对返回 None。
    """
    if stable_side is None or stable_decs is None:
        return None

    amount_col = "amount0_raw" if stable_side == 0 else "amount1_raw"
    row = session.execute(text(f"""
        SELECT SUM(ABS({amount_col})) AS vol_raw
        FROM swaps
        WHERE pool_address = :addr
          AND block_timestamp >= :t0
          AND block_timestamp <  :t1
    """), {"addr": pool_address, "t0": window_start, "t1": window_end}).fetchone()

    if row is None or row.vol_raw is None:
        return Decimal(0)
    return raw_to_human(int(row.vol_raw), stable_decs)
