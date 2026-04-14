"""
回测报告输出
============

print_report() : 终端格式化输出完整回测报告
to_dataframe() : 将快照导出为 pandas DataFrame（便于绘图和进一步分析）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backtesting_engine.metrics import BacktestResult


def print_report(result: "BacktestResult") -> None:
    """将 BacktestResult 格式化输出到终端。"""
    cfg = result.config
    s0  = result.snapshots[0]  if result.snapshots else None
    se  = result.snapshots[-1] if result.snapshots else None
    dur = result._duration_days

    LINE = "═" * 64

    print(f"\n{LINE}")
    print(f"  Backtest Report")
    print(f"  Pool   : {cfg.pool_address}")
    print(f"  Period : {cfg.from_dt.date()} → {cfg.to_dt.date()}  ({dur:.0f} days)")
    print(LINE)

    # ── 收益概览 ──────────────────────────────────────────────────────────
    init  = result._initial_investment
    final = se.portfolio_value_usdc if se else 0.0

    print(f"\n  【收益概览】")
    print(f"  {'初始投入（估算）':<20s}: {init:>10.2f} USDC")
    print(f"  {'期末净值':<20s}: {final:>10.2f} USDC")
    print(f"  {'LP 总回报率':<20s}: {result.total_return_pct:>+9.2f} %")
    print(f"  {'HODL 回报率':<20s}: {result.hodl_return_pct:>+9.2f} %")
    print(f"  {'vs HODL 超额收益':<20s}: {result.alpha_vs_hodl:>+9.2f} %")

    # ── 年化指标 ──────────────────────────────────────────────────────────
    print(f"\n  【年化指标】")
    print(f"  {'Gross Fee APR':<20s}: {result.gross_fee_apr:>+9.2f} %")
    print(f"  {'Net APR':<20s}: {result.net_apr:>+9.2f} %")

    # ── 收益分解 ──────────────────────────────────────────────────────────
    print(f"\n  【收益分解（USDC）】")
    print(f"  {'手续费收入':<20s}: {result.total_fees_usdc:>+10.2f}")
    print(f"  {'无常损失 IL':<20s}: {result.total_il_usdc:>+10.2f}")
    print(f"  {'Gas 成本':<20s}: {-result.total_gas_usdc:>+10.2f}")
    print(f"  {'手续费-IL（净贡献）':<20s}: {result.fee_minus_il_usdc:>+10.2f}")

    # ── V3 专属 ───────────────────────────────────────────────────────────
    print(f"\n  【V3 LP 专属指标】")
    print(f"  {'In-Range 时间':<20s}: {result.in_range_pct:>9.1f} %")
    print(f"  {'Rebalance 次数':<20s}: {result.total_rebalances:>9d} 次")
    print(f"  {'平均持仓时长':<20s}: {result.avg_hold_hours:>9.1f} 小时")

    if s0 and se:
        eth_start = s0.eth_price_usdc
        eth_end   = se.eth_price_usdc
        eth_chg   = (eth_end - eth_start) / eth_start * 100
        print(f"  {'ETH 价格区间':<20s}: ${eth_start:>7.0f} → ${eth_end:.0f}  ({eth_chg:+.1f}%)")

    # ── 风险指标 ──────────────────────────────────────────────────────────
    print(f"\n  【风险指标】")
    print(f"  {'Max Drawdown':<20s}: {result.max_drawdown:>+9.2f} %")
    print(f"  {'Sharpe Ratio':<20s}: {result.sharpe_ratio:>+9.3f}")
    print(f"  {'Sortino Ratio':<20s}: {result.sortino_ratio:>+9.3f}")
    print(f"  {'日收益率波动率':<20s}: {result.daily_return_vol * 100:>9.3f} %")

    print(f"\n{LINE}\n")


def to_dataframe(result: "BacktestResult"):
    """
    将快照列表导出为 pandas DataFrame，以 time 为索引。

    常用分析：
        df["portfolio_value_usdc"].plot()          # 净值曲线
        df["il_usdc"].plot()                       # IL 曲线
        df[df["in_range"]].shape[0] / len(df)     # in-range 比例
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("请安装 pandas：pip install pandas")

    records = [
        {
            "time":                   s.time,
            "eth_price_usdc":         s.eth_price_usdc,
            "current_tick":           s.current_tick,
            "in_range":               s.in_range,
            "has_position":           s.has_position,
            "position_value_usdc":    s.position_value_usdc,
            "fees_earned_usdc":       s.fees_earned_usdc,
            "gas_cost_usdc":          s.gas_cost_usdc,
            "il_usdc":                s.il_usdc,
            "hodl_value_usdc":        s.hodl_value_usdc,
            "portfolio_value_usdc":   s.portfolio_value_usdc,
            "rebalance_count":        s.rebalance_count,
        }
        for s in result.snapshots
    ]

    df = pd.DataFrame(records).set_index("time")

    # 派生列（便于分析）
    df["daily_return"] = df["portfolio_value_usdc"].pct_change()
    df["fee_income_hourly"] = df["fees_earned_usdc"].diff().clip(lower=0)

    return df
