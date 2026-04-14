"""
MultiIndicatorStrategy（测试策略）
====================================

从 pool_strategy_indicators 表读取由原始 swaps/pools 直接计算的综合指标，
综合以下六个维度做出开仓 / 持仓 / 再平衡 / 平仓的判断：

    1. TVL          ：池子深度，太低则流动性风险高
    2. Volume 24H   ：实际交易活跃度
    3. Vol/TVL      ：资金利用率，越高手续费收益越好
    4. Fee APR      ：年化手续费收益率，核心收益指标
    5. 价格波动率   ：过高则无常损失风险大
    6. 无常损失估算 ：当前持仓的潜在损失

决策逻辑（优先级从高到低）
--------------------------
    1. 无指标数据                   → HOLD（数据尚未就绪）
    2. Fee APR < CLOSE_FEE_APR
       OR il_estimate < IL_STOP     → CLOSE（有仓位）/ HOLD（无仓位）
    3. 有仓位 + 价格接近区间边界   → REBALANCE
    4. 有仓位 + 价格居中           → HOLD
    5. Fee APR >= OPEN_FEE_APR
       AND vol_tvl >= OPEN_VOL_TVL
       AND volatility <= MAX_VOLATILITY → OPEN（无仓位）
    6. 其余                         → HOLD

默认参数
--------
    OPEN_FEE_APR     = 0.30   （30% 年化，开仓门槛）
    CLOSE_FEE_APR    = 0.05   （5%，收益太低退出）
    OPEN_VOL_TVL     = 0.5    （资金利用率 > 0.5 才开仓）
    MAX_VOLATILITY   = 0.05   （小时波动率 > 5% 则风险过高）
    IL_STOP          = -0.05  （无常损失超过 -5% 止损）
    RANGE_PCT        = 0.05   （±5% 价格区间）
    REBALANCE_RATIO  = 0.20   （距边界 < 20% 区间宽度时触发再平衡）
"""

import math
from decimal import Decimal
from typing import Optional

from src.db import repository as repo
from src.db.database import get_session
from src.strategy_engine.base import BaseStrategy, Decision, StrategyDecision
from src.strategy_engine.context import ActivePosition, MarketContext

# ── 策略参数 ──────────────────────────────────────────────────────────────────
OPEN_FEE_APR    = Decimal("0.30")   # 30%
CLOSE_FEE_APR   = Decimal("0.05")   # 5%
OPEN_VOL_TVL    = Decimal("0.5")
MAX_VOLATILITY  = Decimal("0.05")   # 小时波动率 5%
IL_STOP         = Decimal("-0.05")  # 无常损失 -5%
RANGE_PCT       = 0.05
REBALANCE_RATIO = 0.20
USDC_RAW        = 200_000_000       # 200 USDC，6 decimals


