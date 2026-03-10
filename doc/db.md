1. tokens
作用：存储 token 基础信息，避免每次都查链上。
token_address：token 合约地址
symbol：比如 ETH、USDC
name：token 名称
decimals：精度
chain_id：后面扩多链时有用
```
CREATE TABLE tokens (
    token_address      VARCHAR(42) PRIMARY KEY,
    symbol             VARCHAR(32),
    name               VARCHAR(128),
    decimals           INTEGER NOT NULL,
    chain_id           INTEGER NOT NULL DEFAULT 1,
    created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMP NOT NULL DEFAULT NOW()
);
```

2. pools
作用：存储 Uniswap V3 所有池子的基础信息。
```
CREATE TABLE pools (
    pool_address        VARCHAR(42) PRIMARY KEY,
    chain_id            INTEGER NOT NULL DEFAULT 1,
    token0_address      VARCHAR(42) NOT NULL,
    token1_address      VARCHAR(42) NOT NULL,
    fee                 INTEGER NOT NULL,
    tick_spacing        INTEGER NOT NULL,
    created_block       BIGINT NOT NULL,
    created_tx_hash     VARCHAR(66) NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_pools_token0 FOREIGN KEY (token0_address) REFERENCES tokens(token_address),
    CONSTRAINT fk_pools_token1 FOREIGN KEY (token1_address) REFERENCES tokens(token_address)
);
```
索引
```
CREATE INDEX idx_pools_token0 ON pools(token0_address);
CREATE INDEX idx_pools_token1 ON pools(token1_address);
CREATE INDEX idx_pools_fee ON pools(fee);
CREATE INDEX idx_pools_token_pair_fee ON pools(token0_address, token1_address, fee);
```

三、第二层：原始事件表

这一层非常重要。
设计原则：尽量保留链上原始信息，不要过早“加工丢失细节”
每张事件表都建议保留：
block_number
tx_hash
log_index
并且用：
```
(tx_hash, log_index)
```
做唯一约束。


3. swaps

作用：

存储每笔 Swap 事件。

```
CREATE TABLE swaps (
    id                  BIGSERIAL PRIMARY KEY,
    chain_id            INTEGER NOT NULL DEFAULT 1,
    pool_address        VARCHAR(42) NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMP NOT NULL,
    tx_hash             VARCHAR(66) NOT NULL,
    log_index           INTEGER NOT NULL,

    sender              VARCHAR(42) NOT NULL,
    recipient           VARCHAR(42) NOT NULL,

    amount0_raw         NUMERIC(78, 0) NOT NULL,
    amount1_raw         NUMERIC(78, 0) NOT NULL,

    sqrt_price_x96      NUMERIC(78, 0) NOT NULL,
    liquidity           NUMERIC(78, 0) NOT NULL,
    tick                INTEGER NOT NULL,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_swaps_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address),
    CONSTRAINT uq_swaps_tx_log UNIQUE (tx_hash, log_index)
);
```
为什么 amount 用 NUMERIC(78,0)
因为链上数值很大，尤其是 uint256，不要用 bigint。
建议索引
```
CREATE INDEX idx_swaps_pool_address ON swaps(pool_address);
CREATE INDEX idx_swaps_block_timestamp ON swaps(block_timestamp);
CREATE INDEX idx_swaps_pool_time ON swaps(pool_address, block_timestamp);
CREATE INDEX idx_swaps_block_number ON swaps(block_number);
```

4. mints

作用：记录新增流动性事件。

```
CREATE TABLE mints (
    id                  BIGSERIAL PRIMARY KEY,
    chain_id            INTEGER NOT NULL DEFAULT 1,
    pool_address        VARCHAR(42) NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMP NOT NULL,
    tx_hash             VARCHAR(66) NOT NULL,
    log_index           INTEGER NOT NULL,

    sender              VARCHAR(42) NOT NULL,
    owner               VARCHAR(42) NOT NULL,
    tick_lower          INTEGER NOT NULL,
    tick_upper          INTEGER NOT NULL,

    amount_liquidity    NUMERIC(78, 0) NOT NULL,
    amount0_raw         NUMERIC(78, 0) NOT NULL,
    amount1_raw         NUMERIC(78, 0) NOT NULL,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_mints_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address),
    CONSTRAINT uq_mints_tx_log UNIQUE (tx_hash, log_index)
);
```
索引
```
CREATE INDEX idx_mints_pool_address ON mints(pool_address);
CREATE INDEX idx_mints_block_timestamp ON mints(block_timestamp);
CREATE INDEX idx_mints_pool_time ON mints(pool_address, block_timestamp);
CREATE INDEX idx_mints_owner ON mints(owner);
```


