"""
策略抽象基类
============

所有策略都继承 BaseStrategy，实现 evaluate() 方法。
evaluate() 接收市场上下文和当前仓位状态，返回 Decision。

Decision.action 取值：
    OPEN      : 无仓位且满足开仓条件，执行 mint
    HOLD      : 保持现状，不操作
    REBALANCE : 价格接近区间边界，平仓后按新价格重开
    CLOSE     : 满足退出条件，完整平仓后不再开仓
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.strategy_engine.context import ActivePosition, MarketContext


class StrategyDecision(str, Enum):
    OPEN      = "OPEN"
    HOLD      = "HOLD"
    REBALANCE = "REBALANCE"
    CLOSE     = "CLOSE"


@dataclass
class Decision:
    """策略评估结果。"""

    action:          StrategyDecision
    reason:          str

    # 仅 OPEN / REBALANCE 时填充
    tick_lower:      Optional[int] = None
    tick_upper:      Optional[int] = None
    amount0_desired: Optional[int] = None
    amount1_desired: Optional[int] = None

    # 附加元数据（写入 strategy_signals.reason）
    meta: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    """
    策略抽象基类。

    子类只需实现 evaluate()，无需关心执行和持久化细节。
    """

    @abstractmethod
    def evaluate(
        self,
        ctx: "MarketContext",
        position: Optional["ActivePosition"],
    ) -> Decision:
        """
        根据市场上下文和当前持仓状态，返回策略决策。

        Parameters
        ----------
        ctx      : MarketContext
            当前链上状态 + 近期聚合指标。
        position : ActivePosition | None
            DB 中记录的当前活跃仓位，无持仓则为 None。

        Returns
        -------
        Decision
        """
        ...
