-- AutoTrades Phase 5A single-owner Auction Signal service.
-- Target database: backtest (or the configured trades database used for replay).
-- Safe report-only replay does not require these tables. Apply this migration
-- before enabling --write-checkpoints or --write-opportunities.

CREATE TABLE IF NOT EXISTS stock_opportunities (
    id BIGINT NOT NULL AUTO_INCREMENT,
    trading_day DATE NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    opportunity_key VARCHAR(255) NOT NULL,
    boundary_event_key VARCHAR(255) NOT NULL,
    range_id VARCHAR(255) NULL,
    side VARCHAR(8) NOT NULL,
    primary_setup_family VARCHAR(50) NOT NULL,
    primary_setup_subtype VARCHAR(100) NOT NULL,
    primary_candidate_id VARCHAR(255) NOT NULL,
    primary_candidate_role VARCHAR(50) NOT NULL,
    lifecycle_state VARCHAR(30) NOT NULL,
    attempt_time DATETIME NULL,
    first_observed_time DATETIME NOT NULL,
    last_observed_time DATETIME NOT NULL,
    eligible_time DATETIME NULL,
    terminal_time DATETIME NULL,
    selected_time DATETIME NULL,
    consumed_time DATETIME NULL,
    entry_anchor_price DECIMAL(14,4) NULL,
    boundary_price DECIMAL(14,4) NULL,
    stop_anchor_price DECIMAL(14,4) NULL,
    target_basis VARCHAR(100) NULL,
    target_reference_price DECIMAL(14,4) NULL,
    source_auction_state VARCHAR(50) NULL,
    established_trend_side VARCHAR(8) NULL,
    candidate_interpretations_json JSON NOT NULL,
    event_history_json JSON NOT NULL,
    reason_codes_json JSON NOT NULL,
    diagnostics_json JSON NOT NULL,
    config_version VARCHAR(100) NOT NULL,
    signal_id VARCHAR(36) NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_stock_opportunity_key (opportunity_key),
    KEY idx_stock_opportunity_symbol_day (symbol, trading_day),
    KEY idx_stock_opportunity_active (symbol, trading_day, lifecycle_state, side),
    KEY idx_stock_opportunity_boundary (boundary_event_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Phase 4A created this field as BIGINT in its draft migration. Signal IDs in
-- the existing signals table are UUID-like VARCHAR(36), so align the type.
ALTER TABLE stock_opportunities
    MODIFY COLUMN signal_id VARCHAR(36) NULL;

CREATE TABLE IF NOT EXISTS stock_engine_checkpoints (
    id BIGINT NOT NULL AUTO_INCREMENT,
    trading_day DATE NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    engine_name VARCHAR(64) NOT NULL,
    engine_version VARCHAR(32) NOT NULL,
    config_version VARCHAR(100) NOT NULL,
    last_processed_snapshot_time DATETIME NOT NULL,
    last_snapshot_hash VARCHAR(64) NULL,
    checkpoint_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    checkpoint_version INT NOT NULL DEFAULT 1,
    state_json JSON NOT NULL,
    diagnostics_json JSON NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_stock_engine_checkpoint_day_symbol_engine
        (trading_day, symbol, engine_name),
    KEY idx_stock_engine_checkpoint_lookup
        (trading_day, engine_name, checkpoint_status),
    KEY idx_stock_engine_checkpoint_time
        (symbol, last_processed_snapshot_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
