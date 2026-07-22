from __future__ import annotations

from typing import List
from pydantic import BaseModel, Field


class UniverseConfig(BaseModel):
    """Single source for monthly / expiry-cycle stock universe policy.

    blacklist: symbols structurally excluded from monthly/daily selection.
    whitelist: symbols protected from structural filters.

    Intraday stockscan must not mutate this policy.  It should only select
    the day's active subset from symbols where enabled=True.
    """

    blacklist: List[str] = Field(default_factory=lambda: [

        # Adani / event driven
        "ADANIENT",
        "ADANIPOWER",
        "ADANIENSOL",

        # PSU / theme driven
        "IREDA",
        "IRFC",
        "RVNL",
        "NBCC",
        "NHPC",

        # Retail / speculative
        "IDEA",
        "SUZLON",

        # New age / sentiment driven
        "NYKAA",
        "SWIGGY",

        # High beta banking
        "YESBANK",
        "RBLBANK",

        # Renewable / narrative driven
        "WAAREEENER",

        # Shipping / infra
        "COCHINSHIP",
        "CONCOR",

        # Airports / exchange
        "GMRAIRPORT",
        "IEX",

        # Property
        "GODREJPROP",

        # PSU banks
        "BANKINDIA",
        "CANBK",
        "PNB",
        "UNIONBANK",
        "BANDHANBNK",
        "FEDERALBNK",
        "IDFCFIRSTB",

        # Auto / ancillary
        "ASHOKLEY",
        "SONACOMS",
        "MOTHERSON",

        # Metals
        "SAIL",
        "NMDC",

        # Operator / erratic behavior
        "DIXON",
        "KAYNES",
        "KEI",
        "KFINTECH",

        # Merger / restructuring uncertainty
        "PFC",
        "RECLTD",

        # Low movement / opportunity cost
        "WIPRO",
        "NTPC",

        # Empirically inconsistent behavior
        "CUMMINSIND",
        "IOC",
        "VBL",
        "ZYDUSLIFE",

        # Index products
        "FINNIFTY",
        "MIDCPNIFTY",
        "NIFTYNXT50",
    ])
    whitelist: List[str] = Field(default_factory=lambda: [
        "NIFTY 50", "NIFTY BANK", "KOTAKBANK", "ICICIBANK",
        "AXISBANK", "HCLTECH", "MARUTI",
    ])


class ScanServiceConfig(BaseModel):
    """Once-daily stockscan configuration.

    stockscan is now a daily active-universe selector, not an intraday
    promote/demote service. It runs once after the first 1-minute candle and
    sets only symbols.active for the selected daily basket. It does not touch
    enabled or generate_signals.
    """

    run_time: str = "09:16:00"
    log_file: str = "/var/www/autotrades/scripts/stockscan.log"

    # Daily selection capacity. This cap includes universe.whitelist when
    # cap_total_includes_whitelist=True.
    daily_active_limit: int = 75
    cap_total_includes_whitelist: bool = True

    # universe.whitelist is the single source of truth for symbols that are
    # protected from monthly filtering and forced into the daily active basket.

    # Direct 1-minute candle scan source.
    exchange: str = "NSE"
    historical_interval: str = "minute"
    market_open_time: str = "09:15:00"
    first_candle_minutes: int = 1
    quote_batch_size: int = 250
    historical_rate_sleep_sec: float = 0.08

    # Scoring switches.
    # gap_pct          = first 1-minute open vs previous close.
    # day_move_pct     = first 1-minute close vs previous close.
    # candle_move_pct  = first 1-minute close vs first 1-minute open.
    # candle_range_pct = first 1-minute high-low range vs first 1-minute open.
    use_gap: bool = True
    use_day_move: bool = True
    use_candle_move: bool = True
    use_candle_range: bool = True
    use_turnover: bool = True

    # Scoring weights. Scores are normalized to 0..1 before weighting.
    # day_move carries the largest weight because it captures both gap and
    # first-candle continuation/rejection relative to previous close.
    w_gap: float = 0.15
    w_day_move: float = 0.35
    w_candle_move: float = 0.20
    w_candle_range: float = 0.15
    w_turnover: float = 0.15

    # Normalizers for first-candle ranking.
    # These are intentionally configurable because the first 1-minute candle
    # can be very broad on strong gap/trend days.  Larger values reduce score
    # saturation and spread candidate ranking better.
    gap_norm_pct: float = 2.00
    day_move_norm_pct: float = 3.00
    candle_move_norm_pct: float = 1.50
    candle_range_norm_pct: float = 2.00
    turnover_norm_lakh: float = 3000.0


class StockEnableConfig(BaseModel):
    """Legacy config retained only so old imports fail less noisily.

    scripts/stockenable.py is retired.  Daily selection is handled by
    scripts/run_stockscan.py using ScanServiceConfig.
    """

    retired: bool = True
    log_file: str = "/var/www/autotrades/scripts/stockenable.log"


class FilterConfig(BaseModel):
    log_file: str = "/var/www/autotrades/scripts/filter_stocks.log"
    csv_file: str = "/var/www/autotrades/scripts/universe_metrics.csv"
    min_price: float = 200.0
    quote_batch: int = 250
    exchange: str = "NSE"
    rate_sleep_sec: float = 0.35

    enable_vol_beta_filter: bool = True
    vol_interval: str = "minute"
    vol_lookback_days: int = 21
    atr_period: int = 14
    min_atr_pct: float = 0.080

    beta_index_symbol: str = "NIFTY 50"
    beta_lookback_days: int = 60
    beta_abs_floor: float = 0.60

    vol_rate_sleep_sec: float = 0.35
    max_symbols_vol: int = 0
    compute_all_metrics: bool = True


class ScannerConfig(BaseModel):
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    scan: ScanServiceConfig = Field(default_factory=ScanServiceConfig)
    stockenable: StockEnableConfig = Field(default_factory=StockEnableConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)


SCANNER_CONFIG = ScannerConfig()
