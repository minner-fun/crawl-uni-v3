# 数据库信息
```
host: 127.0.0.1 
port: 6379
user: root 
passwd: 12345678
database: v3info
```
# 第一层 token与pools信息表
## 1. tokens
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

## 2. pools
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

# 三、第二层：原始事件表

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


## 3. swaps

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

## 4. mints

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


## 5. burns

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

## 6. collects

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