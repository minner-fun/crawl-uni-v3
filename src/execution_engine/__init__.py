"""
execution_engine
================
策略层与 Uniswap V3 链上交互的执行层。

主入口：PositionManager
    提供读写 NonfungiblePositionManager 合约的完整接口：
    - 读仓位   : get_position(token_id)
    - 开仓     : mint(MintParams)
    - 加流动性 : increase_liquidity(IncreaseLiquidityParams)
    - 减流动性 : decrease_liquidity(DecreaseLiquidityParams)
    - 收手续费 : collect(CollectParams)
    - 销毁空仓 : burn(token_id)

用法示例::

    from src.execution_engine import build_position_manager

    pm = build_position_manager()
    pos = pm.get_position(12345)
    result = pm.mint(MintParams(...))
"""

from src.execution_engine.position_manager import (
    PositionManager,
    PositionInfo,
    MintParams,
    MintResult,
    IncreaseLiquidityParams,
    DecreaseLiquidityParams,
    CollectParams,
    LiquidityResult,
    AmountsResult,
    build_position_manager,
)

__all__ = [
    "PositionManager",
    "build_position_manager",
    "PositionInfo",
    "MintParams",
    "MintResult",
    "IncreaseLiquidityParams",
    "DecreaseLiquidityParams",
    "CollectParams",
    "LiquidityResult",
    "AmountsResult",
]
