"""
VolumeRebalanceStrategy
=======================

简单 volume/tvl 触发 + ±5% 区间 + 边界 rebalance 策略。

决策逻辑（优先级从高到低）：
    1. avg volume/tvl < 0.5  → CLOSE（有仓位）/ HOLD（无仓位）
    2. 有仓位且价格接近边界  → REBALANCE
    3. 有仓位且价格居中      → HOLD
    4. 无仓位且 avg vtv ≥ 2  → OPEN
    5. 其余                  → HOLD

参数：
    开仓阈值   volume/tvl > 2.0（近 3 天均值，由 runner 控制天数）
    平仓阈值   volume/tvl < 0.5
    区间半径   ±5% 当前价格（≈ ±488 ticks，tick_spacing 对齐）
    Rebalance  当前 tick 距区间边界 < 区间总宽度 20%（≈ 195 ticks）
    投入金额   固定 200 USDC（token0） + 等值 WETH（token1，由合约决定实际用量）
"""

import math
from decimal import Decimal
from typing import Optional

from src.strategy_engine.base import BaseStrategy, Decision, StrategyDecision
from src.strategy_engine.context import ActivePosition, MarketContext

# ── 策略参数（可外部覆盖，但保持默认值便于快速测试）────────────────────────────
VOLUME_TVL_OPEN_THRESHOLD  = Decimal("2.0")
VOLUME_TVL_CLOSE_THRESHOLD = Decimal("0.5")
RANGE_PCT                  = 0.05    # 区间半径：±5%
REBALANCE_BOUNDARY_RATIO   = 0.20   # 距边界不足 20% 区间宽度时触发 rebalance
USDC_RAW                   = 200_000_000  # 200 USDC，6 decimals


