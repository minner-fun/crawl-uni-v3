# crawl-uni-v3

以太坊 Uniswap V3 全栈 LP 管理系统。覆盖从链上数据采集、指标聚合、回溯测试，到策略执行、仓位管理、Telegram 实时通知的完整闭环。

---

## 功能模块总览

| 模块 | 目录 | 核心职责 |
|------|------|---------|
| 数据采集层 | `src/data_collector/` | 历史事件爬取（HTTP）+ 实时事件订阅（WebSocket） |
| 数据引擎层 | `src/data_engine/` | 价格快照、小时/日聚合指标计算 |
| 数据库层 | `src/db/` | SQLAlchemy 模型 + PostgreSQL 存储 |
| 执行引擎层 | `src/execution_engine/` | 链上 mint / collect / burn 等写操作 |
| 策略引擎层 | `src/strategy_engine/` | 策略抽象 + 上下文构建 + 执行调度 |
| 回溯测试层 | `src/backtesting_engine/` | 历史数据驱动的策略性能评估 |
| 通知引擎层 | `src/notification_engine/` | 仓位变动 Telegram 消息推送 |

---

## 项目结构

```
crawl-uni-v3/
├── .env                              # 私钥 / RPC URL / 通知配置（不提交 git）
├── .env-example                      # 配置项模板
├── requirements.txt                  # Python 依赖
├── src/
│   ├── Constracts.py                 # 合约地址、ABI、RPC URL 统一出口
│   ├── main.py                       # 生产入口：启动策略执行循环
│   │
│   ├── data_collector/               # 数据采集层
│   │   ├── crawl_factory.py          # 历史采集：PoolCreated 事件
│   │   ├── crawl_pools.py            # 历史采集：Mint/Burn/Collect/Swap
│   │   └── ws_pool_listener.py       # 实时采集：WebSocket 事件订阅
│   │
│   ├── data_engine/                  # 数据引擎层
│   │   ├── price_snapshot.py         # 从 Swap 事件构建价格快照
│   │   ├── hourly_metrics.py         # 聚合小时指标（量、TVL、fee APR）
│   │   ├── daily_metrics.py          # 聚合日指标
│   │   ├── utils.py                  # 公共工具函数
│   │   └── run.py                    # 手动触发指标构建的脚本
│   │
│   ├── db/                           # 数据库层
│   │   ├── models.py                 # SQLAlchemy 表模型
│   │   ├── repository.py             # CRUD 数据访问层
│   │   └── database.py               # Session / Engine 管理
│   │
│   ├── execution_engine/             # 执行引擎层
│   │   └── position_manager.py       # NonfungiblePositionManager 封装
│   │
│   ├── strategy_engine/              # 策略引擎层
│   │   ├── base.py                   # BaseStrategy 抽象类 + Decision 数据类
│   │   ├── context.py                # MarketContext 构建
│   │   ├── runner.py                 # StrategyRunner 执行调度
│   │   └── strategies/
│   │       └── volume_rebalance.py   # VolumeRebalanceStrategy 实现
│   │
│   ├── backtesting_engine/           # 回溯测试层
│   │   ├── data_loader.py            # 加载历史小时/日数据
│   │   ├── position.py               # V3 仓位数学模型（含完整 IL 推导）
│   │   ├── simulator.py              # 逐小时回溯模拟循环
│   │   ├── metrics.py                # 收益、风险指标计算
│   │   ├── report.py                 # 终端报告 + DataFrame 导出
│   │   └── run_backtesting.py        # 快速运行回测的入口脚本
│   │
│   └── notification_engine/          # 通知引擎层
│       └── telegram.py               # Telegram Bot 消息推送
└── README.md
```

---

## 快速开始

### 1. 克隆并安装依赖

```bash
git clone <repo-url>
cd crawl-uni-v3

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置环境变量

复制模板后填入实际值：

```bash
cp .env-example .env
```

```env
# Alchemy / Infura HTTP 节点
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY

# WebSocket 节点（实时监听用）
MAINNET_WS_URL=wss://eth-mainnet.g.alchemy.com/v2/YOUR_KEY

# PostgreSQL
DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/v3info

# 执行账户私钥（链上写操作）
EXECUTOR_PRIVATE_KEY=0x...

# Telegram 通知
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

> **获取 Telegram Chat ID**：向 Bot 发送任意消息后，访问  
> `https://api.telegram.org/bot{TOKEN}/getUpdates`，在返回 JSON 中找 `message.chat.id`。

### 3. 初始化数据库

```python
from src.db.database import init_db
init_db()   # 自动创建所有表
```

---