class MultiIndicatorStrategy(BaseStrategy):
    """
    综合 pool_strategy_indicators 六项指标进行决策的测试策略。

    与 VolumeRebalanceStrategy 的区别：
    - 数据来源：从独立的 pool_strategy_indicators 表读取，不依赖 data_engine 中间表
    - 指标更丰富：同时考虑 Fee APR、波动率、无常损失
    - 开仓条件更严格：需要 APR + Vol/TVL + 波动率三重过滤

    使用方式
    --------
        strategy = MultiIndicatorStrategy()
        runner = StrategyRunner(strategy=strategy, ...)
        runner.run_once()
    """

    def __init__(
        self,
        open_fee_apr:    Decimal = OPEN_FEE_APR,
        close_fee_apr:   Decimal = CLOSE_FEE_APR,
        open_vol_tvl:    Decimal = OPEN_VOL_TVL,
        max_volatility:  Decimal = MAX_VOLATILITY,
        il_stop:         Decimal = IL_STOP,
        range_pct:       float   = RANGE_PCT,
        rebalance_ratio: float   = REBALANCE_RATIO,
        usdc_raw:        int     = USDC_RAW,
    ) -> None:
        self.open_fee_apr    = open_fee_apr
        self.close_fee_apr   = close_fee_apr
        self.open_vol_tvl    = open_vol_tvl
        self.max_volatility  = max_volatility
        self.il_stop         = il_stop
        self.range_pct       = range_pct
        self.rebalance_ratio = rebalance_ratio
        self.usdc_raw        = usdc_raw

    # ------------------------------------------------------------------
    # 主评估逻辑
    # ------------------------------------------------------------------

    def evaluate(
        self,
        ctx: MarketContext,
        position: Optional[ActivePosition],
    ) -> Decision:
        # ── 从 pool_strategy_indicators 读取最新指标 ─────────────────────────
        indicators = self._load_indicators(ctx.pool_address, ctx.chain_id)

        if indicators is None:
            return Decision(
                action=StrategyDecision.HOLD,
                reason="pool_strategy_indicators 暂无数据，等待指标计算完成",
                meta={"source": "MultiIndicatorStrategy"},
            )

        fee_apr    = Decimal(str(indicators.fee_apr))    if indicators.fee_apr    is not None else None
        vol_tvl    = Decimal(str(indicators.volume_tvl_ratio)) if indicators.volume_tvl_ratio is not None else None
        volatility = Decimal(str(indicators.price_volatility_24h)) if indicators.price_volatility_24h is not None else None
        il         = Decimal(str(indicators.il_estimate)) if indicators.il_estimate is not None else None
        tvl_usd    = Decimal(str(indicators.tvl_usd))    if indicators.tvl_usd    is not None else None
        metric_hour = indicators.metric_hour

        # 构建 meta 供日志和回测使用
        meta = {
            "source":       "MultiIndicatorStrategy",
            "metric_hour":  str(metric_hour),
            "fee_apr":      float(fee_apr)    if fee_apr    is not None else None,
            "vol_tvl":      float(vol_tvl)    if vol_tvl    is not None else None,
            "volatility":   float(volatility) if volatility is not None else None,
            "il_estimate":  float(il)         if il         is not None else None,
            "tvl_usd":      float(tvl_usd)    if tvl_usd    is not None else None,
        }

        # ── 1. 退出条件（优先级最高）─────────────────────────────────────────
        exit_reason = self._check_exit(fee_apr, il)
        if exit_reason:
            if position is not None:
                return Decision(
                    action=StrategyDecision.CLOSE,
                    reason=exit_reason,
                    meta=meta,
                )
            return Decision(
                action=StrategyDecision.HOLD,
                reason=f"[无仓位] {exit_reason}，保持观望",
                meta=meta,
            )

        # ── 2. 有仓位：检查是否需要再平衡 ───────────────────────────────────
        if position is not None:
            tick_offset      = self._tick_offset(ctx.tick_spacing)
            rebalance_margin = round(tick_offset * 2 * self.rebalance_ratio)
            return self._evaluate_with_position(
                ctx, position, tick_offset, rebalance_margin, meta
            )

        # ── 3. 无仓位：检查开仓条件 ──────────────────────────────────────────
        open_ok, open_reason = self._check_open(fee_apr, vol_tvl, volatility)
        if open_ok:
            tick_offset       = self._tick_offset(ctx.tick_spacing)
            tick_lower, tick_upper = self._calc_ticks(ctx, tick_offset)
            return Decision(
                action=StrategyDecision.OPEN,
                reason=open_reason,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                amount0_desired=self.usdc_raw,
                amount1_desired=self._est_amount1(ctx),
                meta={**meta, "tick_offset": tick_offset},
            )

        return Decision(
            action=StrategyDecision.HOLD,
            reason=f"开仓条件未满足（{open_reason}）",
            meta=meta,
        )

    # ------------------------------------------------------------------
    # 有仓位时的再平衡判断
    # ------------------------------------------------------------------

    def _evaluate_with_position(
        self,
        ctx: MarketContext,
        position: ActivePosition,
        tick_offset: int,
        rebalance_margin: int,
        meta: dict,
    ) -> Decision:
        cur        = ctx.current_tick
        tl, tu     = position.tick_lower, position.tick_upper
        near_upper = cur >= tu - rebalance_margin
        near_lower = cur <= tl + rebalance_margin

        if near_upper or near_lower:
            side = "上边界" if near_upper else "下边界"
            dist = tu - cur if near_upper else cur - tl
            new_lower, new_upper = self._calc_ticks(ctx, tick_offset)
            return Decision(
                action=StrategyDecision.REBALANCE,
                reason=(
                    f"tick {cur} 接近{side}（距边界 {dist} ticks < 阈值 {rebalance_margin} ticks），"
                    f"重建区间 [{new_lower}, {new_upper}]"
                ),
                tick_lower=new_lower,
                tick_upper=new_upper,
                amount0_desired=self.usdc_raw,
                amount1_desired=self._est_amount1(ctx),
                meta={**meta, "old_tick_lower": tl, "old_tick_upper": tu,
                      "distance_to_edge": dist, "rebalance_margin": rebalance_margin},
            )

        return Decision(
            action=StrategyDecision.HOLD,
            reason=(
                f"仓位正常：tick {cur} 在区间 [{tl}, {tu}] 内，"
                f"距下边界 {cur - tl} ticks，距上边界 {tu - cur} ticks"
            ),
            meta={**meta, "distance_to_lower": cur - tl, "distance_to_upper": tu - cur,
                  "rebalance_margin": rebalance_margin},
        )

    # ------------------------------------------------------------------
    # 条件判断工具
    # ------------------------------------------------------------------

    def _check_exit(
        self,
        fee_apr: Optional[Decimal],
        il: Optional[Decimal],
    ) -> Optional[str]:
        """
        返回退出原因字符串，无需退出时返回 None。
        优先级：IL 止损 > Fee APR 过低。
        """
        if il is not None and il < self.il_stop:
            return (
                f"无常损失 IL={float(il):.2%} < 止损线 {float(self.il_stop):.2%}，"
                f"触发止损平仓"
            )
        if fee_apr is not None and fee_apr < self.close_fee_apr:
            return (
                f"Fee APR={float(fee_apr):.2%} < 平仓阈值 {float(self.close_fee_apr):.2%}，"
                f"收益不足，退出观望"
            )
        return None

    def _check_open(
        self,
        fee_apr: Optional[Decimal],
        vol_tvl: Optional[Decimal],
        volatility: Optional[Decimal],
    ) -> tuple[bool, str]:
        """
        返回 (是否可以开仓, 描述原因)。
        三个条件须同时满足：高 APR + 合理利用率 + 低波动。
        """
        if fee_apr is None or vol_tvl is None:
            return False, "指标数据不完整（fee_apr 或 vol_tvl 为空）"

        if fee_apr < self.open_fee_apr:
            return False, (
                f"Fee APR={float(fee_apr):.2%} < 开仓阈值 {float(self.open_fee_apr):.2%}"
            )
        if vol_tvl < self.open_vol_tvl:
            return False, (
                f"Vol/TVL={float(vol_tvl):.3f} < 开仓阈值 {float(self.open_vol_tvl):.3f}"
            )
        if volatility is not None and volatility > self.max_volatility:
            return False, (
                f"小时波动率={float(volatility):.4f} > 上限 {float(self.max_volatility):.4f}，"
                f"波动过大暂不开仓"
            )

        vol_str = f"{float(volatility):.4f}" if volatility is not None else "N/A"
        return True, (
            f"Fee APR={float(fee_apr):.2%}，"
            f"Vol/TVL={float(vol_tvl):.3f}，"
            f"波动率={vol_str}，"
            f"满足开仓条件"
        )

    # ------------------------------------------------------------------
    # Tick / 金额计算工具
    # ------------------------------------------------------------------

    def _tick_offset(self, tick_spacing: int) -> int:
        """±RANGE_PCT 对应的 tick 偏移，向上对齐到 tick_spacing。"""
        raw = math.log(1 + self.range_pct) / math.log(1.0001)
        return math.ceil(raw / tick_spacing) * tick_spacing

    @staticmethod
    def _calc_ticks(ctx: MarketContext, tick_offset: int) -> tuple[int, int]:
        ts        = ctx.tick_spacing
        raw_lower = ctx.current_tick - tick_offset
        raw_upper = ctx.current_tick + tick_offset
        tick_lower = (raw_lower // ts) * ts
        tick_upper = math.ceil(raw_upper / ts) * ts
        return tick_lower, tick_upper

    def _est_amount1(self, ctx: MarketContext) -> int:
        """估算 amount1_desired（按 200 USDC 等值的 WETH 上限）。"""
        if not ctx.price_token1 or ctx.price_token1 == 0:
            return int(Decimal("0.1") * Decimal(10 ** 18))
        usdc_human = Decimal(self.usdc_raw) / Decimal(10 ** ctx.decimals0)
        weth_human = usdc_human / ctx.price_token1
        return int(weth_human * Decimal(10 ** ctx.decimals1))

    # ------------------------------------------------------------------
    # DB 数据加载
    # ------------------------------------------------------------------

    def _load_indicators(self, pool_address: str, chain_id: int):
        """从 pool_strategy_indicators 读取最新一条记录。"""
        with get_session() as session:
            return repo.get_latest_strategy_indicators(session, pool_address, chain_id)