5. burns

作用：

记录移除流动性事件。
```
CREATE TABLE burns (
    id                  BIGSERIAL PRIMARY KEY,
    chain_id            INTEGER NOT NULL DEFAULT 1,
    pool_address        VARCHAR(42) NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMP NOT NULL,
    tx_hash             VARCHAR(66) NOT NULL,
    log_index           INTEGER NOT NULL,

    owner               VARCHAR(42),
    tick_lower          INTEGER NOT NULL,
    tick_upper          INTEGER NOT NULL,

    amount_liquidity    NUMERIC(78, 0) NOT NULL,
    amount0_raw         NUMERIC(78, 0) NOT NULL,
    amount1_raw         NUMERIC(78, 0) NOT NULL,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_burns_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address),
    CONSTRAINT uq_burns_tx_log UNIQUE (tx_hash, log_index)
);
```
索引
```
CREATE INDEX idx_burns_pool_address ON burns(pool_address);
CREATE INDEX idx_burns_block_timestamp ON burns(block_timestamp);
CREATE INDEX idx_burns_pool_time ON burns(pool_address, block_timestamp);
```

6. collects

作用：

记录手续费领取事件。

这个表我建议你第一版就加上。
因为后面你想分析“真实 LP 收益”，没有 collects 会缺一块。

```
CREATE TABLE collects (
    id                  BIGSERIAL PRIMARY KEY,
    chain_id            INTEGER NOT NULL DEFAULT 1,
    pool_address        VARCHAR(42) NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMP NOT NULL,
    tx_hash             VARCHAR(66) NOT NULL,
    log_index           INTEGER NOT NULL,

    owner               VARCHAR(42) NOT NULL,
    recipient           VARCHAR(42) NOT NULL,
    tick_lower          INTEGER NOT NULL,
    tick_upper          INTEGER NOT NULL,

    amount0_raw         NUMERIC(78, 0) NOT NULL,
    amount1_raw         NUMERIC(78, 0) NOT NULL,

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_collects_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address),
    CONSTRAINT uq_collects_tx_log UNIQUE (tx_hash, log_index)
);
```
```
CREATE INDEX idx_collects_pool_address ON collects(pool_address);
CREATE INDEX idx_collects_block_timestamp ON collects(block_timestamp);
CREATE INDEX idx_collects_owner ON collects(owner);
CREATE INDEX idx_collects_pool_time ON collects(pool_address, block_timestamp);
```

四、第三层：快照与聚合表

这一层是给 Data Engine 用的。
原则是：

原始事件表负责“真实记录”
聚合表负责“快速分析”


7. pool_price_snapshots

作用：

按时间记录池子价格和状态快照，用于：

波动率计算

价格曲线

tick 变化分析

TVL 估算
```
CREATE TABLE pool_price_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    pool_address        VARCHAR(42) NOT NULL,
    chain_id            INTEGER NOT NULL DEFAULT 1,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMP NOT NULL,

    sqrt_price_x96      NUMERIC(78, 0) NOT NULL,
    tick                INTEGER NOT NULL,
    liquidity           NUMERIC(78, 0) NOT NULL,

    price_token0        NUMERIC(38, 18),
    price_token1        NUMERIC(38, 18),

    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_snapshots_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address),
    CONSTRAINT uq_snapshots_pool_block UNIQUE (pool_address, block_number)
);
```
```
CREATE INDEX idx_snapshots_pool_time ON pool_price_snapshots(pool_address, block_timestamp);
CREATE INDEX idx_snapshots_block_time ON pool_price_snapshots(block_timestamp);
```

8. pool_metrics_hourly

作用：

按小时聚合池子指标。
这是 Data Engine 的核心中间表。

```
CREATE TABLE pool_metrics_hourly (
    id                      BIGSERIAL PRIMARY KEY,
    pool_address            VARCHAR(42) NOT NULL,
    chain_id                INTEGER NOT NULL DEFAULT 1,
    metric_hour             TIMESTAMP NOT NULL,

    price_open              NUMERIC(38, 18),
    price_close             NUMERIC(38, 18),
    price_high              NUMERIC(38, 18),
    price_low               NUMERIC(38, 18),

    volume_token0_raw       NUMERIC(78, 0) NOT NULL DEFAULT 0,
    volume_token1_raw       NUMERIC(78, 0) NOT NULL DEFAULT 0,
    volume_usd              NUMERIC(38, 18) NOT NULL DEFAULT 0,

    swap_count              INTEGER NOT NULL DEFAULT 0,
    mint_count              INTEGER NOT NULL DEFAULT 0,
    burn_count              INTEGER NOT NULL DEFAULT 0,
    collect_count           INTEGER NOT NULL DEFAULT 0,

    fee_usd                 NUMERIC(38, 18) NOT NULL DEFAULT 0,
    avg_liquidity           NUMERIC(78, 0),
    close_liquidity         NUMERIC(78, 0),

    created_at              TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_hourly_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address),
    CONSTRAINT uq_hourly_pool_hour UNIQUE (pool_address, metric_hour)
);
```
这张表能支持什么分析

