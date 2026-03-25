"""
PositionManager
===============
封装 Uniswap V3 NonfungiblePositionManager 合约的读写操作，
供策略层直接调用，无需关心 web3 / ABI / gas / approve 细节。

环境变量依赖（需写入 .env）：
    MAINNET_RPC_URL      : Ethereum 主网 RPC 端点
    EXECUTOR_PRIVATE_KEY : 执行账户私钥（0x 开头）

写操作执行顺序：
    1. 自动检查 ERC-20 allowance，不足时先发 approve 交易
    2. 构建目标交易并签名广播
    3. 等待 receipt，确认后返回结构化结果
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import ContractLogicError

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# ---------------------------------------------------------------------------
# ABI
# ---------------------------------------------------------------------------

from src.Constracts import (
    MAINNET_RPC_URL,
    UNISWAP_V3_NONFUNGIBLE_POSITION_MANAGER,
    UNISWAP_V3_NONFUNGIBLE_POSITION_MANAGER_ABI,
)

# approve / allowance 接口，仅用于执行层内部授权
_ERC20_EXEC_ABI = json.loads("""[
    {
        "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")

# mint 的 deadline 默认宽限（秒）
_DEFAULT_DEADLINE_BUFFER = 600
# gas 估算超出倍数
_GAS_MULTIPLIER = 1.2
# 等待 receipt 的超时（秒）/ 轮询间隔（秒）
_TX_TIMEOUT   = 300
_TX_POLL_INTERVAL = 2


# ---------------------------------------------------------------------------
# 数据结构（策略层使用）
# ---------------------------------------------------------------------------

@dataclass
class PositionInfo:
    """NonfungiblePositionManager.positions(tokenId) 的返回值。"""
    token_id:                    int
    nonce:                       int
    operator:                    str
    token0:                      str
    token1:                      str
    fee:                         int
    tick_lower:                  int
    tick_upper:                  int
    liquidity:                   int
    fee_growth_inside0_last_x128: int
    fee_growth_inside1_last_x128: int
    tokens_owed0:                int
    tokens_owed1:                int


@dataclass
class MintParams:
    """
    开仓参数，对应 NonfungiblePositionManager.mint() 的 MintParams struct。

    amount0_desired / amount1_desired : 期望投入的 token 原始数量（最小单位）
    amount0_min / amount1_min         : 滑点保护下限（原始数量）
    deadline                          : Unix 时间戳；None 表示自动取 now + 600s
    """
    token0:           str
    token1:           str
    fee:              int
    tick_lower:       int
    tick_upper:       int
    amount0_desired:  int
    amount1_desired:  int
    amount0_min:      int = 0
    amount1_min:      int = 0
    recipient:        Optional[str] = None   # None → 使用执行账户地址
    deadline:         Optional[int] = None


@dataclass
class MintResult:
    token_id:  int
    liquidity: int
    amount0:   int
    amount1:   int
    tx_hash:   str


@dataclass
class IncreaseLiquidityParams:
    """
    加流动性参数，对应 NonfungiblePositionManager.increaseLiquidity()。
    """
    token_id:         int
    amount0_desired:  int
    amount1_desired:  int
    amount0_min:      int = 0
    amount1_min:      int = 0
    deadline:         Optional[int] = None


@dataclass
class DecreaseLiquidityParams:
    """
    减流动性参数，对应 NonfungiblePositionManager.decreaseLiquidity()。

    liquidity : 要移除的流动性数量（最大值可从 get_position().liquidity 取得）
    """
    token_id:    int
    liquidity:   int
    amount0_min: int = 0
    amount1_min: int = 0
    deadline:    Optional[int] = None


@dataclass
class CollectParams:
    """
    收取手续费参数，对应 NonfungiblePositionManager.collect()。

    amount0_max / amount1_max 设为 2**128-1 表示收取全部可用手续费。
    """
    token_id:     int
    recipient:    Optional[str] = None   # None → 使用执行账户地址
    amount0_max:  int = 2**128 - 1
    amount1_max:  int = 2**128 - 1


@dataclass
class LiquidityResult:
    liquidity: int
    amount0:   int
    amount1:   int
    tx_hash:   str


@dataclass
class AmountsResult:
    amount0:  int
    amount1:  int
    tx_hash:  str


# ---------------------------------------------------------------------------
# PositionManager
# ---------------------------------------------------------------------------

class PositionManager:
    """
    Uniswap V3 仓位管理执行器。

    Parameters
    ----------
    w3 : Web3
        已连接的 Web3 实例。
    private_key : str
        执行账户的私钥（0x 开头）。
    """

    def __init__(self, w3: Web3, private_key: str) -> None:
        self.w3 = w3
        self._account = w3.eth.account.from_key(private_key)
        self._address = self._account.address

        self._npm = w3.eth.contract(
            address=w3.to_checksum_address(UNISWAP_V3_NONFUNGIBLE_POSITION_MANAGER),
            abi=json.loads(UNISWAP_V3_NONFUNGIBLE_POSITION_MANAGER_ABI),
        )

    # ------------------------------------------------------------------
    # 只读接口
    # ------------------------------------------------------------------

    def get_position(self, token_id: int) -> PositionInfo:
        """
        读取链上仓位信息。

        Parameters
        ----------
        token_id : int
            NonfungiblePositionManager 发行的 NFT tokenId。

        Returns
        -------
        PositionInfo
        """
        (
            nonce,
            operator,
            token0,
            token1,
            fee,
            tick_lower,
            tick_upper,
            liquidity,
            fee_growth_inside0_last_x128,
            fee_growth_inside1_last_x128,
            tokens_owed0,
            tokens_owed1,
        ) = self._npm.functions.positions(token_id).call()

        return PositionInfo(
            token_id=token_id,
            nonce=nonce,
            operator=operator,
            token0=token0,
            token1=token1,
            fee=fee,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=liquidity,
            fee_growth_inside0_last_x128=fee_growth_inside0_last_x128,
            fee_growth_inside1_last_x128=fee_growth_inside1_last_x128,
            tokens_owed0=tokens_owed0,
            tokens_owed1=tokens_owed1,
        )

    # ------------------------------------------------------------------
    # 写操作 - 公共接口
    # ------------------------------------------------------------------

    def mint(self, params: MintParams) -> MintResult:
        """
        开新仓（Mint 新 NFT）。

        内部会自动 approve token0 / token1（若 allowance 不足）。

        Parameters
        ----------
        params : MintParams

        Returns
        -------
        MintResult
            包含 token_id, liquidity, amount0, amount1, tx_hash。
        """
        recipient = self._resolve_recipient(params.recipient)
        deadline  = self._resolve_deadline(params.deadline)

        self._ensure_allowance(params.token0, params.amount0_desired)
        self._ensure_allowance(params.token1, params.amount1_desired)

        mint_params = {
            "token0":          self.w3.to_checksum_address(params.token0),
            "token1":          self.w3.to_checksum_address(params.token1),
            "fee":             params.fee,
            "tickLower":       params.tick_lower,
            "tickUpper":       params.tick_upper,
            "amount0Desired":  params.amount0_desired,
            "amount1Desired":  params.amount1_desired,
            "amount0Min":      params.amount0_min,
            "amount1Min":      params.amount1_min,
            "recipient":       recipient,
            "deadline":        deadline,
        }

        receipt = self._send_tx(
            self._npm.functions.mint(mint_params)
        )
        tx_hash = receipt["transactionHash"].hex()

        # 解析 IncreaseLiquidity 事件（mint 触发）
        logs = self._npm.events.IncreaseLiquidity().process_receipt(receipt)
        if logs:
            ev = logs[0]["args"]
            return MintResult(
                token_id=ev["tokenId"],
                liquidity=ev["liquidity"],
                amount0=ev["amount0"],
                amount1=ev["amount1"],
                tx_hash=tx_hash,
            )

        # 若事件解析失败，降级返回零值（交易已上链）
        return MintResult(token_id=0, liquidity=0, amount0=0, amount1=0, tx_hash=tx_hash)

    def increase_liquidity(self, params: IncreaseLiquidityParams) -> LiquidityResult:
        """
        向已有仓位增加流动性。

        内部会自动 approve token（若 allowance 不足）。

        Parameters
        ----------
        params : IncreaseLiquidityParams

        Returns
        -------
        LiquidityResult
        """
        pos = self.get_position(params.token_id)
        self._ensure_allowance(pos.token0, params.amount0_desired)
        self._ensure_allowance(pos.token1, params.amount1_desired)

        deadline = self._resolve_deadline(params.deadline)
        increase_params = {
            "tokenId":         params.token_id,
            "amount0Desired":  params.amount0_desired,
            "amount1Desired":  params.amount1_desired,
            "amount0Min":      params.amount0_min,
            "amount1Min":      params.amount1_min,
            "deadline":        deadline,
        }

        receipt = self._send_tx(
            self._npm.functions.increaseLiquidity(increase_params)
        )
        tx_hash = receipt["transactionHash"].hex()

        logs = self._npm.events.IncreaseLiquidity().process_receipt(receipt)
        if logs:
            ev = logs[0]["args"]
            return LiquidityResult(
                liquidity=ev["liquidity"],
                amount0=ev["amount0"],
                amount1=ev["amount1"],
                tx_hash=tx_hash,
            )

        return LiquidityResult(liquidity=0, amount0=0, amount1=0, tx_hash=tx_hash)

    def decrease_liquidity(self, params: DecreaseLiquidityParams) -> AmountsResult:
        """
        从仓位移除流动性（资金留在合约，需随后调用 collect 提取）。

        Parameters
        ----------
        params : DecreaseLiquidityParams

        Returns
        -------
        AmountsResult
            注意：返回值是"可提取"金额，资金仍在合约内，需调用 collect。
        """
        deadline = self._resolve_deadline(params.deadline)
        decrease_params = {
            "tokenId":    params.token_id,
            "liquidity":  params.liquidity,
            "amount0Min": params.amount0_min,
            "amount1Min": params.amount1_min,
            "deadline":   deadline,
        }

        receipt = self._send_tx(
            self._npm.functions.decreaseLiquidity(decrease_params)
        )
        tx_hash = receipt["transactionHash"].hex()

        logs = self._npm.events.DecreaseLiquidity().process_receipt(receipt)
        if logs:
            ev = logs[0]["args"]
            return AmountsResult(amount0=ev["amount0"], amount1=ev["amount1"], tx_hash=tx_hash)

        return AmountsResult(amount0=0, amount1=0, tx_hash=tx_hash)

    def collect(self, params: CollectParams) -> AmountsResult:
        """
        收取仓位内累积的手续费（以及 decreaseLiquidity 后的待提资金）。

        Parameters
        ----------
        params : CollectParams

        Returns
        -------
        AmountsResult
        """
        recipient = self._resolve_recipient(params.recipient)
        collect_params = {
            "tokenId":    params.token_id,
            "recipient":  recipient,
            "amount0Max": params.amount0_max,
            "amount1Max": params.amount1_max,
        }

        receipt = self._send_tx(
            self._npm.functions.collect(collect_params)
        )
        tx_hash = receipt["transactionHash"].hex()

        logs = self._npm.events.Collect().process_receipt(receipt)
        if logs:
            ev = logs[0]["args"]
            return AmountsResult(amount0=ev["amount0"], amount1=ev["amount1"], tx_hash=tx_hash)

        return AmountsResult(amount0=0, amount1=0, tx_hash=tx_hash)

    def burn(self, token_id: int) -> str:
        """
        销毁流动性已清零的仓位 NFT，释放链上存储 Gas 返还。

        调用前请确认 get_position(token_id).liquidity == 0 且
        tokens_owed0 == tokens_owed1 == 0（即已 collect 完毕）。

        Parameters
        ----------
        token_id : int

        Returns
        -------
        str : tx_hash
        """
        receipt = self._send_tx(self._npm.functions.burn(token_id))
        return receipt["transactionHash"].hex()

    def close_position(self, token_id: int) -> dict:
        """
        完整关仓流程：decreaseLiquidity → collect → burn。

        convenience 方法，策略层一次调用即可完全退出仓位。

        Parameters
        ----------
        token_id : int

        Returns
        -------
        dict 包含 decrease / collect / burn 的结果。
        """
        pos = self.get_position(token_id)

        decrease_result: Optional[AmountsResult] = None
        if pos.liquidity > 0:
            decrease_result = self.decrease_liquidity(
                DecreaseLiquidityParams(token_id=token_id, liquidity=pos.liquidity)
            )

        collect_result = self.collect(CollectParams(token_id=token_id))
        burn_tx = self.burn(token_id)

        return {
            "decrease": decrease_result,
            "collect":  collect_result,
            "burn_tx":  burn_tx,
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _resolve_recipient(self, recipient: Optional[str]) -> str:
        if recipient is None:
            return self._address
        return self.w3.to_checksum_address(recipient)

    def _resolve_deadline(self, deadline: Optional[int]) -> int:
        if deadline is None:
            return int(time.time()) + _DEFAULT_DEADLINE_BUFFER
        return deadline

    def _ensure_allowance(self, token_address: str, amount: int) -> None:
        """
        检查 token 的 allowance，不足时发送 approve 交易。
        approve 金额 = amount（精确授权，减少风险）。
        """
        token = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address),
            abi=_ERC20_EXEC_ABI,
        )
        spender = self.w3.to_checksum_address(UNISWAP_V3_NONFUNGIBLE_POSITION_MANAGER)
        current_allowance = token.functions.allowance(self._address, spender).call()

        if current_allowance < amount:
            receipt = self._send_tx(token.functions.approve(spender, amount))
            if receipt["status"] != 1:
                raise RuntimeError(
                    f"approve 失败: token={token_address} amount={amount} "
                    f"tx={receipt['transactionHash'].hex()}"
                )

    def _build_tx(self, fn) -> dict:
        """构建交易参数：gas 估算 + nonce + chainId。"""
        nonce = self.w3.eth.get_transaction_count(self._address, "pending")
        base_tx = {
            "from":  self._address,
            "nonce": nonce,
        }

        try:
            gas_estimate = fn.estimate_gas(base_tx)
            gas = int(gas_estimate * _GAS_MULTIPLIER)
        except ContractLogicError as exc:
            raise RuntimeError(f"gas 估算失败（合约 revert）: {exc}") from exc

        return fn.build_transaction({
            **base_tx,
            "gas":     gas,
            "chainId": self.w3.eth.chain_id,
        })

    def _send_tx(self, fn) -> dict:
        """
        签名、广播交易，等待 receipt，检查状态。

        Returns
        -------
        dict : transaction receipt
        """
        tx = self._build_tx(fn)
        signed = self._account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

        receipt = self.w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=_TX_TIMEOUT, poll_latency=_TX_POLL_INTERVAL
        )

        if receipt["status"] != 1:
            raise RuntimeError(
                f"交易失败（status=0）: {tx_hash.hex()}\n"
                f"receipt: {dict(receipt)}"
            )

        return receipt


# ---------------------------------------------------------------------------
# 工厂函数（策略层直接调用）
# ---------------------------------------------------------------------------

def build_position_manager() -> PositionManager:
    """
    从环境变量创建并返回 PositionManager 实例。

    需要在 .env 中设置：
        MAINNET_RPC_URL       : Ethereum RPC 端点
        EXECUTOR_PRIVATE_KEY  : 执行账户私钥（0x...）

    Returns
    -------
    PositionManager
    """
    rpc_url     = MAINNET_RPC_URL
    private_key = os.environ.get("EXECUTOR_PRIVATE_KEY")

    if not private_key:
        raise EnvironmentError(
            "缺少环境变量 EXECUTOR_PRIVATE_KEY，请在 .env 中配置执行账户私钥。"
        )

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError(f"无法连接 RPC：{rpc_url}")

    return PositionManager(w3=w3, private_key=private_key)
