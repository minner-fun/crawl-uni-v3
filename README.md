# crawl-uni-v3

以太坊 Uniswap V3 链上事件数据采集工具，基于 Web3.py 和 Alchemy RPC，支持采集 Factory 合约的池子创建事件，以及具体池子合约的流动性、交易等核心事件。

---

## 功能概览

| 脚本 | 采集内容 |
|---|---|
| `src/spider/crawl_factory.py` | Factory 合约的 `PoolCreated` 事件（链上新池子） |
| `src/spider/crawl_pools.py` | 指定池子的 `Mint` / `Burn` / `Collect` / `Swap` 四类事件 |

---

## 项目结构

```
crawl-uni-v3/
├── .env                        # 私钥/RPC URL 配置（不提交 git）
├── .gitignore
├── requestments.txt            # Python 依赖列表
├── src/
│   ├── Constracts.py           # 合约地址、ABI、配置统一出口
│   └── spider/
│       ├── crawl_factory.py    # 采集 PoolCreated 事件
│       └── crawl_pools.py      # 采集 Mint/Burn/Collect/Swap 事件
└── README.md
```

---

## 快速开始

### 1. 克隆项目

```bash
git clone <repo-url>
cd crawl-uni-v3
```

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requestments.txt
```

### 3. 配置 RPC URL

在项目根目录创建 `.env` 文件：

```env
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<YOUR_API_KEY>
```

> 推荐使用 [Alchemy](https://www.alchemy.com/) 获取免费 API Key。  
> **注意：** 免费套餐下 `eth_getLogs` 单次最多查询 **10 个区块**，脚本已内置自动分批逻辑。

### 4. 运行采集脚本

在**项目根目录**执行：

```bash
# 采集 PoolCreated 事件
python src/spider/crawl_factory.py

# 采集指定池子的 Mint/Burn/Collect/Swap 事件
python src/spider/crawl_pools.py
```

---

## 配置说明

### 修改采集区块范围

在对应脚本中修改以下两个变量：

```python
FROM_BLOCK = 24625757   # 起始区块
TO_BLOCK   = 24625772   # 结束区块
```

### 修改目标合约

`src/Constracts.py` 中维护了所有合约地址和 ABI：

| 变量 | 说明 |
|---|---|
| `UNISWAP_V3_FACTORY_ADDRESS` | Uniswap V3 Factory 合约地址（以太坊主网） |
| `UNISWAP_V3_USDC_ETH_POOL_ADDRESS` | USDC/ETH 0.05% 池子地址（默认采集目标） |
| `UNISWAP_V3_FACTORY_ABI` | Factory 合约 ABI |
| `POOLS_ABI` | Pool 合约 ABI（含 Mint/Burn/Collect/Swap 等事件） |

替换 `UNISWAP_V3_USDC_ETH_POOL_ADDRESS` 即可采集任意 Uniswap V3 池子。

---

## 采集事件字段说明

### PoolCreated

| 字段 | 类型 | 说明 |
|---|---|---|
| `token0` | address | 池子 token0 地址 |
| `token1` | address | 池子 token1 地址 |
| `fee` | uint24 | 手续费率（单位 bps×100，如 500=0.05%） |
| `tickSpacing` | int24 | tick 间距 |
| `pool` | address | 新创建的池子合约地址 |

### Mint（添加流动性）

| 字段 | 类型 | 说明 |
|---|---|---|
| `sender` | address | 调用 mint 的地址 |
| `owner` | address | 头寸归属地址 |
| `tickLower` | int24 | 价格区间下界 |
| `tickUpper` | int24 | 价格区间上界 |
| `amount` | uint128 | 流动性数量 |
| `amount0` | uint256 | 实际注入的 token0 数量 |
| `amount1` | uint256 | 实际注入的 token1 数量 |

### Burn（移除流动性）

| 字段 | 类型 | 说明 |
|---|---|---|
| `owner` | address | 头寸归属地址 |
| `tickLower` | int24 | 价格区间下界 |
| `tickUpper` | int24 | 价格区间上界 |
| `amount` | uint128 | 移除的流动性数量 |
| `amount0` | uint256 | 取回的 token0 数量 |
| `amount1` | uint256 | 取回的 token1 数量 |

### Collect（提取手续费）

| 字段 | 类型 | 说明 |
|---|---|---|
| `owner` | address | 头寸归属地址 |
| `recipient` | address | 手续费接收地址 |
| `tickLower` | int24 | 价格区间下界 |
| `tickUpper` | int24 | 价格区间上界 |
| `amount0` | uint128 | 提取的 token0 手续费 |
| `amount1` | uint128 | 提取的 token1 手续费 |

### Swap（交易）

| 字段 | 类型 | 说明 |
|---|---|---|
| `sender` | address | 发起交易的地址 |
| `recipient` | address | 接收代币的地址 |
| `amount0` | int256 | token0 变化量（正=流入池子，负=流出） |
| `amount1` | int256 | token1 变化量（正=流入池子，负=流出） |
| `sqrtPriceX96` | uint160 | 交易后的价格（Q64.96 格式） |
| `liquidity` | uint128 | 交易后的当前流动性 |
| `tick` | int24 | 交易后的当前 tick |

---

## 主要依赖

| 库 | 版本 | 用途 |
|---|---|---|
| `web3` | 7.x | 以太坊节点交互 |
| `python-dotenv` | 1.x | 从 `.env` 读取配置 |
| `requests` | 2.x | HTTP 请求 |
