"""
ws_pool_listener.py — Uniswap V3 Pool 实时事件监听器（WebSocket 版）
======================================================================

架构概述
--------
                 ┌──────────────────────────────┐
  Ethereum Node  │  eth_subscribe("logs", ...)  │
  (WebSocket)    └──────────────┬───────────────┘
                                │ raw log
                                ▼
                       asyncio.Queue (无界)
                                │
                                ▼
                      _event_writer() 协程
                      ├── process_log()   ← 按 log.address 路由解析
                      └── DB 写入 (幂等 INSERT ... ON CONFLICT)

断线重连流程
------------
  WS 断线 / 超时
      ↓
  记录 last_confirmed_block（已写入 DB 的最后区块）
      ↓
  _backfill_http()：HTTP get_logs 补全断线期间遗漏的数据
      ↓
  重新建立 WS 订阅，继续监听

数据一致性
----------
  - 链重组（reorg）：采用"立即写入 + CONFIRM_BLOCKS 策略"：
    仅处理 block_number ≤ latest_block - CONFIRM_BLOCKS 的事件。
    WS 收到的过新事件放入 pending_buffer，等确认后再写 DB。
  - 幂等写入：所有 INSERT 均使用 ON CONFLICT DO NOTHING，
    重跑或补数据不产生重复记录。
  - sync_cursor（target_type="pool_ws"）：记录已写入的最大区块，
    用于重启后的 HTTP backfill 起点。

环境变量（.env）
----------------
  MAINNET_WS_URL  : WebSocket 端点，如 wss://eth-mainnet.g.alchemy.com/v2/KEY
  MAINNET_RPC_URL : HTTP 端点，仅在 backfill 时使用

用法
----
  python -m src.data_collector.ws_pool_listener

  可通过顶部 POOL_ADDRESSES / CONFIRM_BLOCKS 等常量自定义。
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Optional

from web3 import AsyncWeb3, Web3
from web3.providers import WebSocketProvider

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.Constracts import (
    MAINNET_RPC_URL,
    MAINNET_WS_URL,
    POOLS_ABI,
    UNISWAP_V3_USDC_ETH_POOL_ADDRESS,
    UNISWAP_V3_ETH_USDT_POOL_ADDRESS,
    UNISWAP_V3_WBTC_USDC_POOL_ADDRESS,
)
from src.db import repository as repo
from src.db.database import get_session, init_db

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ws_pool_listener")

# ---------------------------------------------------------------------------
# 监听配置（按需修改）
# ---------------------------------------------------------------------------

CHAIN_ID = 1

# 同时订阅的 Pool 地址列表
POOL_ADDRESSES: list[str] = [
    UNISWAP_V3_USDC_ETH_POOL_ADDRESS,
    UNISWAP_V3_ETH_USDT_POOL_ADDRESS,
    UNISWAP_V3_WBTC_USDC_POOL_ADDRESS,
]

CONFIRM_BLOCKS      = 2     # 等待 N 块确认后才写 DB，防链重组
WS_EVENT_TIMEOUT    = 60    # 超过此秒数无新事件，判定 WS 静默断线（秒）
RECONNECT_DELAY     = 5     # 断线后等待重连的基础间隔（秒）
RECONNECT_MAX_DELAY = 60    # 重连等待上限（指数退避）
HTTP_RETRY_MAX      = 5     # HTTP backfill 的最大重试次数
HTTP_FETCH_RANGE    = 10    # HTTP get_logs 单次查询最大区块数（Alchemy 免费限制）
HTTP_FETCH_INTERVAL = 0.05  # HTTP 批次间限速（秒）

# ---------------------------------------------------------------------------
# 合约对象初始化（同步 Web3，仅用于 topic 计算和 HTTP backfill）
# ---------------------------------------------------------------------------

_w3_http = Web3(Web3.HTTPProvider(MAINNET_RPC_URL))

# 按地址建立合约映射，供事件解析路由使用
POOL_CONTRACTS: dict[str, Web3.eth.Contract] = {
    _w3_http.to_checksum_address(addr): _w3_http.eth.contract(
        address=_w3_http.to_checksum_address(addr),
        abi=json.loads(POOLS_ABI),
    )
    for addr in POOL_ADDRESSES
}

POOL_ADDRESSES_CHECKSUM = list(POOL_CONTRACTS.keys())

# Event topic 常量
_keccak = _w3_http.keccak
TOPIC_MINT    = "0x" + _keccak(text="Mint(address,address,int24,int24,uint128,uint256,uint256)").hex()
TOPIC_BURN    = "0x" + _keccak(text="Burn(address,int24,int24,uint128,uint256,uint256)").hex()
TOPIC_COLLECT = "0x" + _keccak(text="Collect(address,address,int24,int24,uint128,uint128)").hex()
TOPIC_SWAP    = "0x" + _keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

ALL_TOPICS = [TOPIC_MINT, TOPIC_BURN, TOPIC_COLLECT, TOPIC_SWAP]

# eth_subscribe filter（topics 第一层 array = OR 逻辑）
WS_FILTER = {
    "address": POOL_ADDRESSES_CHECKSUM,
    "topics":  [ALL_TOPICS],
}

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

_shutdown = False   # 优雅退出标志

# pending_buffer: deque of (block_number, log)，用于 confirm_blocks 缓冲
_pending_buffer: deque[tuple[int, dict]] = deque()

# 会话级统计
_session_counts: dict[str, int] = defaultdict(int)


# ---------------------------------------------------------------------------
# 信号处理
# ---------------------------------------------------------------------------

def _setup_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    main_task: "asyncio.Task | None" = None,
) -> None:
    """注册 SIGINT / SIGTERM，触发优雅退出。

    收到信号后只设置退出标志，让 listener / writer 自己完成收尾，
    避免在 finally 前把 queue / pending_buffer 中的事件直接取消掉。
    """
    def _handle(signum, frame):
        global _shutdown
        logger.info("收到退出信号 (%s)，等待当前批次完成后退出...", signum)
        _shutdown = True

    signal.signal(signal.SIGINT,  _handle)
    signal.signal(signal.SIGTERM, _handle)


_MAIN_TASK: "asyncio.Task | None" = None


# ---------------------------------------------------------------------------
# HTTP Backfill（断线期间遗漏数据补偿）
# ---------------------------------------------------------------------------

def _http_get_logs_with_retry(params: dict) -> list:
    """同步 HTTP get_logs，带指数退避重试。"""
    delay = 2.0
    for attempt in range(1, HTTP_RETRY_MAX + 1):
        try:
            return _w3_http.eth.get_logs(params)
        except Exception as exc:
            if "429" in str(exc):
                if attempt == HTTP_RETRY_MAX:
                    raise
                logger.warning("HTTP 限流 (429)，%.1fs 后重试 (%d/%d)...",
                               delay, attempt, HTTP_RETRY_MAX)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
            else:
                raise
    return []


async def _backfill_http(from_block: int, to_block: int) -> int:
    """
    用 HTTP get_logs 补全 [from_block, to_block] 范围内的遗漏事件。

    Parameters
    ----------
    from_block : int  起始区块（含）
    to_block   : int  结束区块（含）

    Returns
    -------
    int : 实际写入的事件总数
    """
    if from_block > to_block:
        return 0

    logger.info("[backfill] HTTP 补数据 %d → %d (%d 块)",
                from_block, to_block, to_block - from_block + 1)

    total_written = 0
    batch_start   = from_block

    while batch_start <= to_block:
        batch_end = min(batch_start + HTTP_FETCH_RANGE - 1, to_block)

        raw_logs = await asyncio.get_event_loop().run_in_executor(
            None,
            _http_get_logs_with_retry,
            {
                "address":   POOL_ADDRESSES_CHECKSUM,
                "fromBlock": hex(batch_start),
                "toBlock":   hex(batch_end),
                "topics":    [ALL_TOPICS],
            },
        )

        if raw_logs:
            written = await _write_logs_to_db(raw_logs, confirmed=True)
            total_written += written

        batch_start = batch_end + 1
        await asyncio.sleep(HTTP_FETCH_INTERVAL)

    # 更新 sync_cursor
    await asyncio.get_event_loop().run_in_executor(None, _update_cursor, to_block)

    logger.info("[backfill] 完成，共写入 %d 条事件", total_written)
    return total_written


# ---------------------------------------------------------------------------
# 事件解析 & 写库
# ---------------------------------------------------------------------------

def _parse_log(raw_log: dict) -> Optional[dict]:
    """
    将原始 log 解析为结构化事件。

    按 log["address"] 找对应合约（多池路由），
    按 topic0 路由到对应事件类型。
    返回 None 表示无法识别的事件。
    """
    addr      = raw_log.get("address") or raw_log.get("address", "")
    contract  = POOL_CONTRACTS.get(addr)
    if contract is None:
        # 尝试 checksum 化后再查找
        try:
            addr     = _w3_http.to_checksum_address(addr)
            contract = POOL_CONTRACTS.get(addr)
        except Exception:
            pass
    if contract is None:
        return None

    topics = raw_log.get("topics", [])
    if not topics:
        return None

    topic0 = ("0x" + topics[0].hex()) if isinstance(topics[0], bytes) else topics[0]

    event_map = {
        TOPIC_MINT:    contract.events.Mint,
        TOPIC_BURN:    contract.events.Burn,
        TOPIC_COLLECT: contract.events.Collect,
        TOPIC_SWAP:    contract.events.Swap,
    }

    event_cls = event_map.get(topic0)
    if event_cls is None:
        return None

    try:
        return event_cls().process_log(raw_log)
    except Exception as exc:
        logger.debug("事件解析失败: %s  log=%s", exc, raw_log)
        return None


def _rpc_fetch_timestamp(block_number: int) -> datetime:
    block = _w3_http.eth.get_block(block_number)
    return datetime.utcfromtimestamp(block["timestamp"])


def _update_cursor(last_block: int) -> None:
    """更新各池的 sync_cursor（同步，在 executor 中调用）。"""
    with get_session() as session:
        for addr in POOL_ADDRESSES_CHECKSUM:
            repo.update_sync_cursor(
                session,
                chain_id          = CHAIN_ID,
                target_type       = "pool_ws",
                target_address    = addr,
                last_synced_block = last_block,
            )


async def _write_logs_to_db(raw_logs: list[dict], confirmed: bool = True) -> int:
    """
    解析 raw_logs 并批量写入 DB。

    Returns
    -------
    int : 实际新增的事件行数
    """
    if not raw_logs:
        return 0

    events = [_parse_log(log) for log in raw_logs]
    events = [e for e in events if e is not None]
    if not events:
        return 0

    unique_blocks = {e["blockNumber"] for e in events}

    def _sync_write():
        total = 0
        with get_session() as session:
            ts_map = repo.get_or_fetch_block_timestamps(
                session,
                chain_id      = CHAIN_ID,
                block_numbers = unique_blocks,
                rpc_fetcher   = _rpc_fetch_timestamp,
            )
            for event in events:
                pool_addr = _w3_http.to_checksum_address(event["address"])
                common = {
                    "chain_id":        CHAIN_ID,
                    "pool_address":    pool_addr,
                    "block_number":    event["blockNumber"],
                    "block_timestamp": ts_map[event["blockNumber"]],
                    "tx_hash":         event["transactionHash"].hex(),
                    "log_index":       event["logIndex"],
                }
                name = event["event"]
                args = event["args"]
                ok   = False

                if name == "Mint":
                    ok = repo.insert_mint(session, {
                        **common,
                        "sender":           _w3_http.to_checksum_address(args["sender"]),
                        "owner":            _w3_http.to_checksum_address(args["owner"]),
                        "tick_lower":       args["tickLower"],
                        "tick_upper":       args["tickUpper"],
                        "amount_liquidity": args["amount"],
                        "amount0_raw":      args["amount0"],
                        "amount1_raw":      args["amount1"],
                    })
                elif name == "Burn":
                    ok = repo.insert_burn(session, {
                        **common,
                        "owner":            _w3_http.to_checksum_address(args["owner"]),
                        "tick_lower":       args["tickLower"],
                        "tick_upper":       args["tickUpper"],
                        "amount_liquidity": args["amount"],
                        "amount0_raw":      args["amount0"],
                        "amount1_raw":      args["amount1"],
                    })
                elif name == "Collect":
                    ok = repo.insert_collect(session, {
                        **common,
                        "owner":       _w3_http.to_checksum_address(args["owner"]),
                        "recipient":   _w3_http.to_checksum_address(args["recipient"]),
                        "tick_lower":  args["tickLower"],
                        "tick_upper":  args["tickUpper"],
                        "amount0_raw": args["amount0"],
                        "amount1_raw": args["amount1"],
                    })
                elif name == "Swap":
                    ok = repo.insert_swap(session, {
                        **common,
                        "sender":         _w3_http.to_checksum_address(args["sender"]),
                        "recipient":      _w3_http.to_checksum_address(args["recipient"]),
                        "amount0_raw":    args["amount0"],
                        "amount1_raw":    args["amount1"],
                        "sqrt_price_x96": args["sqrtPriceX96"],
                        "liquidity":      args["liquidity"],
                        "tick":           args["tick"],
                    })

                if ok:
                    total += 1
                    _session_counts[name] += 1
        return total

    return await asyncio.get_event_loop().run_in_executor(None, _sync_write)


# ---------------------------------------------------------------------------
# WS 监听主协程
# ---------------------------------------------------------------------------

async def _ws_listen_loop(queue: asyncio.Queue) -> None:
    """
    持续维护 WebSocket 连接，将收到的原始 log 放入 queue。

    - 建立连接后，先检查是否需要 HTTP backfill（补足断线期间的数据）
    - 订阅成功后，使用 asyncio.timeout 检测静默断线
    - 断线后指数退避重连
    """
    reconnect_delay = RECONNECT_DELAY

    while not _shutdown:
        try:
            logger.info("[ws] 正在连接 %s ...", MAINNET_WS_URL[:40] + "...")

            async with AsyncWeb3(WebSocketProvider(MAINNET_WS_URL)) as w3:
                # ── 连接成功：先做 backfill ──────────────────────────────────
                reconnect_delay = RECONNECT_DELAY   # 重置退避计时器

                latest_block = await w3.eth.block_number
                confirmed_head = latest_block - CONFIRM_BLOCKS

                # 从 DB 读取各池最小的 last_synced_block 作为 backfill 起点
                backfill_from = await asyncio.get_event_loop().run_in_executor(
                    None, _get_last_synced_block
                )
                if backfill_from is not None and backfill_from < confirmed_head:
                    await _backfill_http(backfill_from + 1, confirmed_head)

                logger.info(
                    "[ws] 连接成功，latest_block=%d，开始订阅 %d 个池子",
                    latest_block, len(POOL_ADDRESSES_CHECKSUM),
                )

                # ── 订阅事件流 ───────────────────────────────────────────────
                # web3.py 7.x: eth.subscribe() returns the subscription ID (str);
                # events are received via w3.socket.process_subscriptions().
                await w3.eth.subscribe("logs", WS_FILTER)

                async for raw_log in _iter_with_timeout(
                    w3.socket.process_subscriptions(), WS_EVENT_TIMEOUT
                ):
                    if _shutdown:
                        break
                    log_receipt = _extract_log_receipt(raw_log)
                    if log_receipt is None:
                        logger.warning("[ws] 收到无法识别的订阅消息，已跳过: %s", raw_log)
                        continue
                    await queue.put(log_receipt)   # 放入 queue，由 writer 消费

        except asyncio.TimeoutError:
            logger.warning("[ws] %ds 无事件，判定连接静默断线，准备重连...",
                           WS_EVENT_TIMEOUT)
        except Exception as exc:
            logger.error("[ws] 连接异常：%s", exc, exc_info=False)
        finally:
            if not _shutdown:
                logger.info("[ws] %.1fs 后重连...", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)


async def _iter_with_timeout(subscription, timeout: float):
    """
    为 async for 的每次迭代包裹超时检测。

    asyncio.timeout 在 Python 3.11+ 可用；
    3.10 及以下用 asyncio.wait_for 包装。
    """
    while True:
        try:
            item = await asyncio.wait_for(
                subscription.__anext__(), timeout=timeout
            )
            yield item
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise   # 向上传播给 _ws_listen_loop 处理


def _extract_log_receipt(payload) -> Optional[dict]:
    """兼容 web3.py 不同版本的订阅消息结构，提取真正的 log receipt。

    web3.py 7.x 中 process_subscriptions() 每次 yield 的结构为：
        {'subscription': '0x...', 'result': AttributeDict({address, topics, ...})}
    其中 AttributeDict 继承自 collections.abc.Mapping 而非 dict，
    因此必须用 isinstance(x, Mapping) 而非 isinstance(x, dict) 来判断。
    """
    if isinstance(payload, Mapping):
        message = dict(payload)
    else:
        try:
            message = dict(payload)
        except Exception:
            return None

    # 已经是裸 log（HTTP backfill 路径或旧版 web3 直接 yield log）
    if "address" in message and "topics" in message:
        return message

    # web3.py 7.x process_subscriptions() 格式：{'subscription': ..., 'result': AttributeDict}
    result = message.get("result")
    if isinstance(result, Mapping):
        return dict(result)

    # 完整 JSON-RPC 通知格式：{'params': {'subscription': ..., 'result': ...}}
    params = message.get("params")
    if isinstance(params, Mapping):
        nested_result = params.get("result")
        if isinstance(nested_result, Mapping):
            return dict(nested_result)

    return None


def _get_last_synced_block() -> Optional[int]:
    """
    读取所有被监听池子中 sync_cursor 的最小值，作为 backfill 起点。
    最小值保证所有池子都能被补全。
    """
    blocks: list[int] = []
    with get_session() as session:
        for addr in POOL_ADDRESSES_CHECKSUM:
            last = repo.get_sync_cursor(session, CHAIN_ID, "pool_ws", addr)
            if last is not None:
                blocks.append(last)
    return min(blocks) if blocks else None


# ---------------------------------------------------------------------------
# 事件消费协程（从 queue 读取并写 DB）
# ---------------------------------------------------------------------------

async def _event_writer(queue: asyncio.Queue) -> None:
    """
    从 queue 消费原始 log，通过 pending_buffer 实现 CONFIRM_BLOCKS 延迟写入。

    pending_buffer 中缓存 (block_number, raw_log) 二元组：
    - 收到新事件时，将 block_number ≤ latest_confirmed 的条目批量写 DB
    - 未确认的继续留在 buffer
    """
    while True:
        if _shutdown and queue.empty():
            await _flush_confirmed_buffer()
            break

        try:
            timeout = 1.0 if _shutdown else 3.0
            raw_log = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            # 定期清理已确认的 buffer，即使 queue 空闲
            await _flush_confirmed_buffer()
            continue

        block_number = raw_log.get("blockNumber", 0)
        _pending_buffer.append((block_number, raw_log))

        # 尝试写入已确认的事件
        await _flush_confirmed_buffer()
        queue.task_done()


async def _flush_confirmed_buffer() -> None:
    """
    将 pending_buffer 中 block_number ≤ (latest − CONFIRM_BLOCKS) 的事件写入 DB。
    """
    if not _pending_buffer:
        return

    try:
        latest_block   = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _w3_http.eth.block_number
        )
        confirmed_head = latest_block - CONFIRM_BLOCKS
    except Exception:
        return

    to_write: list[dict] = []
    while _pending_buffer and _pending_buffer[0][0] <= confirmed_head:
        _, raw_log = _pending_buffer.popleft()
        to_write.append(raw_log)

    if not to_write:
        return

    written = await _write_logs_to_db(to_write, confirmed=True)
    max_block = max(log.get("blockNumber", 0) for log in to_write)
    await asyncio.get_event_loop().run_in_executor(None, _update_cursor, max_block)

    now = datetime.utcnow().strftime("%H:%M:%S")
    stats = " ".join(f"{k}={v}" for k, v in _session_counts.items() if v) or "无新增"
    logger.info(
        "[writer] %s  处理 %d 条，新增 %d 条  (%s)",
        now, len(to_write), written, stats,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def main() -> None:
    if not MAINNET_WS_URL:
        logger.error("未配置 MAINNET_WS_URL，请在 .env 中添加 WebSocket 端点后重试。")
        sys.exit(1)

    # 初始化 DB
    init_db()
    logger.info("数据库就绪")
    logger.info("监听池子：%d 个", len(POOL_ADDRESSES_CHECKSUM))
    for addr in POOL_ADDRESSES_CHECKSUM:
        logger.info("  %s", addr)
    logger.info("确认块数：%d", CONFIRM_BLOCKS)
    logger.info("WS 超时：%ds", WS_EVENT_TIMEOUT)
    logger.info("按 Ctrl+C 优雅退出")
    logger.info("─" * 60)

    queue: asyncio.Queue = asyncio.Queue()

    # 并发运行 WS 监听 + 事件写入两个协程
    listener_task = asyncio.create_task(_ws_listen_loop(queue), name="ws_listener")
    writer_task   = asyncio.create_task(_event_writer(queue),   name="event_writer")

    try:
        await asyncio.gather(listener_task, writer_task)
    finally:
        listener_task.cancel()
        await asyncio.gather(listener_task, return_exceptions=True)

        try:
            await asyncio.wait_for(writer_task, timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("[writer] 等待队列排空超时，强制停止")
            writer_task.cancel()
        await asyncio.gather(writer_task, return_exceptions=True)

        logger.info("─" * 60)
        logger.info("监听已停止，本次会话写入统计：")
        for event_type, count in _session_counts.items():
            logger.info("  %-10s: %d 条", event_type, count)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        global _MAIN_TASK
        _MAIN_TASK = asyncio.current_task()
        await main()

    _setup_signal_handlers(loop)
    try:
        loop.run_until_complete(_run())
    except asyncio.CancelledError:
        pass  # 优雅退出，忽略取消异常
    finally:
        loop.close()
