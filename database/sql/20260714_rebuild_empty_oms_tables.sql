-- AutoTrades 2.0: rebuild the six EMPTY OMS tables with canonical keys/indexes.
-- IMPORTANT: this script drops the six OMS tables. Run only when all six are empty.
-- Stop broker reconcile and trade backfill before running it.

USE autotrades;

-- Confirm these all return 0 before proceeding.
SELECT 'oms_funds' AS table_name, COUNT(*) AS row_count FROM oms_funds
UNION ALL SELECT 'oms_funds_history', COUNT(*) FROM oms_funds_history
UNION ALL SELECT 'oms_orders', COUNT(*) FROM oms_orders
UNION ALL SELECT 'oms_orders_history', COUNT(*) FROM oms_orders_history
UNION ALL SELECT 'oms_positions', COUNT(*) FROM oms_positions
UNION ALL SELECT 'oms_positions_history', COUNT(*) FROM oms_positions_history;

DROP TABLE IF EXISTS
  oms_positions_history,
  oms_positions,
  oms_orders_history,
  oms_orders,
  oms_funds_history,
  oms_funds;

CREATE TABLE `oms_funds` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `trading_day` date NOT NULL,
  `client_id` varchar(50) NOT NULL,
  `net_balance` decimal(15,2) DEFAULT NULL,
  `available_cash` decimal(15,2) DEFAULT NULL,
  `opening_balance` decimal(15,2) DEFAULT NULL,
  `live_balance` decimal(15,2) DEFAULT NULL,
  `collateral` decimal(15,2) DEFAULT NULL,
  `utilised_margin` decimal(15,2) DEFAULT NULL,
  `span_margin` decimal(15,2) DEFAULT NULL,
  `exposure_margin` decimal(15,2) DEFAULT NULL,
  `option_premium` decimal(15,2) DEFAULT NULL,
  `m2m_realised` decimal(15,2) DEFAULT NULL,
  `m2m_unrealised` decimal(15,2) DEFAULT NULL,
  `available_margin` decimal(15,2) DEFAULT NULL,
  `polled_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_client_day` (`client_id`,`trading_day`),
  KEY `idx_trading_day` (`trading_day`),
  KEY `idx_client_polled_at` (`client_id`,`polled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `oms_funds_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `client_id` varchar(50) NOT NULL,
  `trading_day` date NOT NULL,
  `snapshot_json` json NOT NULL,
  `polled_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_day_time` (`client_id`,`trading_day`,`polled_at`),
  KEY `idx_trading_day_time` (`trading_day`,`polled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `oms_orders` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `trading_day` date NOT NULL,
  `client_id` varchar(50) NOT NULL,
  `order_id` varchar(40) NOT NULL,
  `exchange_order_id` varchar(40) DEFAULT NULL,
  `tradingsymbol` varchar(50) DEFAULT NULL,
  `instrument` varchar(30) DEFAULT NULL,
  `instrument_token` bigint DEFAULT NULL,
  `exchange` varchar(10) DEFAULT NULL,
  `transaction_type` varchar(5) DEFAULT NULL,
  `product` varchar(10) DEFAULT NULL,
  `order_type` varchar(10) DEFAULT NULL,
  `variety` varchar(10) DEFAULT NULL,
  `validity` varchar(10) DEFAULT NULL,
  `validity_ttl` int DEFAULT NULL,
  `quantity` int DEFAULT NULL,
  `disclosed_quantity` int DEFAULT NULL,
  `filled_quantity` int DEFAULT NULL,
  `pending_quantity` int DEFAULT NULL,
  `cancelled_quantity` int DEFAULT NULL,
  `price` decimal(14,4) DEFAULT NULL,
  `average_price` decimal(14,4) DEFAULT NULL,
  `trigger_price` decimal(14,4) DEFAULT NULL,
  `status` varchar(30) DEFAULT NULL,
  `order_timestamp` datetime DEFAULT NULL,
  `exchange_timestamp` datetime DEFAULT NULL,
  `tag` varchar(50) DEFAULT NULL,
  `order_issued_at` varchar(10) DEFAULT NULL,
  `order_placed_by` varchar(50) DEFAULT NULL,
  `recon_status` varchar(20) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `first_seen_at` datetime DEFAULT NULL,
  `polled_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_client_order` (`client_id`,`order_id`),
  KEY `idx_day_client_time` (`trading_day`,`client_id`,`order_timestamp`),
  KEY `idx_symbol` (`tradingsymbol`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `oms_orders_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `client_id` varchar(50) NOT NULL,
  `trading_day` date NOT NULL,
  `polled_at` datetime NOT NULL,
  `broker_payload` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_day_time` (`client_id`,`trading_day`,`polled_at`),
  KEY `idx_polled_at` (`polled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `oms_positions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `trading_day` date NOT NULL,
  `client_id` varchar(50) NOT NULL,
  `tradingsymbol` varchar(50) NOT NULL,
  `instrument` varchar(20) DEFAULT NULL,
  `instrument_token` bigint DEFAULT NULL,
  `exchange` varchar(10) DEFAULT NULL,
  `segment` varchar(10) DEFAULT NULL,
  `product` varchar(10) DEFAULT NULL,
  `quantity` int DEFAULT NULL,
  `overnight_quantity` int DEFAULT NULL,
  `multiplier` decimal(10,4) DEFAULT NULL,
  `average_price` decimal(14,4) DEFAULT NULL,
  `close_price` decimal(14,4) DEFAULT NULL,
  `last_price` decimal(14,4) DEFAULT NULL,
  `value` decimal(14,4) DEFAULT NULL,
  `pnl` decimal(14,4) DEFAULT NULL,
  `m2m` decimal(14,4) DEFAULT NULL,
  `unrealised` decimal(14,4) DEFAULT NULL,
  `realised` decimal(14,4) DEFAULT NULL,
  `buy_quantity` int DEFAULT NULL,
  `buy_price` decimal(14,4) DEFAULT NULL,
  `buy_value` decimal(14,4) DEFAULT NULL,
  `buy_m2m` decimal(14,4) DEFAULT NULL,
  `sell_quantity` int DEFAULT NULL,
  `sell_price` decimal(14,4) DEFAULT NULL,
  `sell_value` decimal(14,4) DEFAULT NULL,
  `sell_m2m` decimal(14,4) DEFAULT NULL,
  `day_buy_quantity` int DEFAULT NULL,
  `day_buy_price` decimal(14,4) DEFAULT NULL,
  `day_buy_value` decimal(14,4) DEFAULT NULL,
  `day_sell_quantity` int DEFAULT NULL,
  `day_sell_price` decimal(14,4) DEFAULT NULL,
  `day_sell_value` decimal(14,4) DEFAULT NULL,
  `polled_at` datetime NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_position` (`client_id`,`trading_day`,`tradingsymbol`,`product`),
  KEY `idx_position_latest` (`client_id`,`tradingsymbol`,`product`,`polled_at`),
  KEY `idx_symbol` (`tradingsymbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `oms_positions_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `client_id` varchar(50) NOT NULL,
  `trading_day` date NOT NULL,
  `polled_at` datetime NOT NULL,
  `broker_payload` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_day_time` (`client_id`,`trading_day`,`polled_at`),
  KEY `idx_polled_at` (`polled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Verification: every table must show id as PRI / auto_increment.
SELECT
    table_name,
    column_name,
    column_type,
    is_nullable,
    column_key,
    extra
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name IN (
      'oms_funds',
      'oms_funds_history',
      'oms_orders',
      'oms_orders_history',
      'oms_positions',
      'oms_positions_history'
  )
  AND column_name = 'id'
ORDER BY table_name;

SELECT
    table_name,
    index_name,
    non_unique,
    GROUP_CONCAT(column_name ORDER BY seq_in_index) AS indexed_columns
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name IN (
      'oms_funds',
      'oms_funds_history',
      'oms_orders',
      'oms_orders_history',
      'oms_positions',
      'oms_positions_history'
  )
GROUP BY table_name, index_name, non_unique
ORDER BY table_name, index_name;