24h volume

7d volume

hourly volatility

fee APR

活跃度排行


9. pool_metrics_daily

作用：

按天聚合，适合 Dashboard 和策略模块直接读取。
```
CREATE TABLE pool_metrics_daily (
    id                      BIGSERIAL PRIMARY KEY,
    pool_address            VARCHAR(42) NOT NULL,
    chain_id                INTEGER NOT NULL DEFAULT 1,
    metric_date             DATE NOT NULL,

    price_open              NUMERIC(38, 18),
    price_close             NUMERIC(38, 18),
    price_high              NUMERIC(38, 18),
    price_low               NUMERIC(38, 18),

    volume_usd              NUMERIC(38, 18) NOT NULL DEFAULT 0,
    fee_usd                 NUMERIC(38, 18) NOT NULL DEFAULT 0,
    tvl_usd                 NUMERIC(38, 18),

    swap_count              INTEGER NOT NULL DEFAULT 0,
    mint_count              INTEGER NOT NULL DEFAULT 0,
    burn_count              INTEGER NOT NULL DEFAULT 0,
    collect_count           INTEGER NOT NULL DEFAULT 0,

    volatility_1d           NUMERIC(20, 10),
    volume_tvl_ratio        NUMERIC(20, 10),
    fee_apr                 NUMERIC(20, 10),
    il_estimate_1d          NUMERIC(20, 10),

    created_at              TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_daily_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address),
    CONSTRAINT uq_daily_pool_date UNIQUE (pool_address, metric_date)
);
```
这是你后面最常查的一张表

比如：

最近 7 天哪个 pool 最值得做 LP

fee_apr 最高的是谁

volume/tvl 最高的是谁

波动率和 APR 组合最好的池子是谁


五、第四层：系统状态与策略表

10. sync_state

作用：

记录各采集任务同步到了哪个区块。
```
CREATE TABLE sync_state (
    sync_key            VARCHAR(128) PRIMARY KEY,
    chain_id            INTEGER NOT NULL DEFAULT 1,
    last_synced_block   BIGINT NOT NULL,
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);
```

11. strategy_signals

作用：

存储策略模块输出结果
```
CREATE TABLE strategy_signals (
    id                      BIGSERIAL PRIMARY KEY,
    pool_address            VARCHAR(42) NOT NULL,
    chain_id                INTEGER NOT NULL DEFAULT 1,
    signal_time             TIMESTAMP NOT NULL,

    signal_type             VARCHAR(64) NOT NULL,
    signal_score            NUMERIC(20, 10),

    recommended_lower_price NUMERIC(38, 18),
    recommended_upper_price NUMERIC(38, 18),

    expected_fee_apr        NUMERIC(20, 10),
    expected_il             NUMERIC(20, 10),
    expected_net_apr        NUMERIC(20, 10),

    reason                  JSONB,
    created_at              TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_signal_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address)
);
```

六、如果你后面要做自己的 LP 仓位管理

这部分第一版可以不做，但我建议预留思路。
12. lp_positions
```
CREATE TABLE lp_positions (
    id                      BIGSERIAL PRIMARY KEY,
    position_id             VARCHAR(128) UNIQUE,
    pool_address            VARCHAR(42) NOT NULL,
    owner_address           VARCHAR(42) NOT NULL,

    tick_lower              INTEGER NOT NULL,
    tick_upper              INTEGER NOT NULL,
    liquidity               NUMERIC(78, 0) NOT NULL,

    opened_at               TIMESTAMP NOT NULL,
    closed_at               TIMESTAMP,

    status                  VARCHAR(32) NOT NULL DEFAULT 'OPEN',
    created_at              TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_lp_positions_pool FOREIGN KEY (pool_address) REFERENCES pools(pool_address)
);
```
13. lp_position_actions
```
CREATE TABLE lp_position_actions (
    id                  BIGSERIAL PRIMARY KEY,
    position_id         VARCHAR(128) NOT NULL,
    action_type         VARCHAR(32) NOT NULL,
    tx_hash             VARCHAR(66),
    block_number        BIGINT,
    action_time         TIMESTAMP NOT NULL,
    metadata            JSONB,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);
```