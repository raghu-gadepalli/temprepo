# services/generator/derivatives_generator.py
#
# v2 generator (updated):
# - Table: derivativeschain_v2 (symbol, snapshot_time, raw, derived)
# - RAW = {spot_price, future, options}
# - DERIVED computed from *all rows since start-of-day* (single DB read) + current raw
# - Persist as DerivativesChainSchema(symbol, snapshot_time, raw, derived)
#
# UPDATED:
# - Only ONE broker quote call per symbol:
#     spot + future + full option chain are fetched in a single fetch_quote(keys)

import logging
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, Tuple, List

from configs.derivatives_config import DERIVATIVES_CONFIG
from schemas.derivatives import DerivativesChainSchema
from schemas.symbol import SymbolSchema
from services.zerodha.kiteconnect_service import KiteConnectService

from services.derivatives.derivatives_helper import build_derived_from_day

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def normalize_quote_time(raw_time: Any, target_date: date) -> Optional[str]:
    """
    Normalize broker quote timestamp to an ISO string for the current trading day.

    Kite quote timestamps may arrive as datetime objects or strings.  Store an
    ISO timestamp only when the timestamp belongs to the generator's as_of date;
    otherwise return None to avoid trusting stale / previous-day quotes.
    Naive timestamps are treated as IST because the generator runs in IST.
    """
    if raw_time is None:
        return None

    try:
        if isinstance(raw_time, datetime):
            parsed = raw_time
        elif isinstance(raw_time, str):
            raw = raw_time.strip()
            if not raw:
                return None
            parsed = datetime.fromisoformat(raw)
        else:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        else:
            parsed = parsed.astimezone(IST)

        if parsed.date() != target_date:
            return None

        return parsed.isoformat()
    except Exception:
        return None


def _quote_key(sym: SymbolSchema) -> str:
    return f"{sym.exchange}:{sym.symbol}"


def _build_quote_record(sym: SymbolSchema, q: Dict[str, Any], as_of: datetime) -> Dict[str, Any]:
    """
    Normalize one broker quote into stored RAW record shape used for future/options.
    """
    q = q or {}
    ohlc = q.get("ohlc", {}) or {}

    raw_quote_time = q.get("timestamp") or q.get("last_trade_time") or q.get("last_update_time")
    normalized_qt = normalize_quote_time(raw_quote_time, as_of.date())

    return {
        "instrument": sym.symbol,
        "exchange": sym.exchange,
        "expiry": getattr(sym, "expiry", None).isoformat() if getattr(sym, "expiry", None) else None,

        "last_price": q.get("last_price"),
        "oi": q.get("oi", 0) or 0,
        "volume": q.get("volume", 0) or 0,

        "ohlc": {
            "open":  ohlc.get("open"),
            "high":  ohlc.get("high"),
            "low":   ohlc.get("low"),
            "close": ohlc.get("close"),
        },
        "quote_time": normalized_qt,
    }


