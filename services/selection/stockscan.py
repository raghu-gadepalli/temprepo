#!/usr/bin/env python3
"""Daily active-universe selector for AutoTrades.

stockscan is no longer an intraday promote/demote engine. It runs once after
first 1-minute candle, scans the monthly enabled EQ universe directly from
broker candle/quote data, and marks the selected symbols active for the day.

It deliberately does not create signals, evaluate setups, or touch
symbols.generate_signals. StockAdvisor/EvidenceEvaluator handle per-snapshot
tradeability later.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import AppConfig
from configs.scanner_config import SCANNER_CONFIG
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from services.zerodha.kiteconnect_service import KiteConnectService
from utils.datetime_utils import IST, business_now
from utils.universe_policy import universe_blacklist, universe_whitelist

logger = logging.getLogger(__name__)

SCAN = SCANNER_CONFIG.scan
BLACKLIST = universe_blacklist()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _norm(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _aware_ist(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _score(value: float, normalizer: float) -> float:
    if normalizer <= 0:
        return 0.0
    return min(abs(float(value)) / float(normalizer), 1.0)


def _market_open(as_of: datetime) -> datetime:
    t = dtime.fromisoformat(SCAN.market_open_time)
    a = _aware_ist(as_of or business_now())
    return datetime.combine(a.date(), t, IST)


def _quote_key(sym: SymbolSchema) -> str:
    exchange = (getattr(sym, "exchange", None) or SCAN.exchange or "NSE").strip().upper()
    return f"{exchange}:{sym.symbol}"


def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
    step = max(1, int(size))
    for idx in range(0, len(items), step):
        yield items[idx : idx + step]


def _parse_candle_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware_ist(value)
    try:
        return _aware_ist(datetime.fromisoformat(str(value)))
    except Exception:
        return None


# ---------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    symbol: str
    score: float
    direction: str
    candle_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    prev_close: Optional[float]
    gap_pct: float
    day_move_pct: float
    candle_move_pct: float
    candle_range_pct: float
    turnover_lakh: float
    gap_score: float
    day_move_score: float
    candle_move_score: float
    candle_range_score: float
    turnover_score: float


# ---------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------


def _annotated_candidate_rows(
    candidates: List[Candidate],
    selected: List[str],
    selected_whitelist: List[str],
    selected_dynamic: List[str],
) -> List[Dict[str, Any]]:
    """Return candidate rows with selection/debug markers for test output.

    The markers make whitelist selection explicit. They are diagnostic only;
    stockscan still applies selection from the selected symbol lists.
    """
    whitelist_set = {_norm(x) for x in selected_whitelist}
    dynamic_set = {_norm(x) for x in selected_dynamic}
    selected_set = {_norm(x) for x in selected}

    rows: List[Dict[str, Any]] = []
    for rank, cand in enumerate(candidates, start=1):
        row = asdict(cand)
        sym = _norm(cand.symbol)
        is_whitelist = sym in whitelist_set
        is_dynamic = sym in dynamic_set
        is_selected = sym in selected_set

        if is_whitelist:
            reason = "WHITELIST"
        elif is_dynamic:
            reason = "DAILY_SCORE"
        else:
            reason = ""

        row.update(
            {
                "rank": rank,
                "selected": bool(is_selected),
                "is_whitelisted": bool(is_whitelist),
                "selection_reason": reason,
            }
        )
        rows.append(row)
    return rows

class StockScanner:
    """Once-daily stock selector.

    It answers only: which enabled EQ symbols should be active for today's
    snapshot generation? It never mutates monthly enabled policy and never
    toggles generate_signals.
    """

    def __init__(self) -> None:
        self.whitelist = universe_whitelist()

    def _kite(self) -> KiteConnectService:
        user = UserSchema.fetch_user(AppConfig.DATA_USER)
        if not user:
            raise RuntimeError(f"DATA_USER not found: {AppConfig.DATA_USER}")
        if not user.apikey or not user.access_token:
            raise RuntimeError(f"DATA_USER missing apikey/access_token: {AppConfig.DATA_USER}")
        return KiteConnectService(api_key=user.apikey, access_token=user.access_token)

    def _scan_universe(self) -> List[SymbolSchema]:
        rows = SymbolSchema.fetch_daily_scan_universe(type_filter="EQ") or []
        universe: List[SymbolSchema] = []
        for rec in rows:
            sym = _norm(getattr(rec, "symbol", ""))
            if not sym:
                continue
            if sym in BLACKLIST:
                continue
            if getattr(rec, "token", None) in (None, ""):
                logger.warning("Stockscan skipping %s due to missing token", sym)
                continue
            universe.append(rec)
        return universe

    def _fetch_quotes(self, kite: KiteConnectService, universe: List[SymbolSchema]) -> Dict[str, Dict[str, Any]]:
        key_by_symbol = {_norm(s.symbol): _quote_key(s) for s in universe}
        out: Dict[str, Dict[str, Any]] = {}
        for batch in _chunks(list(key_by_symbol.values()), SCAN.quote_batch_size):
            qmap = kite.fetch_quote(batch) or {}
            for symbol, key in key_by_symbol.items():
                if key in qmap:
                    out[symbol] = qmap[key] or {}
        return out

    def _first_candle(
        self,
        kite: KiteConnectService,
        sym: SymbolSchema,
        as_of: datetime,
    ) -> Optional[Dict[str, Any]]:
        start = _market_open(as_of)
        min_end = start + timedelta(minutes=max(1, int(SCAN.first_candle_minutes)))
        end = max(_aware_ist(as_of), min_end)

        token = int(getattr(sym, "token"))
        candles = kite.fetch_historical_data(
            instrument_token=token,
            from_date=start,
            to_date=end,
            interval=SCAN.historical_interval,
            oi=False,
        ) or []

        if not candles:
            return None

        valid: List[Tuple[datetime, Dict[str, Any]]] = []
        for c in candles:
            ts = _parse_candle_time(c.get("date"))
            if ts is None:
                continue
            if ts < start:
                continue
            valid.append((ts, c))

        if not valid:
            return None
        valid.sort(key=lambda x: x[0])
        return valid[0][1]

    def _build_candidate(
        self,
        sym: SymbolSchema,
        candle: Dict[str, Any],
        quote: Optional[Dict[str, Any]],
    ) -> Optional[Candidate]:
        o = _safe_float(candle.get("open"))
        h = _safe_float(candle.get("high"))
        l = _safe_float(candle.get("low"))
        c = _safe_float(candle.get("close"))
        v = _safe_float(candle.get("volume"))
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            return None

        q = quote or {}
        prev_close = None
        try:
            ohlc = q.get("ohlc") or {}
            if ohlc.get("close") is not None:
                prev_close = float(ohlc["close"])
        except Exception:
            prev_close = None

        gap_pct = ((o - prev_close) / prev_close * 100.0) if prev_close and prev_close > 0 else 0.0
        day_move_pct = ((c - prev_close) / prev_close * 100.0) if prev_close and prev_close > 0 else 0.0
        move_pct = (c - o) / o * 100.0
        range_pct = (h - l) / o * 100.0
        turnover_lakh = (v * c) / 100000.0

        gap_score = _score(gap_pct, SCAN.gap_norm_pct)
        day_move_score = _score(day_move_pct, SCAN.day_move_norm_pct)
        move_score = _score(move_pct, SCAN.candle_move_norm_pct)
        range_score = _score(range_pct, SCAN.candle_range_norm_pct)
        turnover_score = _score(turnover_lakh, SCAN.turnover_norm_lakh)

        total = 0.0
        if SCAN.use_gap:
            total += SCAN.w_gap * gap_score
        if SCAN.use_day_move:
            total += SCAN.w_day_move * day_move_score
        if SCAN.use_candle_move:
            total += SCAN.w_candle_move * move_score
        if SCAN.use_candle_range:
            total += SCAN.w_candle_range * range_score
        if SCAN.use_turnover:
            total += SCAN.w_turnover * turnover_score

        if day_move_pct > 0:
            direction = "UP"
        elif day_move_pct < 0:
            direction = "DOWN"
        elif c > o:
            direction = "UP"
        elif c < o:
            direction = "DOWN"
        else:
            direction = "FLAT"

        ts = _parse_candle_time(candle.get("date")) or _market_open(business_now())
        return Candidate(
            symbol=sym.symbol,
            score=round(total, 6),
            direction=direction,
            candle_time=ts,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            prev_close=prev_close,
            gap_pct=round(gap_pct, 4),
            day_move_pct=round(day_move_pct, 4),
            candle_move_pct=round(move_pct, 4),
            candle_range_pct=round(range_pct, 4),
            turnover_lakh=round(turnover_lakh, 4),
            gap_score=round(gap_score, 4),
            day_move_score=round(day_move_score, 4),
            candle_move_score=round(move_score, 4),
            candle_range_score=round(range_score, 4),
            turnover_score=round(turnover_score, 4),
        )

    def _build_candidates(self, as_of: datetime) -> Tuple[List[Candidate], Dict[str, int]]:
        as_of = _aware_ist(as_of or business_now())
        universe = self._scan_universe()
        kite = self._kite()
        quotes = self._fetch_quotes(kite, universe)

        candidates: List[Candidate] = []
        missing_candle = 0
        invalid_candle = 0
        inspected = 0

        for idx, sym in enumerate(universe, start=1):
            inspected += 1
            try:
                candle = self._first_candle(kite, sym, as_of)
                if candle is None:
                    missing_candle += 1
                    continue
                cand = self._build_candidate(sym, candle, quotes.get(_norm(sym.symbol)))
                if cand is None:
                    invalid_candle += 1
                    continue
                candidates.append(cand)
                if SCAN.historical_rate_sleep_sec > 0:
                    time.sleep(SCAN.historical_rate_sleep_sec)
            except Exception:
                missing_candle += 1
                logger.exception("Stockscan failed candidate build for %s", getattr(sym, "symbol", None))

        candidates.sort(key=lambda x: x.score, reverse=True)
        stats = {
            "universe": len(universe),
            "inspected": inspected,
            "candidates": len(candidates),
            "missing_candle": missing_candle,
            "invalid_candle": invalid_candle,
        }
        return candidates, stats

    def _select_symbols(self, candidates: List[Candidate]) -> Tuple[List[str], List[str], List[str]]:
        whitelist = set(self.whitelist)
        candidate_symbols = {c.symbol for c in candidates}

        # Include only whitelist symbols that exist in the enabled scan universe.
        universe_symbols = {_norm(s.symbol) for s in self._scan_universe()}
        selected_whitelist = sorted([s for s in whitelist if s in universe_symbols])

        if SCAN.cap_total_includes_whitelist:
            dynamic_slots = max(0, int(SCAN.daily_active_limit) - len(selected_whitelist))
        else:
            dynamic_slots = max(0, int(SCAN.daily_active_limit))

        selected_dynamic: List[str] = []
        selected_set = set(selected_whitelist)
        for cand in candidates:
            if len(selected_dynamic) >= dynamic_slots:
                break
            sym = _norm(cand.symbol)
            if sym in selected_set:
                continue
            selected_dynamic.append(cand.symbol)
            selected_set.add(sym)

        selected = selected_whitelist + selected_dynamic
        return selected, selected_whitelist, selected_dynamic

    def assemble(self, as_of: datetime) -> Dict[str, Any]:
        as_of = _aware_ist(as_of or business_now())
        candidates, stats = self._build_candidates(as_of)
        selected, selected_whitelist, selected_dynamic = self._select_symbols(candidates)
        candidate_rows = _annotated_candidate_rows(
            candidates=candidates,
            selected=selected,
            selected_whitelist=selected_whitelist,
            selected_dynamic=selected_dynamic,
        )
        return {
            "as_of": as_of.isoformat(),
            "stats": stats,
            "daily_active_limit": SCAN.daily_active_limit,
            "selected": selected,
            "selected_whitelist": selected_whitelist,
            "selected_dynamic": selected_dynamic,
            "candidates": candidate_rows,
        }

    def generate_scan(self, as_of: datetime, apply_updates: bool = True) -> Dict[str, Any]:
        result = self.assemble(as_of)
        selected = result["selected"]
        update_result: Dict[str, int] = {}
        if apply_updates:
            update_result = SymbolSchema.apply_daily_active_selection(
                selected_symbols=selected,
                whitelist_symbols=list(self.whitelist),
                type_filter="EQ",
            )

        result.update(
            {
                "apply_updates": bool(apply_updates),
                "update_result": update_result,
                "activated": selected,
                "activated_count": len(selected),
                "processed": int(update_result.get("activated_count", 0)) if update_result else 0,
            }
        )
        return result
