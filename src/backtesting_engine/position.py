"""
V3Position — Uniswap V3 虚拟仓位
==================================

封装集中流动性的完整数学推导，供回测引擎按小时驱动。

核心公式（以 sqrtPrice 为基本变量，p = 1.0001^tick）：
    spa = sqrt(p_lower)，spb = sqrt(p_upper)，sp = sqrt(p_current)

    ① 从金额计算 L（from_amounts）：
        在区间内：
            L0 = amount0 × sp × spb / (spb - sp)       （token0 约束）
            L1 = amount1 / (sp - spa)                   （token1 约束）
            L  = min(L0, L1)                            （取限制侧）

    ② 从 L 计算当前金额（get_amounts）：
        sp < spa（低于区间，全为 token0）：
            amount0 = L × (spb - spa) / (spa × spb)
            amount1 = 0
        sp > spb（高于区间，全为 token1）：
            amount0 = 0
            amount1 = L × (spb - spa)
        spa ≤ sp ≤ spb（区间内）：
            amount0 = L × (spb - sp) / (sp × spb)
            amount1 = L × (sp - spa)

    ③ 手续费按流动性占比累积：
        fee_share = position_L / pool_total_L
        fee_earned += pool_fee × fee_share

    ④ IL = LP仓位价值 − HODL价值
        HODL 以开仓时锁定的初始金额（token0 + token1）为基准。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def _sp(tick: int) -> float:
    """sqrt(1.0001^tick)，即该 tick 对应的 sqrtPrice。"""
    return math.sqrt(1.0001 ** tick)


@dataclass
class V3Position:
    """
    V3 集中流动性仓位（回测专用虚拟仓位）。

    Attributes
    ----------
    tick_lower / tick_upper : 仓位的 tick 区间
    liquidity               : V3 流动性单位 L（浮点数，精度足够回测使用）
    open_amount0_human      : 开仓时实际使用的 token0 金额（人类可读，如 USDC 200.0）
    open_amount1_human      : 开仓时实际使用的 token1 金额（人类可读，如 WETH 0.0667）
    fees_token0_raw         : 累积手续费 token0（raw 单位，累加过程中保持精度）
    fees_token1_raw         : 累积手续费 token1（raw 单位）
    """

    tick_lower:         int
    tick_upper:         int
    liquidity:          float

    open_amount0_human: float = 0.0
    open_amount1_human: float = 0.0

    fees_token0_raw:    float = field(default=0.0)
    fees_token1_raw:    float = field(default=0.0)

    # ------------------------------------------------------------------
    # 工厂方法：从期望金额计算 L
    # ------------------------------------------------------------------

    @classmethod
    def from_amounts(
        cls,
        tick_lower:   int,
        tick_upper:   int,
        amount0_raw:  int,
        amount1_raw:  int,
        current_tick: int,
        decimals0:    int = 6,
        decimals1:    int = 18,
    ) -> "V3Position":
        """
        给定 tick 区间和期望投入金额（raw 单位），计算实际使用的流动性 L 及金额。

        合约逻辑：选取使 L 最小的那侧（即 binding constraint），
        另一侧只使用与之匹配所需的金额，多余部分退回。

        Parameters
        ----------
        amount0_raw / amount1_raw : 期望最大投入（raw 单位）
        current_tick              : 开仓时的当前 tick

        Returns
        -------
        V3Position  已填充 liquidity 和 open_amount_human 字段
        """
        spa = _sp(tick_lower)
        spb = _sp(tick_upper)
        sp  = _sp(current_tick)

        # 当前 tick 超出区间时，sqrtPrice 夹到边界计算 L
        sp_clamped = max(spa, min(sp, spb))

        # L from token0 side（仅当 sp_clamped < spb 时有效）
        denom0 = (spb - sp_clamped) / (sp_clamped * spb)
        L0 = (amount0_raw / denom0) if denom0 > 1e-30 else float("inf")

        # L from token1 side（仅当 sp_clamped > spa 时有效）
        denom1 = sp_clamped - spa
        L1 = (amount1_raw / denom1) if denom1 > 1e-30 else float("inf")

        L = min(L0, L1)
        if L == float("inf") or L <= 0:
            # 极端情况（价格完全出界），用最小侧估算
            L = max(L0, L1) if max(L0, L1) != float("inf") else 1.0

        # 实际使用金额（raw）
        if sp <= spa:
            actual0_raw = L * (spb - spa) / (spa * spb)
            actual1_raw = 0.0
        elif sp >= spb:
            actual0_raw = 0.0
            actual1_raw = L * (spb - spa)
        else:
            actual0_raw = L * (spb - sp) / (sp * spb)
            actual1_raw = L * (sp - spa)

        return cls(
            tick_lower         = tick_lower,
            tick_upper         = tick_upper,
            liquidity          = L,
            open_amount0_human = actual0_raw / 10 ** decimals0,
            open_amount1_human = actual1_raw / 10 ** decimals1,
        )

    # ------------------------------------------------------------------
    # 仓位状态查询
    # ------------------------------------------------------------------

    def is_in_range(self, current_tick: int) -> bool:
        """价格是否在区间内（tick_lower ≤ current_tick < tick_upper）。"""
        return self.tick_lower <= current_tick < self.tick_upper

    def get_amounts(
        self,
        current_tick: int,
        decimals0:    int = 6,
        decimals1:    int = 18,
    ) -> tuple[float, float]:
        """
        根据当前 tick 计算仓位的 (amount0_human, amount1_human)。
        手续费不包含在内，单独通过 get_fees_usdc() 获取。
        """
        sp  = _sp(current_tick)
        spa = _sp(self.tick_lower)
        spb = _sp(self.tick_upper)
        L   = self.liquidity

        if sp <= spa:
            # 价格低于区间：仓位全为 token0
            a0_raw = L * (spb - spa) / (spa * spb)
            return a0_raw / 10 ** decimals0, 0.0
        elif sp >= spb:
            # 价格高于区间：仓位全为 token1
            a1_raw = L * (spb - spa)
            return 0.0, a1_raw / 10 ** decimals1
        else:
            # 价格在区间内
            a0_raw = L * (spb - sp) / (sp * spb)
            a1_raw = L * (sp - spa)
            return a0_raw / 10 ** decimals0, a1_raw / 10 ** decimals1

    def position_value_usdc(
        self,
        current_tick:  int,
        eth_price_usdc: float,
        decimals0:     int = 6,
        decimals1:     int = 18,
    ) -> float:
        """当前仓位价值（USDC，不含手续费）。"""
        a0, a1 = self.get_amounts(current_tick, decimals0, decimals1)
        return a0 + a1 * eth_price_usdc

    def hodl_value_usdc(self, eth_price_usdc: float) -> float:
        """
        HODL 参考价值：若一直持有开仓时的初始金额，当前价值。
        = initial_token0 + initial_token1 × current_eth_price
        """
        return self.open_amount0_human + self.open_amount1_human * eth_price_usdc

    def il_usdc(
        self,
        current_tick:   int,
        eth_price_usdc: float,
        decimals0:      int = 6,
        decimals1:      int = 18,
    ) -> float:
        """
        无常损失（USDC）= LP仓位价值 − HODL参考价值。

        通常为负数；正数表示 LP 方式反而比 HODL 更好（极少见，
        发生在价格回归开仓时的对称区间时）。
        手续费不计入，以便与 fee_earned 分开分析。
        """
        pos_val  = self.position_value_usdc(current_tick, eth_price_usdc, decimals0, decimals1)
        hodl_val = self.hodl_value_usdc(eth_price_usdc)
        return pos_val - hodl_val

    # ------------------------------------------------------------------
    # 手续费
    # ------------------------------------------------------------------

    def accrue_fees(
        self,
        fee_token0_raw:  int,
        fee_token1_raw:  int,
        pool_liquidity:  int,
    ) -> None:
        """
        按仓位流动性占比，累积本时间步的手续费（raw 单位）。

        fee_token0_raw / fee_token1_raw : 全池本小时手续费（volume × fee_rate，已存 DB）
        pool_liquidity                  : 全池本小时末活跃流动性
        """
        if pool_liquidity <= 0 or self.liquidity <= 0:
            return
        share = self.liquidity / pool_liquidity
        self.fees_token0_raw += abs(fee_token0_raw) * share
        self.fees_token1_raw += abs(fee_token1_raw) * share

    def get_fees_usdc(
        self,
        eth_price_usdc: float,
        decimals0:      int = 6,
        decimals1:      int = 18,
    ) -> float:
        """手续费的 USDC 等值。"""
        fee0 = self.fees_token0_raw / 10 ** decimals0
        fee1 = self.fees_token1_raw / 10 ** decimals1
        return fee0 + fee1 * eth_price_usdc