class DerivativesGenerator:
    """
    v2 generator (today-only single DB read):
      - resolve equity_ref from symbol
      - fetch spot + FUT + options metadata from DB
      - fetch ALL broker quotes in ONE call
      - fetch ALL today's rows once (today <= now)
      - compute derived in-memory
      - persist raw + derived
    """

    def __init__(self, api_key: str, access_token: str):
        self.kite = KiteConnectService(api_key=api_key, access_token=access_token)

    def _minute_from(self, dt: Optional[datetime] = None) -> Tuple[datetime, datetime]:
        if dt is None:
            now = datetime.now(IST)
        else:
            now = dt.astimezone(IST) if dt.tzinfo is not None else dt  # assume naive IST
        minute = now.replace(second=0, microsecond=0)
        naive = minute.replace(tzinfo=None)
        return minute, naive

    def _fetch_today_rows(self, *, symbol: str, asof_naive: datetime) -> List[DerivativesChainSchema]:
        """
        One DB read:
          fetch ALL rows for today (<= asof), chronological (ascending).
        """
        try:
            return DerivativesChainSchema.fetch_recent_today_for_symbol_before_time(
                symbol=symbol,
                t=asof_naive,
                limit=10000,
                ascending=True,
            ) or []
        except Exception:
            logger.exception("DERIV: today history fetch failed for %s @ %s", symbol, asof_naive)
            return []

    def _resolve_chain_inputs(
        self,
        symbol: str,
        ts_naive: datetime,
    ) -> Tuple[Optional[str], Optional[SymbolSchema], Optional[SymbolSchema], List[SymbolSchema]]:
        """
        Resolve:
          - chain_symbol (equity ref)
          - spot symbol (EQ)
          - current future
          - current option chain for future expiry
        Returns (chain_symbol, spot_sym, fut, opts)
        """
        sym = SymbolSchema.fetch_symbol(symbol)
        if not sym:
            logger.warning("DERIV_SKIP unknown symbol=%s (no SymbolSchema)", symbol)
            return None, None, None, []

        if sym.type not in ("EQ", "FUT"):
            logger.warning("DERIV_SKIP symbol=%s type=%s (not EQ/FUT)", symbol, sym.type)
            return None, None, None, []

        equity_ref = sym.equity_ref if sym.type == "FUT" else sym.symbol
        chain_symbol = equity_ref

        spot_sym = SymbolSchema.fetch_symbol(equity_ref)
        if not spot_sym:
            logger.warning("DERIV_SKIP cannot fetch spot symbol equity_ref=%s", equity_ref)
            return chain_symbol, None, None, []

        fut = SymbolSchema.fetch_current_future(equity_ref, ts_naive.date())
        if not fut:
            logger.warning("DERIV_SKIP no current FUT equity_ref=%s asof=%s", equity_ref, ts_naive.date())
            return chain_symbol, spot_sym, None, []

        opts = SymbolSchema.fetch_current_option_chain(equity_ref, ts_naive.date()) or []
        opts = [o for o in opts if getattr(o, "expiry", None) == getattr(fut, "expiry", None)]

        if not opts:
            logger.warning(
                "DERIV_SKIP no options equity_ref=%s expiry=%s asof=%s",
                equity_ref,
                getattr(fut, "expiry", None),
                ts_naive.date(),
            )
            return chain_symbol, spot_sym, fut, []

        return chain_symbol, spot_sym, fut, opts

    def _fetch_all_quotes_once(
        self,
        spot_sym: SymbolSchema,
        fut: SymbolSchema,
        opts: List[SymbolSchema],
        ts_aware: datetime,
    ) -> Tuple[Optional[float], Dict[str, Any], Dict[str, Any]]:
        """
        Single broker quote call for:
          - spot
          - future
          - all option contracts
        Returns:
          spot_price, raw_fut, raw_opt_map
        """
        try:
            keys: List[str] = []
            seen = set()

            def add_key(k: str):
                if k and k not in seen:
                    seen.add(k)
                    keys.append(k)

            spot_key = _quote_key(spot_sym)
            fut_key = _quote_key(fut)

            add_key(spot_key)
            add_key(fut_key)

            opt_key_to_sym: Dict[str, SymbolSchema] = {}
            for o in opts:
                k = _quote_key(o)
                add_key(k)
                opt_key_to_sym[k] = o

            t0 = time.perf_counter()
            qmap = self.kite.fetch_quote(keys) or {}
            logger.debug(
                "DERIV: ONE-CALL quote fetch keys=%s took %.1fms",
                len(keys),
                (time.perf_counter() - t0) * 1000,
            )

            # spot
            spot_q = qmap.get(spot_key, {}) or {}
            spot_price = spot_q.get("last_price")

            # future
            fut_q = qmap.get(fut_key, {}) or {}
            raw_fut = _build_quote_record(fut, fut_q, ts_aware)

            # options
            raw_opt: Dict[str, Any] = {}
            for k, o in opt_key_to_sym.items():
                q = qmap.get(k, {}) or {}
                rec = _build_quote_record(o, q, ts_aware)
                opt_key = f"{int(o.strike_price)}_{o.type}"  # e.g. "1500_CE"
                raw_opt[opt_key] = rec

            return spot_price, raw_fut, raw_opt

        except Exception:
            logger.exception(
                "DERIV: error in one-call quote fetch for spot=%s fut=%s opt_count=%s",
                getattr(spot_sym, "symbol", "?"),
                getattr(fut, "symbol", "?"),
                len(opts or []),
            )
            return None, {}, {}

    def generate(self, symbol: str, as_of_time: Optional[datetime] = None) -> Dict[str, Any]:
        ts_aware, ts_naive = self._minute_from(as_of_time)

        chain_symbol, spot_sym, fut, opts = self._resolve_chain_inputs(symbol, ts_naive)
        if not chain_symbol or not spot_sym or not fut or not opts:
            return {"raw": None, "derived": None}

        # -----------------------------
        # ONE broker call for spot+future+options
        # -----------------------------
        spot, raw_fut, raw_opt = self._fetch_all_quotes_once(
            spot_sym=spot_sym,
            fut=fut,
            opts=opts,
            ts_aware=ts_aware,
        )

        raw_payload: Dict[str, Any] = {
            "spot_price": spot,
            "future": raw_fut,
            "options": raw_opt,
        }

        # -----------------------------
        # Compute derived (single DB read + current raw)
        # -----------------------------
        derived_cfg = DERIVATIVES_CONFIG.derived
        os_cfg = derived_cfg.option_sentiment

        windows_cfg = os_cfg.windows
        ladder_window = derived_cfg.option_ladder.window
        top_n = derived_cfg.options_lite.top_n

        opt_sent_atm_window = os_cfg.atm_window
        opt_sent_notional_floor = os_cfg.notional_floor
        opt_sent_min_contracts_floor = os_cfg.min_contracts_floor

        today_rows = self._fetch_today_rows(symbol=chain_symbol, asof_naive=ts_naive)

        samples: List[Dict[str, Any]] = []
        for r in (today_rows or []):
            try:
                if getattr(r, "snapshot_time", None) is None:
                    continue
                rr = getattr(r, "raw", None)
                if not isinstance(rr, dict):
                    continue
                samples.append({"snapshot_time": r.snapshot_time, "raw": rr})
            except Exception:
                continue

        samples.append({"snapshot_time": ts_naive, "raw": raw_payload})

        derived_payload: Optional[Dict[str, Any]] = None
        try:
            derived_payload = build_derived_from_day(
                samples=samples,
                asof=ts_naive,
                windows=windows_cfg,
                ladder_window=ladder_window,
                opt_sent_atm_window=opt_sent_atm_window,
                opt_sent_notional_floor=opt_sent_notional_floor,
                opt_sent_min_contracts_floor=opt_sent_min_contracts_floor,
                top_n=top_n,
            )
        except Exception:
            logger.exception("DERIV: build_derived_from_day failed for %s @ %s", chain_symbol, ts_naive)
            derived_payload = None

        # -----------------------------
        # Persist (raw + derived)
        # -----------------------------
        try:
            rec = DerivativesChainSchema(
                symbol=chain_symbol,
                snapshot_time=ts_naive,
                raw=raw_payload,
                derived=derived_payload,
            )
            DerivativesChainSchema.create(rec)
        except Exception:
            logger.exception("DERIV: failed to persist derivativeschain_v2 for %s @ %s", chain_symbol, ts_naive)

        return {
            "symbol": chain_symbol,
            "snapshot_time": ts_naive.isoformat(),
            "raw": raw_payload,
            "derived": derived_payload,
        }
    