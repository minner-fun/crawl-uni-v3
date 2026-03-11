import json
import sys
import time
from pathlib import Path

from web3 import Web3

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.Constracts import (
    ERC20_ABI,
    MAINNET_RPC_URL,
    UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V3_FACTORY_ADDRESS,
)
from src.db import repository as repo
from src.db.database import get_session, init_db

# ── 常量 ────────────────────────────────────────────────────────────────────
CHAIN_ID             = 1
ALCHEMY_FREE_MAX_RANGE = 10   # Alchemy 免费套餐 eth_getLogs 单次最多查 10 个区块

# eth_getLogs 每次消耗 75 CU，免费套餐上限 330 CU/s，约合 4 次/s
# 保守取 0.3s 间隔（约 3.3 次/s），留一些余量给其他请求（如 ERC20 call）
REQUEST_INTERVAL = 0.3        # 每批拉取之间的正常间隔（秒）
RETRY_MAX        = 5          # 遇到 429 最多重试次数
RETRY_BASE_DELAY = 2.0        # 第一次退避等待时间（秒），每次翻倍

FROM_BLOCK = 12389621
TO_BLOCK   = 13389621

# ── RPC 连接 ─────────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(MAINNET_RPC_URL))

print("检查 RPC 连通性...")
if not w3.is_connected():
    print("RPC 连接失败，请检查 MAINNET_RPC_URL。")
    sys.exit(1)

try:
    print(f"RPC 连接成功: chain_id={w3.eth.chain_id}, latest_block={w3.eth.block_number}")
except Exception as e:
    print(f"RPC 读取链信息失败 -> {e}")
    sys.exit(1)

# ── 合约对象 ──────────────────────────────────────────────────────────────────
factory_address = w3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS)
factory         = w3.eth.contract(address=factory_address, abi=json.loads(UNISWAP_V3_FACTORY_ABI))
_erc20_abi      = json.loads(ERC20_ABI)

POOL_CREATED_TOPIC = w3.keccak(
    text="PoolCreated(address,address,uint24,int24,address)"
).hex()


# ── Token 采集辅助 ────────────────────────────────────────────────────────────

# 单次运行内的内存缓存，避免对同一 token 重复查 DB / 重复打 RPC
_token_cache: set[str] = set()


def _fetch_token_from_chain(token_address: str) -> dict:
    """
    调用链上 ERC20 合约读取 symbol / name / decimals。
    任何一个字段调用失败（如老旧 bytes32 类型合约）均用默认值兜底，
    保证不会因个别奇怪 token 打断整体采集流程。
    """
    contract = w3.eth.contract(
        address=w3.to_checksum_address(token_address),
        abi=_erc20_abi,
    )

    def safe_call(fn):
        try:
            return fn().call()
        except Exception:
            return None

    symbol   = safe_call(contract.functions.symbol)
    name     = safe_call(contract.functions.name)
    decimals = safe_call(contract.functions.decimals)

    return {
        "token_address": w3.to_checksum_address(token_address),
        "symbol":   symbol,
        "name":     name,
        "decimals": decimals if decimals is not None else 18,
        "chain_id": CHAIN_ID,
    }


def _ensure_token(session, token_address: str) -> None:
    """
    确保 token 存在于数据库中。
    优先级：内存缓存 → DB 查询 → 链上采集。
    """
    addr = w3.to_checksum_address(token_address)

    # 本次运行内已处理过，直接跳过
    if addr in _token_cache:
        return

    existing = repo.get_token(session, addr)
    if existing is not None:
        print(f"    token 已存在，跳过采集: {addr}  ({existing.symbol})")
    else:
        print(f"    采集新 token: {addr}")
        info = _fetch_token_from_chain(addr)
        repo.upsert_token(session, info)
        print(f"      symbol={info['symbol']}, name={info['name']}, decimals={info['decimals']}")

    _token_cache.add(addr)


# ── RPC 限流工具 ──────────────────────────────────────────────────────────────

def _get_logs_with_retry(params: dict) -> list:
    """
    带指数退避重试的 eth_getLogs。
    遇到 429（超出 Alchemy CU 限额）时等待后重试，而不是直接退出。
    """
    delay = RETRY_BASE_DELAY
    for attempt in range(1, RETRY_MAX + 1):
        try:
            return w3.eth.get_logs(params)
        except Exception as e:
            if "429" in str(e):
                if attempt == RETRY_MAX:
                    print(f"    已重试 {RETRY_MAX} 次仍触发限流，放弃。")
                    raise
                print(f"    触发限流 (429)，{delay:.1f}s 后第 {attempt} 次重试...")
                time.sleep(delay)
                delay *= 2  # 指数退避
            else:
                raise
    return []  # 不会到达这里，仅满足类型检查


# ── 数据库初始化 ───────────────────────────────────────────────────────────────
init_db()
print("数据库表已就绪\n")


# ── 批量拉取 PoolCreated 日志 ──────────────────────────────────────────────────
raw_logs   = []
batch_start = FROM_BLOCK

while batch_start <= TO_BLOCK:
    batch_end = min(batch_start + ALCHEMY_FREE_MAX_RANGE - 1, TO_BLOCK)
    print(f"  拉取区块 {batch_start} ~ {batch_end} ...")
    try:
        batch_logs = _get_logs_with_retry({
            "address":   factory_address,
            "fromBlock": hex(batch_start),
            "toBlock":   hex(batch_end),
            "topics":    [POOL_CREATED_TOPIC],
        })
        raw_logs.extend(batch_logs)
    except Exception as e:
        print(f"获取区块 {batch_start}~{batch_end} 最终失败: {e}")
        sys.exit(1)
    batch_start = batch_end + 1
    time.sleep(REQUEST_INTERVAL)  # 限速：控制请求频率，避免触发 429

events = [factory.events.PoolCreated().process_log(log) for log in raw_logs]
print(f"\n共获取到 {len(events)} 个 PoolCreated 事件\n")


# ── 存入数据库 ─────────────────────────────────────────────────────────────────
# 整批 events 放在同一个 session / 事务里：
#   1. 先写 token0 / token1（满足 pools 表的外键约束）
#   2. 再写 pool
#   3. 最后更新爬取进度
with get_session() as session:
    for event in events:
        args      = event["args"]
        token0    = w3.to_checksum_address(args["token0"])
        token1    = w3.to_checksum_address(args["token1"])
        pool_addr = w3.to_checksum_address(args["pool"])

        print(f"Pool {pool_addr}  fee={args['fee']}  tick_spacing={args['tickSpacing']}")

        # ① 确保 token0 / token1 已入库（链上采集或已存在）
        _ensure_token(session, token0)
        _ensure_token(session, token1)

        # ② 写入 pool（幂等，重复运行不报错）
        repo.upsert_pool(session, {
            "pool_address":    pool_addr,
            "chain_id":        CHAIN_ID,
            "token0_address":  token0,
            "token1_address":  token1,
            "fee":             args["fee"],
            "tick_spacing":    args["tickSpacing"],
            "created_block":   event["blockNumber"],
            "created_tx_hash": event["transactionHash"].hex(),
        })
        print(f"  pool 写入 DB ✓")

    # ③ 更新 factory 的爬取进度至本批最末区块
    repo.update_sync_cursor(
        session,
        chain_id       = CHAIN_ID,
        target_type    = "factory",
        target_address = factory_address,
        last_synced_block = TO_BLOCK,
    )
    print(f"\n爬取进度已更新至区块 {TO_BLOCK}")

print("\nDone.")
