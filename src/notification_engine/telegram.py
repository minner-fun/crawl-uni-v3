"""
TelegramNotifier
================
通过 Telegram Bot API 发送仓位操作通知。

支持的通知类型：
    notify_open       : 🟢 新仓位开启
    notify_rebalance  : 🔄 仓位再平衡（关旧开新）
    notify_close      : 🔴 仓位关闭
    notify_hold       : 💤 HOLD 信号（默认关闭，可按需开启）
    notify_error      : ⚠️ 链上执行异常

设计原则：
    - 所有 notify_* 方法均为 fire-and-forget，发送失败只记录日志，不抛异常
    - 使用 HTML 格式（比 MarkdownV2 转义简单）
    - 工厂函数 build_from_env() 从环境变量自动初始化

环境变量：
    TELEGRAM_BOT_TOKEN : BotFather 颁发的 token
    TELEGRAM_CHAT_ID   : 接收消息的 chat_id（个人或频道）

获取 chat_id 方法：
    向 bot 发送任意消息后，访问
    https://api.telegram.org/bot{TOKEN}/getUpdates
    在返回 JSON 中找 message.chat.id。
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10   # HTTP 请求超时（秒）


# ---------------------------------------------------------------------------
# 辅助：tick → ETH 价格（专为 USDC/ETH 池）
# ---------------------------------------------------------------------------

def _tick_to_token1_price(tick: int, decimals0: int = 6, decimals1: int = 18) -> float:
    """
    tick → token1 的人类可读价格（1 token1 = X token0）。

    对于 USDC(d0=6)/WETH(d1=18)：
        price_raw  = 1.0001^tick     （WETH_raw / USDC_raw）
        eth_price  = 10^(d1-d0) / price_raw   （USDC per WETH）
    """
    price_raw = 1.0001 ** tick
    if price_raw <= 0:
        return 0.0
    return (10 ** (decimals1 - decimals0)) / price_raw


def _fmt_price(price: float) -> str:
    """格式化美元价格，自动选择合适精度。"""
    if price >= 10_000:
        return f"${price:,.0f}"
    if price >= 100:
        return f"${price:,.1f}"
    if price >= 1:
        return f"${price:,.2f}"
    return f"${price:.6f}"


def _fmt_amount(amount_raw: int, decimals: int) -> str:
    """raw 金额 → 人类可读字符串。"""
    human = amount_raw / (10 ** decimals)
    if decimals == 6:    # USDC
        return f"{human:,.2f} USDC"
    if decimals == 18:   # ETH/WBTC
        return f"{human:.6f} ETH"
    return f"{human:.6f}"


def _short_tx(tx_hash: str) -> str:
    """0x1234...5678 样式短哈希。"""
    return f"{tx_hash[:6]}...{tx_hash[-4:]}" if len(tx_hash) > 10 else tx_hash


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# TelegramNotifier
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """
    Telegram 通知发送器。

    Parameters
    ----------
    token   : Telegram Bot Token
    chat_id : 接收消息的 chat_id
    pool_label : 可选的池子名称标签（如 "USDC/ETH 0.05%"），用于消息标题
    decimals0  : token0 小数位（默认 6，USDC）
    decimals1  : token1 小数位（默认 18，WETH）
    send_hold  : 是否发送 HOLD 信号通知（默认 False，避免消息轰炸）
    """

    def __init__(
        self,
        token:       str,
        chat_id:     str,
        pool_label:  str  = "Uniswap V3",
        decimals0:   int  = 6,
        decimals1:   int  = 18,
        send_hold:   bool = False,
    ) -> None:
        self._token      = token
        self._chat_id    = chat_id
        self._pool_label = pool_label
        self._d0         = decimals0
        self._d1         = decimals1
        self._send_hold  = send_hold
        self._url        = _TELEGRAM_API.format(token=token)

    # ------------------------------------------------------------------
    # 公共 notify_* 接口
    # ------------------------------------------------------------------

    def notify_open(
        self,
        token_id:    int,
        tick_lower:  int,
        tick_upper:  int,
        amount0:     int,    # raw
        amount1:     int,    # raw
        liquidity:   int,
        tx_hash:     str,
        reason:      str = "",
    ) -> None:
        """🟢 新仓位开启通知。"""
        price_lower = _tick_to_token1_price(tick_upper, self._d0, self._d1)
        price_upper = _tick_to_token1_price(tick_lower, self._d0, self._d1)

        lines = [
            "🟢 <b>新仓位开启</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"<b>Pool</b>: {self._pool_label}",
            f"<b>Token ID</b>: #{token_id}",
            f"<b>价格区间</b>: {_fmt_price(price_lower)} → {_fmt_price(price_upper)}",
            f"<b>投入</b>: {_fmt_amount(amount0, self._d0)} + {_fmt_amount(amount1, self._d1)}",
            f"<b>流动性</b>: {liquidity:,}",
            f"<b>Tx</b>: <code>{_short_tx(tx_hash)}</code>",
        ]
        if reason:
            lines.insert(3, f"<b>原因</b>: {reason}")
        lines.append(f"<b>时间</b>: {_now_utc()}")

        self._send("\n".join(lines))

    def notify_rebalance(
        self,
        old_token_id:   int,
        new_token_id:   int,
        old_tick_lower: int,
        old_tick_upper: int,
        new_tick_lower: int,
        new_tick_upper: int,
        collect_amount0: int,   # raw，收回的 token0
        collect_amount1: int,   # raw，收回的 token1
        new_amount0:     int,
        new_amount1:     int,
        burn_tx:         str,
        mint_tx:         str,
        reason:          str = "",
    ) -> None:
        """🔄 仓位再平衡通知（关旧 → 开新）。"""
        old_lo = _tick_to_token1_price(old_tick_upper, self._d0, self._d1)
        old_hi = _tick_to_token1_price(old_tick_lower, self._d0, self._d1)
        new_lo = _tick_to_token1_price(new_tick_upper, self._d0, self._d1)
        new_hi = _tick_to_token1_price(new_tick_lower, self._d0, self._d1)

        lines = [
            "🔄 <b>仓位再平衡</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"<b>Pool</b>: {self._pool_label}",
        ]
        if reason:
            lines.append(f"<b>原因</b>: {reason}")
        lines += [
            f"<b>旧仓位</b>: #{old_token_id}  "
            f"[{_fmt_price(old_lo)} → {_fmt_price(old_hi)}]",
            f"<b>新仓位</b>: #{new_token_id}  "
            f"[{_fmt_price(new_lo)} → {_fmt_price(new_hi)}]",
            f"<b>收回</b>: {_fmt_amount(collect_amount0, self._d0)} "
            f"+ {_fmt_amount(collect_amount1, self._d1)}",
            f"<b>重投</b>: {_fmt_amount(new_amount0, self._d0)} "
            f"+ {_fmt_amount(new_amount1, self._d1)}",
            f"<b>关仓 Tx</b>: <code>{_short_tx(burn_tx)}</code>",
            f"<b>开仓 Tx</b>: <code>{_short_tx(mint_tx)}</code>",
            f"<b>时间</b>: {_now_utc()}",
        ]

        self._send("\n".join(lines))

    def notify_close(
        self,
        token_id:        int,
        tick_lower:      int,
        tick_upper:      int,
        collect_amount0: int,   # raw
        collect_amount1: int,   # raw
        burn_tx:         str,
        reason:          str = "",
    ) -> None:
        """🔴 仓位关闭通知。"""
        price_lo = _tick_to_token1_price(tick_upper, self._d0, self._d1)
        price_hi = _tick_to_token1_price(tick_lower, self._d0, self._d1)

        lines = [
            "🔴 <b>仓位关闭</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"<b>Pool</b>: {self._pool_label}",
            f"<b>Token ID</b>: #{token_id}",
            f"<b>原区间</b>: {_fmt_price(price_lo)} → {_fmt_price(price_hi)}",
        ]
        if reason:
            lines.append(f"<b>原因</b>: {reason}")
        lines += [
            f"<b>收回</b>: {_fmt_amount(collect_amount0, self._d0)} "
            f"+ {_fmt_amount(collect_amount1, self._d1)}",
            f"<b>Tx</b>: <code>{_short_tx(burn_tx)}</code>",
            f"<b>时间</b>: {_now_utc()}",
        ]

        self._send("\n".join(lines))

    def notify_hold(self, reason: str, avg_vtv: Optional[float] = None) -> None:
        """
        💤 HOLD 信号通知（默认关闭，send_hold=True 时才发送）。
        适合调试阶段，生产环境建议关闭避免频繁打扰。
        """
        if not self._send_hold:
            return

        vtv_str = f"{avg_vtv:.3f}" if avg_vtv is not None else "N/A"
        lines = [
            "💤 <b>策略 HOLD</b>",
            f"<b>Pool</b>: {self._pool_label}",
            f"<b>原因</b>: {reason}",
            f"<b>avg VTV</b>: {vtv_str}",
            f"<b>时间</b>: {_now_utc()}",
        ]
        self._send("\n".join(lines))

    def notify_error(
        self,
        action:    str,
        error_msg: str,
        extra:     str = "",
    ) -> None:
        """⚠️ 链上执行异常通知。"""
        lines = [
            "⚠️ <b>执行异常</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"<b>Pool</b>: {self._pool_label}",
            f"<b>操作</b>: {action}",
            f"<b>错误</b>: {error_msg[:200]}",   # 截断超长错误信息
        ]
        if extra:
            lines.append(f"<b>详情</b>: {extra[:200]}")
        lines.append(f"<b>时间</b>: {_now_utc()}")

        self._send("\n".join(lines))

    # ------------------------------------------------------------------
    # 底层发送
    # ------------------------------------------------------------------

    def _send(self, text: str) -> bool:
        """
        发送 HTML 格式消息到 Telegram。
        失败时记录日志但不抛异常（fire-and-forget）。

        Returns
        -------
        bool : True 表示发送成功
        """
        try:
            resp = requests.post(
                self._url,
                json={
                    "chat_id":    self._chat_id,
                    "text":       text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=_TIMEOUT,
            )
            if not resp.ok:
                logger.warning(
                    "[telegram] 发送失败 status=%d  body=%s",
                    resp.status_code, resp.text[:200],
                )
                return False
            return True
        except Exception as exc:
            logger.warning("[telegram] 发送异常：%s", exc)
            return False

    def test_connection(self) -> bool:
        """
        发送一条测试消息，验证 token 和 chat_id 配置是否正确。
        返回 True 表示配置有效。
        """
        ok = self._send(
            f"✅ <b>Notification Engine 连接测试</b>\n"
            f"Pool: {self._pool_label}\n"
            f"时间: {_now_utc()}"
        )
        if ok:
            logger.info("[telegram] 连接测试成功")
        else:
            logger.error("[telegram] 连接测试失败，请检查 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return ok


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def build_from_env(
    pool_label: str = "Uniswap V3",
    decimals0:  int = 6,
    decimals1:  int = 18,
    send_hold:  bool = False,
) -> Optional[TelegramNotifier]:
    """
    从环境变量构建 TelegramNotifier。

    若 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置，返回 None
    （StrategyRunner 会跳过通知，不影响主流程）。
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat_id:
        logger.info("[telegram] 未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，通知功能关闭")
        return None

    return TelegramNotifier(
        token      = token,
        chat_id    = chat_id,
        pool_label = pool_label,
        decimals0  = decimals0,
        decimals1  = decimals1,
        send_hold  = send_hold,
    )