## 数据采集层

### 历史事件采集（HTTP）

```bash
# 采集 Factory 的 PoolCreated 事件（发现新池子）
python src/data_collector/crawl_factory.py

# 采集指定池子的 Mint / Burn / Collect / Swap 事件
python src/data_collector/crawl_pools.py
```

两个脚本内均可修改 `FROM_BLOCK` / `TO_BLOCK` 指定采集范围。采集结果幂等写入 PostgreSQL（`ON CONFLICT DO NOTHING`）。

> **注意**：Alchemy 免费套餐 `eth_getLogs` 单次最多查询 **10 个区块**，脚本已内置自动分批逻辑。

### 实时事件监听（WebSocket）

```bash
python src/data_collector/ws_pool_listener.py
```

| 特性 | 说明 |
|------|------|
| 多池并发订阅 | 单 WS 连接同时监听多个池子地址 |
| 断线自动重连 | 指数退避重试，静默断连通过 `asyncio.timeout` 心跳检测 |
| HTTP 补漏 | 重连后自动用 `eth_getLogs` 回填断连期间错过的区块 |
| 防 Reorg 写入 | `CONFIRM_BLOCKS=2` 待确认缓冲区，降低因链重组产生脏数据的风险 |
| 异步解耦 | WS 接收与 DB 写入通过 `asyncio.Queue` 分离，不互相阻塞 |

---

## 数据引擎层

从原始链上事件聚合为可供策略使用的结构化指标：

```python
from src.data_engine import price_snapshot, hourly_metrics, daily_metrics

with get_session() as session:
    # 1. 价格快照（每块最后一笔 Swap → close price）
    price_snapshot.build_price_snapshots(session, pool_address, decimals0=6, decimals1=18)

    # 2. 小时指标（Volume、TVL、fee APR、tick 分布）
    hourly_metrics.build_hourly_metrics(session, pool_address, fee=500, ...)

    # 3. 日指标（依赖小时指标聚合）
    daily_metrics.build_daily_metrics(session, pool_address, ...)
```

或直接运行预置脚本：

```bash
python src/data_engine/run.py
```

---

## 数据库层

### 核心表结构

| 表名 | 说明 |
|------|------|
| `blocks` | 区块时间戳缓存（避免重复 RPC 查询） |
| `pools` | 已发现的 Uniswap V3 池子基础信息 |
| `pool_events` | Mint / Burn / Collect / Swap 原始事件 |
| `pool_price_snapshots` | 每区块收盘价快照 |
| `pool_metrics_hourly` | 小时聚合指标（Volume、TVL、fee APR 等） |
| `pool_metrics_daily` | 日聚合指标 |
| `lp_positions` | 策略持有的 LP 仓位状态 |
| `lp_position_actions` | 仓位操作历史（OPEN / REBALANCE / CLOSE 等） |
| `strategy_signals` | 每次策略决策信号记录 |

---

## 执行引擎层

封装 Uniswap V3 `NonfungiblePositionManager` 合约，提供 Python 级别的仓位管理接口：

```python
from src.execution_engine import build_position_manager, MintParams

pm = build_position_manager()

# 查询仓位信息
pos = pm.get_position(token_id=123456)

# 开仓
result = pm.mint(MintParams(
    token0=USDC, token1=WETH, fee=500,
    tick_lower=-887220, tick_upper=887220,
    amount0_desired=200_000_000,   # 200 USDC（6 位精度）
    amount1_desired=...,
))

# 一键平仓（reduce → collect → burn）
close = pm.close_position(token_id=result.token_id)
```

内置自动 ERC20 授权（`_ensure_allowance`）、Gas 估算、私钥签名和广播。

---

## 策略引擎层

### 内置策略：VolumeRebalanceStrategy（USDC/ETH）

| 参数 | 值 |
|------|----|
| 开仓条件 | 3 日均 Volume/TVL ≥ 2.0 |
| 退出条件 | 3 日均 Volume/TVL < 0.5 |
| LP 价格区间 | 当前价格 ±5%（≈ ±490 ticks） |
| 再平衡触发 | 当前 tick 距区间边界 < 20% 范围宽度 |
| 初始投入 | 固定 200 USDC |

### 自定义策略

继承 `BaseStrategy`，实现 `evaluate()` 方法：

```python
from src.strategy_engine.base import BaseStrategy, Decision, StrategyDecision
from src.strategy_engine.context import MarketContext, ActivePosition

class MyStrategy(BaseStrategy):
    def evaluate(self, ctx: MarketContext, position: ActivePosition | None) -> Decision:
        if ctx.avg_volume_tvl_ratio >= 3.0:
            return Decision(
                action          = StrategyDecision.OPEN,
                reason          = "VTV spike",
                tick_lower      = ...,
                tick_upper      = ...,
                amount0_desired = ...,
                amount1_desired = ...,
            )
        return Decision(action=StrategyDecision.HOLD, reason="waiting")
```

