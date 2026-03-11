import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

from web3 import Web3

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.Constracts import (
    MAINNET_RPC_URL,
    POOLS_ABI,
    UNISWAP_V3_USDC_ETH_POOL_ADDRESS,
)
from src.db import repository as repo
from src.db.database import get_session, init_db

# ── 扫描配置 ──────────────────────────────────────────────────────────────────
CHAIN_ID   = 1
FROM_BLOCK = 24554542   # 起始区块（Uniswap V3 部署区块）
TO_BLOCK   = 24637017   # 结束区块（示例：约扫描 100 万个区块）

# 每处理 CHUNK_SIZE 个区块做一次小结：获取时间戳、写库、更新进度
# 调大：RPC 批次少，DB 事务次数少，但单次内存占用多、中断损失大
# 调小：进度更频繁，中断损失小，但事务开销略大
CHUNK_SIZE = 500

ALCHEMY_FREE_MAX_RANGE = 10     # eth_getLogs 单次最多查的区块数
REQUEST_INTERVAL       = 0.05    # eth_getLogs 批次间隔（秒）
BLOCK_TS_INTERVAL      = 0.05    # eth_getBlockByNumber 间隔（秒）
RETRY_MAX              = 5
RETRY_BASE_DELAY       = 2.0

# ── RPC 连接 ──────────────────────────────────────────────────────────────────
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

# ── 合约对象 & 事件 topic ─────────────────────────────────────────────────────
pools_address = w3.to_checksum_address(UNISWAP_V3_USDC_ETH_POOL_ADDRESS)
pool_contract = w3.eth.contract(address=pools_address, abi=json.loads(POOLS_ABI))

POOL_MINT_TOPIC    = w3.keccak(text="Mint(address,address,int24,int24,uint128,uint256,uint256)").hex()
POOL_BURN_TOPIC    = w3.keccak(text="Burn(address,int24,int24,uint128,uint256,uint256)").hex()
POOL_COLLECT_TOPIC = w3.keccak(text="Collect(address,address,int24,int24,uint128,uint128)").hex()
POOL_SWAP_TOPIC    = w3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

TOPIC_TO_EVENT = {
    POOL_MINT_TOPIC:    pool_contract.events.Mint,
    POOL_BURN_TOPIC:    pool_contract.events.Burn,
    POOL_COLLECT_TOPIC: pool_contract.events.Collect,
    POOL_SWAP_TOPIC:    pool_contract.events.Swap,
}
ALL_TOPICS = [POOL_MINT_TOPIC, POOL_BURN_TOPIC, POOL_COLLECT_TOPIC, POOL_SWAP_TOPIC]


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _get_logs_with_retry(params: dict) -> list:
    """带指数退避重试的 eth_getLogs，429 时等待后重试。"""
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
                delay *= 2
            else:
                raise
    return []


def _rpc_fetch_timestamp(block_number: int) -> datetime:
    """从链上获取单个区块时间戳，附带限速间隔。"""
    block = w3.eth.get_block(block_number)
    time.sleep(BLOCK_TS_INTERVAL)
    return datetime.utcfromtimestamp(block["timestamp"])


def _fetch_chunk_logs(chunk_start: int, chunk_end: int) -> list:
    """
    在 chunk_start ~ chunk_end 范围内，按 ALCHEMY_FREE_MAX_RANGE 分批
    拉取四种事件的原始日志，返回合并后的列表。
    """
    raw_logs    = []
    batch_start = chunk_start
    while batch_start <= chunk_end:
        batch_end = min(batch_start + ALCHEMY_FREE_MAX_RANGE - 1, chunk_end)
        logs = _get_logs_with_retry({
            "address":   pools_address,
            "fromBlock": hex(batch_start),
            "toBlock":   hex(batch_end),
            "topics":    [ALL_TOPICS],
        })
        raw_logs.extend(logs)
        batch_start = batch_end + 1
        time.sleep(REQUEST_INTERVAL)
    return raw_logs


def _parse_logs(raw_logs: list) -> list:
    """按 topic0 路由，把 raw log 解析为结构化事件列表。"""
    events = []
    for log in raw_logs:
        topic0    = log["topics"][0].hex()
        event_cls = TOPIC_TO_EVENT.get(topic0)
        if event_cls:
            events.append(event_cls().process_log(log))
    return events


def _build_common_fields(event, ts_map: dict) -> dict:
    """提取四张事件表的公共字段。"""
    return {
        "chain_id":        CHAIN_ID,
        "pool_address":    pools_address,
        "block_number":    event["blockNumber"],
        "block_timestamp": ts_map[event["blockNumber"]],
        "tx_hash":         event["transactionHash"].hex(),
        "log_index":       event["logIndex"],
    }


