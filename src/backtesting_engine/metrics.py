"""
BacktestResult — 回测结果 & 指标计算
======================================

所有指标均为懒计算属性（@property），只在访问时计算一次。

指标体系分四层：

① 收益层
    total_return_pct    总回报率
    hodl_return_pct     HODL 基准回报率
    alpha_vs_hodl       LP 超额收益

② 年化层
    gross_fee_apr       手续费年化（不扣 gas/IL）
    net_apr             净年化（含所有成本）

③ V3 专属层
    in_range_pct        价格在区间内的时间占比
    total_rebalances    Rebalance 总次数
    total_fees_usdc     累计手续费收入
    total_il_usdc       期末 IL（负数）
    total_gas_usdc      累计 gas 成本
    fee_minus_il_usdc   费用 − IL = LP 净贡献

④ 风险层
    max_drawdown        最大回撤（%）
    sharpe_ratio        Sharpe Ratio（年化，无风险利率=0）
    sortino_ratio       Sortino Ratio（仅下行波动）
    daily_return_vol    日收益率标准差
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backtesting_engine.simulator import BacktestConfig, HourlySnapshot
    from src.backtesting_engine.data_loader import PoolMeta


@dataclass
class BacktestResult:
    """
    回测完整结果。

    由 BacktestSimulator.run() 返回，不应手动构造。
    """
    snapshots:        list["HourlySnapshot"]
    config:           "BacktestConfig"
    total_rebalances: int
    pool_meta:        "PoolMeta"

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------

    @property
    def _duration_days(self) -> float:
        if len(self.snapshots) < 2:
            return 1.0
        delta = self.snapshots[-1].time - self.snapshots[0].time
        return max(delta.total_seconds() / 86400, 1.0)

    @property
    def _initial_investment(self) -> float:
        """
        以第一个有仓位快照的仓位价值作为初始投入基准。
        若始终无仓位，回退为 initial_usdc × 2（USDC + ETH）。
        """
        for s in self.snapshots:
            if s.has_position and s.position_value_usdc > 0:
                return s.position_value_usdc
        return self.config.initial_usdc * 2

    @property
    def _first_hodl(self) -> float:
        """HODL 参考净值在第一次开仓时的初始值。"""
        for s in self.snapshots:
            if s.hodl_value_usdc > 0:
                return s.hodl_value_usdc
        return self.config.initial_usdc * 2

    # ------------------------------------------------------------------
    # ① 收益层
    # ------------------------------------------------------------------

    @property
    def total_return_pct(self) -> float:
        """LP 总回报率（%）= (期末净值 − 初始投入) / 初始投入 × 100。"""
        init = self._initial_investment
        if init == 0:
            return 0.0
        final = self.snapshots[-1].portfolio_value_usdc
        return (final - init) / init * 100

    @property
    def hodl_return_pct(self) -> float:
        """50/50 HODL 参考总回报率（%）。"""
        first = self._first_hodl
        if first == 0:
            return 0.0
        final = self.snapshots[-1].hodl_value_usdc
        return (final - first) / first * 100

    @property
    def alpha_vs_hodl(self) -> float:
        """LP 超额收益（%）= total_return_pct − hodl_return_pct。"""
        return self.total_return_pct - self.hodl_return_pct

    # ------------------------------------------------------------------
    # ② 年化层
    # ------------------------------------------------------------------

    @property
    def gross_fee_apr(self) -> float:
        """
        手续费年化收益率（%）：仅计算手续费收入，不扣 gas / IL。

        gross_fee_apr = (total_fees / initial_investment) × (365 / duration_days) × 100
        """
        init = self._initial_investment
        if init == 0:
            return 0.0
        return self.total_fees_usdc / init * (365 / self._duration_days) * 100

    @property
    def net_apr(self) -> float:
        """
        净年化收益率（%）：含 gas 和 IL 所有成本后的真实年化。

        net_apr = ((final − initial) / initial) × (365 / duration_days) × 100
        """
        init = self._initial_investment
        if init == 0:
            return 0.0
        final = self.snapshots[-1].portfolio_value_usdc
        return (final - init) / init * (365 / self._duration_days) * 100

    # ------------------------------------------------------------------
    # ③ V3 专属层
    # ------------------------------------------------------------------

    @property
    def in_range_pct(self) -> float:
        """
        价格在 tick 区间内的时间占比（%）。
        仅统计"有仓位"的小时。
        """
        with_pos = [s for s in self.snapshots if s.has_position]
        if not with_pos:
            return 0.0
        in_cnt = sum(1 for s in with_pos if s.in_range)
        return in_cnt / len(with_pos) * 100

    @property
    def total_fees_usdc(self) -> float:
        return self.snapshots[-1].fees_earned_usdc if self.snapshots else 0.0

    @property
    def total_il_usdc(self) -> float:
        """期末时刻的累积 IL（USDC，通常 ≤ 0）。"""
        return self.snapshots[-1].il_usdc if self.snapshots else 0.0

    @property
    def total_gas_usdc(self) -> float:
        return self.snapshots[-1].gas_cost_usdc if self.snapshots else 0.0

    @property
    def fee_minus_il_usdc(self) -> float:
        """手续费 − |IL| = LP 对比 HODL 的净贡献（正数才真正赚到 alpha）。"""
        return self.total_fees_usdc + self.total_il_usdc  # il 已为负数，直接相加

    @property
    def avg_hold_hours(self) -> float:
        """每个仓位的平均持仓时长（小时）。"""
        if self.total_rebalances == 0:
            return self._duration_days * 24
        n_positions = self.total_rebalances + 1
        return self._duration_days * 24 / n_positions

    # ------------------------------------------------------------------
    # ④ 风险层
    # ------------------------------------------------------------------

    @property
    def max_drawdown(self) -> float:
        """
        最大回撤（%）= 从峰值到谷值的最大跌幅。
        负数表示亏损幅度，如 -18.3 表示最大回撤 18.3%。
        """
        values = [s.portfolio_value_usdc for s in self.snapshots]
        if not values:
            return 0.0
        max_dd = 0.0
        peak   = values[0]
        for v in values:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (v - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        """
        年化 Sharpe Ratio（无风险利率 = 0）。
        = 日均收益率 / 日收益率标准差 × √365
        """
        daily = self._daily_returns()
        if len(daily) < 2:
            return 0.0
        mean = sum(daily) / len(daily)
        std  = _std(daily)
        return (mean / std * math.sqrt(365)) if std > 0 else 0.0

    @property
    def sortino_ratio(self) -> float:
        """
        Sortino Ratio（仅以下行波动率为分母，更适合 LP 策略）。
        = 日均收益率 / 下行标准差 × √365
        """
        daily    = self._daily_returns()
        if len(daily) < 2:
            return 0.0
        mean     = sum(daily) / len(daily)
        neg      = [r for r in daily if r < 0]
        if not neg:
            return float("inf")
        down_std = math.sqrt(sum(r ** 2 for r in neg) / len(neg))
        return (mean / down_std * math.sqrt(365)) if down_std > 0 else 0.0

    @property
    def daily_return_vol(self) -> float:
        """日收益率标准差（年化前），衡量策略波动性。"""
        return _std(self._daily_returns())

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _daily_returns(self) -> list[float]:
        """
        按日取最后一个快照的 portfolio_value，计算日收益率序列。
        """
        from collections import defaultdict
        by_date: dict = {}
        for s in self.snapshots:
            d = s.time.date()
            by_date[d] = s.portfolio_value_usdc   # 覆盖取当日最后一条

        sorted_vals = [v for _, v in sorted(by_date.items())]
        rets: list[float] = []
        for i in range(1, len(sorted_vals)):
            prev = sorted_vals[i - 1]
            if prev > 0:
                rets.append((sorted_vals[i] - prev) / prev)
        return rets

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def print_report(self) -> None:
        from src.backtesting_engine.report import print_report
        print_report(self)

    def to_dataframe(self):
        from src.backtesting_engine.report import to_dataframe
        return to_dataframe(self)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _std(values: list[float]) -> float:
    """总体标准差。"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