class VolumeRebalanceStrategy(BaseStrategy):
    """
    volume/tvl 触发的 ±5% 集中流动性策略。

    可通过构造函数覆盖默认参数：
        strategy = VolumeRebalanceStrategy(open_threshold=Decimal("1.5"))
    """

    def __init__(
        self,
        open_threshold:       Decimal = VOLUME_TVL_OPEN_THRESHOLD,
        close_threshold:      Decimal = VOLUME_TVL_CLOSE_THRESHOLD,
        range_pct:            float   = RANGE_PCT,
        rebalance_boundary:   float   = REBALANCE_BOUNDARY_RATIO,
        usdc_raw:             int     = USDC_RAW,
    ) -> None:
        self.open_threshold     = open_threshold
        self.close_threshold    = close_threshold
        self.range_pct          = range_pct
        self.rebalance_boundary = rebalance_boundary
        self.usdc_raw           = usdc_raw

    # ------------------------------------------------------------------
    # 主评估逻辑
    # ------------------------------------------------------------------

    def evaluate(
        self,
        ctx: MarketContext,
        position: Optional[ActivePosition],
    ) -> Decision:
        tick_offset      = self._tick_offset(ctx.tick_spacing)
        rebalance_margin = round(tick_offset * 2 * self.rebalance_boundary)
        avg_vtv          = ctx.avg_volume_tvl_ratio

        # ── 1. 退出条件（优先级最高）──────────────────────────────────────────
        if avg_vtv is not None and avg_vtv < self.close_threshold:
            if position is not None:
                return Decision(
                    action=StrategyDecision.CLOSE,
                    reason=(
                        f"近 {ctx.n_days} 天均 volume/tvl={avg_vtv:.3f} "
                        f"< {self.close_threshold}，市场冷淡，平仓观望"
                    ),
                    meta={"avg_volume_tvl": float(avg_vtv)},
                )
            return Decision(
                action=StrategyDecision.HOLD,
                reason=(
                    f"近 {ctx.n_days} 天均 volume/tvl={avg_vtv:.3f} "
                    f"< {self.close_threshold}，市场冷淡，保持观望"
                ),
                meta={"avg_volume_tvl": float(avg_vtv)},
            )

        # ── 2. 有仓位：检查是否需要 rebalance ─────────────────────────────────
        if position is not None:
            return self._evaluate_with_position(ctx, position, tick_offset, rebalance_margin, avg_vtv)

        # ── 3. 无仓位：检查开仓条件 ───────────────────────────────────────────
        if avg_vtv is not None and avg_vtv >= self.open_threshold:
            tick_lower, tick_upper = self._calc_ticks(ctx, tick_offset)
            return Decision(
                action=StrategyDecision.OPEN,
                reason=(
                    f"近 {ctx.n_days} 天均 volume/tvl={avg_vtv:.3f} "
                    f">= {self.open_threshold}，开仓 tick=[{tick_lower}, {tick_upper}]"
                ),
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                amount0_desired=self.usdc_raw,
                amount1_desired=self._est_amount1(ctx),
                meta={
                    "avg_volume_tvl": float(avg_vtv),
                    "tick_offset":    tick_offset,
                },
            )

        vtv_str = f"{avg_vtv:.3f}" if avg_vtv is not None else "N/A（数据不足）"
        return Decision(
            action=StrategyDecision.HOLD,
            reason=f"近 {ctx.n_days} 天均 volume/tvl={vtv_str} < {self.open_threshold}，未达开仓条件",
            meta={"avg_volume_tvl": float(avg_vtv) if avg_vtv is not None else None},
        )

    # ------------------------------------------------------------------
    # 有仓位时的评估
    # ------------------------------------------------------------------

    def _evaluate_with_position(
        self,
        ctx: MarketContext,
        position: ActivePosition,
        tick_offset: int,
        rebalance_margin: int,
        avg_vtv: Optional[Decimal],
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
                meta={
                    "avg_volume_tvl":   float(avg_vtv) if avg_vtv is not None else None,
                    "old_tick_lower":   tl,
                    "old_tick_upper":   tu,
                    "distance_to_edge": dist,
                    "rebalance_margin": rebalance_margin,
                },
            )

        return Decision(
            action=StrategyDecision.HOLD,
            reason=(
                f"仓位正常：tick {cur} 在区间 [{tl}, {tu}] 内，"
                f"距下边界 {cur - tl} ticks，距上边界 {tu - cur} ticks"
            ),
            meta={
                "avg_volume_tvl":    float(avg_vtv) if avg_vtv is not None else None,
                "distance_to_lower": cur - tl,
                "distance_to_upper": tu - cur,
                "rebalance_margin":  rebalance_margin,
            },
        )

    # ------------------------------------------------------------------
    # Tick 计算工具
    # ------------------------------------------------------------------

    def _tick_offset(self, tick_spacing: int) -> int:
        """
        ±RANGE_PCT 对应的 tick 偏移量，向上对齐到 tick_spacing。

        log(1.05) / log(1.0001) ≈ 487.9 → 488 ticks
        对齐 tick_spacing=10 → 490 ticks
        """
        raw = math.log(1 + self.range_pct) / math.log(1.0001)
        return math.ceil(raw / tick_spacing) * tick_spacing

    @staticmethod
    def _calc_ticks(ctx: MarketContext, tick_offset: int) -> tuple[int, int]:
        """
        以当前 tick 为中心，上下各偏移 tick_offset，向外对齐 tick_spacing。

        tick_lower 向下取整，tick_upper 向上取整（保证区间不因取整而缩小）。
        """
        ts         = ctx.tick_spacing
        raw_lower  = ctx.current_tick - tick_offset
        raw_upper  = ctx.current_tick + tick_offset
        tick_lower = (raw_lower // ts) * ts
        tick_upper = math.ceil(raw_upper / ts) * ts
        return tick_lower, tick_upper

    def _est_amount1(self, ctx: MarketContext) -> int:
        """
        估算 amount1_desired（WETH raw 单位）。

        按 200 USDC 等值 WETH 设置上限；实际用量由合约根据当前 tick 决定。
        price_token1 = 1 WETH = X USDC（人类可读）。
        """
        if not ctx.price_token1 or ctx.price_token1 == 0:
            # 无价格信息时给 0.1 ETH 作为兜底上限
            return int(Decimal("0.1") * Decimal(10 ** 18))

        usdc_human = Decimal(self.usdc_raw) / Decimal(10 ** ctx.decimals0)
        weth_human = usdc_human / ctx.price_token1
        return int(weth_human * Decimal(10 ** ctx.decimals1))
