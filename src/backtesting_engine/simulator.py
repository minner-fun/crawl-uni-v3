"""
BacktestSimulator
=================

按小时驱动，将策略的 evaluate() 与历史 HourlyBar 数据对接。

每个小时的执行顺序：
    1. 派发手续费（若仓位 in-range）
    2. 构造 MarketContext（与真实 runner 结构完全相同，策略无感知）
    3. 调用 strategy.evaluate(ctx, active_pos) → Decision
    4. 执行 Decision（仅模拟，不发链上交易）
    5. 记录本小时 HourlySnapshot

资本模型：
    - 每次开仓投入固定 initial_usdc（USDC） + 等值 ETH
    - Rebalance 时：先收回当前仓位（token0 + token1 + fees），
      扣除 gas 后，重新按 initial_usdc 投入新区间
    - Close 时：收回所有资金，等待下一个 OPEN 信号
    - HODL 基准在第一次开仓时锁定（持有开仓时使用的初始金额不动）

IL 追踪：
    - unrealized_il : 当前持仓的即时 IL（随价格变化）
    - realized_il   : 历史平仓累积 IL（永久锁定）
    - snapshot 中 il_usdc = unrealized_il + realized_il
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from src.backtesting_engine.data_loader import (
    HourlyBar,
    PoolMeta,
    load_daily_vtv,
    load_hourly_bars,
    load_pool_meta,
    price_close_to_tick,
)
from src.backtesting_engine.position import V3Position
from src.db.database import get_session
from src.strategy_engine.base import BaseStrategy, StrategyDecision
from src.strategy_engine.context import ActivePosition, MarketContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    """回测参数配置。"""
    pool_address:          str
    from_dt:               datetime
    to_dt:                 datetime
    chain_id:              int   = 1
    metrics_lookback_days: int   = 3       # volume/tvl 均值窗口（与真实策略保持一致）
    initial_usdc:          float = 200.0   # 每次开仓投入的固定 USDC 金额
    gas_cost_eth_per_op:   float = 0.015   # 每次完整操作消耗的 ETH（decrease+collect+burn+mint ≈ 0.015）


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

@dataclass
class HourlySnapshot:
    """每小时状态快照，全部量纲统一为 USDC。"""
    time:                   datetime
    eth_price_usdc:         float
    current_tick:           int
    in_range:               bool
    has_position:           bool

    # 仓位价值（不含手续费）
    position_value_usdc:    float

    # 累积（截至本小时末）
    fees_earned_usdc:       float   # 已收 + 未收手续费之和
    gas_cost_usdc:          float   # 累积 gas
    il_usdc:                float   # realized + unrealized IL（通常 ≤ 0）
    hodl_value_usdc:        float   # HODL 参考净值

    # 综合净值 = 仓位价值 + 手续费 − gas
    portfolio_value_usdc:   float
    rebalance_count:        int


# ---------------------------------------------------------------------------
# 主模拟器
# ---------------------------------------------------------------------------

class BacktestSimulator:
    """
    Uniswap V3 LP 策略历史回测模拟器。

    与真实 StrategyRunner 的区别：
        - 不发链上交易，所有操作仅更新内存状态
        - MarketContext 由 HourlyBar 构造（而非链上查询）
        - strategy.evaluate() 接口完全相同，策略代码不需修改
    """

    def __init__(self, strategy: BaseStrategy, config: BacktestConfig) -> None:
        self.strategy = strategy
        self.cfg      = config

    def run(self) -> "BacktestResult":
        from src.backtesting_engine.metrics import BacktestResult

        logger.info(
            "[backtest] start  pool=%s  %s → %s",
            self.cfg.pool_address,
            self.cfg.from_dt.date(),
            self.cfg.to_dt.date(),
        )

        # ── 加载数据 ──────────────────────────────────────────────────────────
        with get_session() as session:
            bars = load_hourly_bars(
                session,
                self.cfg.pool_address,
                self.cfg.from_dt,
                self.cfg.to_dt,
                self.cfg.chain_id,
            )
            vtv_map = load_daily_vtv(
                session,
                self.cfg.pool_address,
                (self.cfg.from_dt - timedelta(days=self.cfg.metrics_lookback_days + 1)).date(),
                self.cfg.to_dt.date(),
                self.cfg.chain_id,
            )
            meta: PoolMeta = load_pool_meta(session, self.cfg.pool_address)

        if not bars:
            raise ValueError(
                "无可用的小时数据，请先运行 data_engine 构建 pool_metrics_hourly。"
            )
        logger.info("[backtest] loaded %d hourly bars", len(bars))

        # ── 仿真状态初始化 ─────────────────────────────────────────────────────
        position:       Optional[V3Position]     = None
        active_pos:     Optional[ActivePosition] = None
        cumul_fees:     float = 0.0   # 已关仓收到的手续费（USDC），不含当前仓位未收部分
        cumul_gas:      float = 0.0   # 累积 gas 成本（USDC）
        realized_il:    float = 0.0   # 已关仓的 IL（USDC），通常 ≤ 0
        rebalance_cnt:  int   = 0
        hodl_usdc:      Optional[float] = None   # 第一次开仓时锁定
        hodl_weth:      Optional[float] = None

        snapshots: list[HourlySnapshot] = []

        for bar in bars:
            tick  = price_close_to_tick(bar.price_close, meta.decimals0, meta.decimals1)
            price = bar.eth_price_usdc   # 1 ETH = X USDC

            # ── 1. 手续费累积（本 bar 的 volume，若仓位 in-range）──────────────
            if position is not None and position.is_in_range(tick):
                position.accrue_fees(
                    bar.fee_token0_raw,
                    bar.fee_token1_raw,
                    bar.pool_close_liquidity,
                )

            # ── 2. 构造 MarketContext ──────────────────────────────────────────
            avg_vtv = self._rolling_vtv(vtv_map, bar.metric_hour.date())
            ctx     = self._build_ctx(bar, tick, avg_vtv, meta)

            # ── 3. 策略评估 ────────────────────────────────────────────────────
            decision = self.strategy.evaluate(ctx, active_pos)

            # ── 4. 执行 Decision ────────────────────────────────────────────────
            if decision.action in (StrategyDecision.REBALANCE, StrategyDecision.CLOSE):
                if position is not None:
                    # 锁定当前仓位的手续费和 IL
                    cumul_fees  += position.get_fees_usdc(price, meta.decimals0, meta.decimals1)
                    realized_il += position.il_usdc(tick, price, meta.decimals0, meta.decimals1)
                    # 扣 gas（decrease + collect + burn）
                    cumul_gas   += self.cfg.gas_cost_eth_per_op * price
                    position     = None
                    active_pos   = None

            if decision.action in (StrategyDecision.OPEN, StrategyDecision.REBALANCE):
                tl, tu = decision.tick_lower, decision.tick_upper
                if tl is not None and tu is not None:
                    if decision.action == StrategyDecision.OPEN:
                        # 开仓也扣 mint gas
                        cumul_gas += (self.cfg.gas_cost_eth_per_op / 3) * price

                    usdc_raw = int(self.cfg.initial_usdc * 10 ** meta.decimals0)
                    weth_raw = int(
                        (self.cfg.initial_usdc / price) * 10 ** meta.decimals1
                        if price > 0 else 0
                    )

                    position = V3Position.from_amounts(
                        tick_lower   = tl,
                        tick_upper   = tu,
                        amount0_raw  = usdc_raw,
                        amount1_raw  = weth_raw,
                        current_tick = tick,
                        decimals0    = meta.decimals0,
                        decimals1    = meta.decimals1,
                    )
                    active_pos = ActivePosition(
                        position_id = "backtest",
                        token_id    = 0,
                        tick_lower  = tl,
                        tick_upper  = tu,
                        liquidity   = int(position.liquidity),
                        status      = "OPEN",
                    )

                    if decision.action == StrategyDecision.REBALANCE:
                        rebalance_cnt += 1

                    # 第一次开仓时锁定 HODL 基准
                    if hodl_usdc is None:
                        hodl_usdc = position.open_amount0_human
                        hodl_weth = position.open_amount1_human
                        logger.info(
                            "[backtest] HODL locked: %.2f USDC + %.6f WETH @ ETH=$%.0f",
                            hodl_usdc, hodl_weth, price,
                        )

            # ── 5. 记录快照 ────────────────────────────────────────────────────
            in_range = position.is_in_range(tick) if position else False

            if position is not None:
                pos_val      = position.position_value_usdc(tick, price, meta.decimals0, meta.decimals1)
                unrealized_f = position.get_fees_usdc(price, meta.decimals0, meta.decimals1)
                unrealized_il = position.il_usdc(tick, price, meta.decimals0, meta.decimals1)
            else:
                pos_val = unrealized_f = unrealized_il = 0.0

            total_fees = cumul_fees + unrealized_f
            total_il   = realized_il + unrealized_il
            hodl_val   = (hodl_usdc or 0.0) + (hodl_weth or 0.0) * price
            port_val   = pos_val + total_fees - cumul_gas

            snapshots.append(HourlySnapshot(
                time                 = bar.metric_hour,
                eth_price_usdc       = price,
                current_tick         = tick,
                in_range             = in_range,
                has_position         = position is not None,
                position_value_usdc  = pos_val,
                fees_earned_usdc     = total_fees,
                gas_cost_usdc        = cumul_gas,
                il_usdc              = total_il,
                hodl_value_usdc      = hodl_val,
                portfolio_value_usdc = port_val,
                rebalance_count      = rebalance_cnt,
            ))

        logger.info(
            "[backtest] done  rebalances=%d  snapshots=%d",
            rebalance_cnt, len(snapshots),
        )

        return BacktestResult(
            snapshots        = snapshots,
            config           = self.cfg,
            total_rebalances = rebalance_cnt,
            pool_meta        = meta,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _rolling_vtv(self, vtv_map: dict[date, float], current_date: date) -> Optional[Decimal]:
        """取 current_date 前 N 天（不含当日）的 VTV 均值。"""
        n = self.cfg.metrics_lookback_days
        vals = [
            vtv_map[current_date - timedelta(days=i)]
            for i in range(1, n + 1)
            if (current_date - timedelta(days=i)) in vtv_map
        ]
        return Decimal(str(sum(vals) / len(vals))) if vals else None

    def _build_ctx(
        self,
        bar:     HourlyBar,
        tick:    int,
        avg_vtv: Optional[Decimal],
        meta:    PoolMeta,
    ) -> MarketContext:
        """将 HourlyBar + 元数据组装为 MarketContext，与真实运行时结构完全一致。"""
        price_raw = bar.price_close * (10 ** (meta.decimals1 - meta.decimals0))
        sqrt_px96 = int(math.sqrt(price_raw) * (2 ** 96))

        return MarketContext(
            pool_address          = self.cfg.pool_address,
            chain_id              = self.cfg.chain_id,
            current_tick          = tick,
            sqrt_price_x96        = sqrt_px96,
            current_liquidity     = bar.pool_close_liquidity,
            price_token0          = Decimal(str(bar.price_close)),
            price_token1          = Decimal(str(bar.eth_price_usdc)) if bar.eth_price_usdc else None,
            tick_spacing          = meta.tick_spacing,
            fee                   = meta.fee_tier,
            token0                = meta.token0,
            token1                = meta.token1,
            decimals0             = meta.decimals0,
            decimals1             = meta.decimals1,
            avg_volume_tvl_ratio  = avg_vtv,
            latest_fee_apr        = None,
            latest_tvl_usd        = None,
            n_days                = self.cfg.metrics_lookback_days,
        )


# ---------------------------------------------------------------------------
# 延迟导入（避免循环引用）
# ---------------------------------------------------------------------------

if TYPE_CHECKING := False:
    from src.backtesting_engine.metrics import BacktestResult  # noqa: F401