### 执行循环

```python
from src.execution_engine import build_position_manager
from src.strategy_engine import StrategyRunner, PoolConfig
from src.strategy_engine.strategies import VolumeRebalanceStrategy
from src.notification_engine import build_notifier

runner = StrategyRunner(
    strategy         = VolumeRebalanceStrategy(),
    position_manager = build_position_manager(),
    pool_config      = PoolConfig(pool_address="0x88e6A0c2..."),
    notifier         = build_notifier(pool_label="USDC/ETH 0.05%"),
)

runner.run_loop(interval_secs=3600)   # 每小时执行一次
```

`run_once()` 的执行顺序：上下文构建 → 策略评估 → 链上执行 → DB 持久化 → 通知推送。

---

## 回溯测试层

使用数据库中的历史小时数据，逐小时回放策略逻辑，计算完整的收益与风险指标。

```python
from datetime import datetime
from src.backtesting_engine import BacktestSimulator, BacktestConfig
from src.strategy_engine.strategies import VolumeRebalanceStrategy

result = BacktestSimulator(
    strategy = VolumeRebalanceStrategy(),
    config   = BacktestConfig(
        pool_address   = "0x88e6A0c2...",
        from_dt        = datetime(2024, 1, 1),
        to_dt          = datetime(2024, 12, 31),
        initial_capital_usdc = 1000.0,    # 初始资金（USDC）
        gas_cost_usdc        = 5.0,       # 每次操作 Gas 成本估算
    ),
).run()

result.print_report()        # 终端打印报告
df = result.to_dataframe()   # 导出 pandas DataFrame 做可视化
```

### 输出指标

| 类别 | 指标 |
|------|------|
| 收益 | 总收益率、HODL 收益率、Alpha（超额收益） |
| 费率 | 毛 Fee APR、净 APR（扣除 Gas + IL） |
| 风险 | 最大回撤、夏普比率、索提诺比率、日收益波动率 |
| 操作 | 在区间时长占比、再平衡次数、平均持仓小时数 |
| 拆分 | 累计手续费收入、IL 总损耗、Gas 总支出 |

V3 无常损失采用完整数学推导，包含 `sqrt(price)` 流动性分配模型。

---

## 通知引擎层

所有仓位操作完成后自动推送 Telegram 消息，发送失败只记录日志，不阻断主流程。

| 通知类型 | 触发时机 | 消息内容 |
|---------|---------|---------|
| 🟢 新仓位开启 | OPEN 执行成功 | Token ID、价格区间、投入金额、Tx Hash |
| 🔄 仓位再平衡 | REBALANCE 完成 | 新旧 Token ID、区间变化、收回/重投金额 |
| 🔴 仓位关闭 | CLOSE 执行成功 | 收回金额、关仓原因 |
| 💤 策略 HOLD | `send_hold=True` 时 | 均值 VTV、HOLD 原因 |
| ⚠️ 执行异常 | 链上操作失败 | 操作类型、错误信息 |

单独测试连通性：

```python
from src.notification_engine import build_notifier

notifier = build_notifier(pool_label="USDC/ETH 0.05%")
notifier.test_connection()   # 发送一条测试消息
```

---

## 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `MAINNET_RPC_URL` | ✅ | Alchemy / Infura HTTP RPC 地址 |
| `MAINNET_WS_URL` | 实时监听时必填 | WebSocket RPC 地址（`wss://`） |
| `DATABASE_URL` | ✅ | PostgreSQL 连接字符串 |
| `EXECUTOR_PRIVATE_KEY` | 链上写操作时必填 | 执行账户私钥（`0x...`） |
| `TELEGRAM_BOT_TOKEN` | 通知时必填 | BotFather 颁发的 Bot Token |
| `TELEGRAM_CHAT_ID` | 通知时必填 | 接收消息的 Chat ID |

---

## 主要依赖

| 库 | 用途 |
|----|------|
| `web3` | 以太坊节点交互（HTTP + WebSocket） |
| `sqlalchemy` | ORM 与 PostgreSQL 交互 |
| `psycopg2-binary` | PostgreSQL 驱动 |
| `python-dotenv` | 从 `.env` 读取配置 |
| `requests` | Telegram Bot API HTTP 请求 |
| `pandas` | 回测结果 DataFrame 导出（可选） |