def _save_chunk(session, events: list, ts_map: dict, chunk_end: int) -> dict:
    """
    在同一个 session / 事务里：
      1. 将本批所有事件写入对应表（幂等）
      2. 更新 sync_cursor 至 chunk_end

    若事务中任意步骤失败，整体回滚，sync_cursor 不会前进，
    下次重跑会重新处理这个 chunk，保证数据不丢。
    """
    counts = {"Mint": 0, "Burn": 0, "Collect": 0, "Swap": 0}

    for event in events:
        name   = event["event"]
        args   = event["args"]
        common = _build_common_fields(event, ts_map)

        if name == "Mint":
            ok = repo.insert_mint(session, {
                **common,
                "sender":           w3.to_checksum_address(args["sender"]),
                "owner":            w3.to_checksum_address(args["owner"]),
                "tick_lower":       args["tickLower"],
                "tick_upper":       args["tickUpper"],
                "amount_liquidity": args["amount"],
                "amount0_raw":      args["amount0"],
                "amount1_raw":      args["amount1"],
            })
        elif name == "Burn":
            ok = repo.insert_burn(session, {
                **common,
                "owner":            w3.to_checksum_address(args["owner"]),
                "tick_lower":       args["tickLower"],
                "tick_upper":       args["tickUpper"],
                "amount_liquidity": args["amount"],
                "amount0_raw":      args["amount0"],
                "amount1_raw":      args["amount1"],
            })
        elif name == "Collect":
            ok = repo.insert_collect(session, {
                **common,
                "owner":       w3.to_checksum_address(args["owner"]),
                "recipient":   w3.to_checksum_address(args["recipient"]),
                "tick_lower":  args["tickLower"],
                "tick_upper":  args["tickUpper"],
                "amount0_raw": args["amount0"],
                "amount1_raw": args["amount1"],
            })
        elif name == "Swap":
            ok = repo.insert_swap(session, {
                **common,
                "sender":         w3.to_checksum_address(args["sender"]),
                "recipient":      w3.to_checksum_address(args["recipient"]),
                "amount0_raw":    args["amount0"],
                "amount1_raw":    args["amount1"],
                "sqrt_price_x96": args["sqrtPriceX96"],
                "liquidity":      args["liquidity"],
                "tick":           args["tick"],
            })
        else:
            continue

        if ok:
            counts[name] += 1

    repo.update_sync_cursor(
        session,
        chain_id          = CHAIN_ID,
        target_type       = "pool",
        target_address    = pools_address,
        last_synced_block = chunk_end,
    )
    return counts


# ── 初始化数据库 & 断点续跑检测 ───────────────────────────────────────────────
init_db()
print("数据库表已就绪")

with get_session() as _s:
    last_synced = repo.get_sync_cursor(_s, CHAIN_ID, "pool", pools_address)

# 若存在历史进度且在扫描范围内，则从上次结束处继续；否则从 FROM_BLOCK 起
if last_synced is not None and last_synced >= FROM_BLOCK:
    actual_start = last_synced + 1
    print(f"检测到历史进度：已同步至区块 {last_synced}，从 {actual_start} 继续\n")
else:
    actual_start = FROM_BLOCK
    print(f"全新扫描，从区块 {actual_start} 开始\n")

if actual_start > TO_BLOCK:
    print("目标范围已全部同步完成，无需重跑。")
    sys.exit(0)

total_blocks = TO_BLOCK - actual_start + 1
total_chunks = math.ceil(total_blocks / CHUNK_SIZE)
print(f"扫描范围 : {actual_start:,} ~ {TO_BLOCK:,}（共 {total_blocks:,} 个区块）")
print(f"分块大小 : {CHUNK_SIZE:,} 个区块 / chunk，共 {total_chunks} 个 chunk\n")


# ── 主循环：按 chunk 逐批处理 ─────────────────────────────────────────────────
global_counts = {"Mint": 0, "Burn": 0, "Collect": 0, "Swap": 0}
chunk_idx     = 0
chunk_start   = actual_start

while chunk_start <= TO_BLOCK:
    chunk_end  = min(chunk_start + CHUNK_SIZE - 1, TO_BLOCK)
    chunk_idx += 1
    progress   = chunk_idx / total_chunks * 100

    print(f"[{chunk_idx}/{total_chunks} | {progress:5.1f}%] "
          f"区块 {chunk_start:,} ~ {chunk_end:,}", end="  ", flush=True)

    # ① 批量拉取本 chunk 内的原始日志
    try:
        raw_logs = _fetch_chunk_logs(chunk_start, chunk_end)
    except Exception as e:
        print(f"\n  拉取失败，中止: {e}")
        sys.exit(1)

    events = _parse_logs(raw_logs)
    print(f"事件 {len(events):4d} 条", end="  ", flush=True)

    if events:
        with get_session() as session:
            unique_blocks = {e["blockNumber"] for e in events}

            # 统计哪些区块需要走 RPC（用于日志展示）
            rpc_needed = [bn for bn in unique_blocks
                          if repo.get_block_timestamp(session, CHAIN_ID, bn) is None]

            # ② 获取区块时间戳（DB 优先，缺失走 RPC 并持久化）
            ts_map = repo.get_or_fetch_block_timestamps(
                session,
                chain_id      = CHAIN_ID,
                block_numbers = unique_blocks,
                rpc_fetcher   = _rpc_fetch_timestamp,
            )

            # ③ 写入事件 + 更新 sync_cursor（同一事务，失败全回滚）
            counts = _save_chunk(session, events, ts_map, chunk_end)

        for k, v in counts.items():
            global_counts[k] += v

        rpc_hit = len(rpc_needed)
        db_hit  = len(unique_blocks) - rpc_hit
        print(f"ts(DB {db_hit}/RPC {rpc_hit})  "
              f"Mint {counts['Mint']} Burn {counts['Burn']} "
              f"Collect {counts['Collect']} Swap {counts['Swap']}")
    else:
        # 无事件也推进进度，避免下次重扫这段空区块
        with get_session() as session:
            repo.update_sync_cursor(
                session,
                chain_id          = CHAIN_ID,
                target_type       = "pool",
                target_address    = pools_address,
                last_synced_block = chunk_end,
            )
        print("(无事件)")

    chunk_start = chunk_end + 1


# ── 全量汇总 ──────────────────────────────────────────────────────────────────
print("\n" + "─" * 50)
print(f"扫描完成：{actual_start:,} ~ {TO_BLOCK:,}")
print("累计写入：")
for event_type, count in global_counts.items():
    print(f"  {event_type:8s}: {count:,} 条")
print(f"最终进度已更新至区块 {TO_BLOCK:,}")
