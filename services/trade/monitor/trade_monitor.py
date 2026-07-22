#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
services/monitor/trade_monitor.py

Lifecycle-style trade monitor.

Responsibilities:
- monitor existing filled trades
- refresh live LTP
- update MTM / PnL / max-min
- update MTM / PnL / max-min
- manage one adaptive target and one adaptive stop via trade_management JSON
- set exit intent only: exit_status=READY
- keep executor responsible for actual order placement/fill finalization

Design:
- No eval
- Explicit ordered monitor checks
- trade_management JSON is the only active target/SL source
- Legacy target/SL columns are retained only for DB compatibility
- Optional source signal context via user_trade.signal_id
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config import AppConfig
from configs.trade_config import TRADE_CONFIG
from configs.monitor_config import MONITOR_CONFIG
from configs.execution_config import EXECUTION_CONFIG
from enums.enums import TradeType, EntryStatus, ExitStatus, TradePosture

from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema

from services.zerodha.kiteconnect_service import KiteConnectService
from services.signals.signal_helper import SignalHelper
from services.audit.auditlog import write_auditlog
from services.trade.monitor.trademon_helper import TradeMonHelper, extract_underlying_atr

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


# =============================================================================
# small helpers
# =============================================================================

def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _to_ist_naive(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            try:
                ts = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
    if not isinstance(ts, datetime):
        return None
    try:
        if ts.tzinfo is not None:
            ts = ts.astimezone(IST)
        return ts.replace(tzinfo=None)
    except Exception:
        return ts.replace(tzinfo=None) if isinstance(ts, datetime) else None


def _enum_str(x: Any) -> str:
    v = getattr(x, "value", x)
    return str(v or "").upper().strip()


def _trade_side_str(v: Any) -> str:
    if isinstance(v, TradeType):
        return v.value
    s = _enum_str(v)
    return "BUY" if s == "BUY" else "SELL"


def _entry_status(ut: Any) -> str:
    return _enum_str(getattr(ut, "entry_status", EntryStatus.CREATED.value))


def _exit_status(ut: Any) -> str:
    return _enum_str(getattr(ut, "exit_status", ExitStatus.NONE.value))


def _to_optional_price(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    v = d(x)
    return v if v > 0 else None


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_path(dct: Any, path: List[str], default: Any = None) -> Any:
    cur = dct
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _first_number(*values: Any) -> Optional[Decimal]:
    for value in values:
        if value is None:
            continue
        val = _to_optional_price(value)
        if val is not None and val > 0:
            return val
    return None


def _normalize_snapshot_dict(snap: Any) -> Dict[str, Any]:
    if snap is None:
        return {}
    try:
        if hasattr(snap, "to_db_dict"):
            return snap.to_db_dict()
    except Exception:
        pass
    try:
        if hasattr(snap, "model_dump"):
            return snap.model_dump(mode="python")
    except Exception:
        pass
    try:
        if isinstance(snap, dict):
            return snap
    except Exception:
        pass
    return getattr(snap, "__dict__", {}) or {}


def _infer_exchange_for_trade(ut: Any) -> str:
    inst = _enum_str(getattr(ut, "instrument_type", None))
    if inst in ("FUT", "CE", "PE"):
        return "NFO"
    return "NSE"


def _quote_key(ut: Any) -> str:
    exch = None

    try:
        esnap = getattr(ut, "entry_snapshot", None) or {}
        if isinstance(esnap, dict):
            exch = esnap.get("exchange") or esnap.get("exch")
    except Exception:
        exch = None

    exch = (exch or _infer_exchange_for_trade(ut) or "NSE").upper()
    sym = str(getattr(ut, "symbol", "") or "").strip()

    return f"{exch}:{sym}"


def _audit_trade_monitor(ctx: "TradeMonitorContext", updates: Dict[str, Any]) -> None:
    """Write a compact, best-effort monitor audit after the trade update.

    Audit must never sit on the critical trade-update path.  Large copies of
    trade_management/active evidence are deliberately omitted because the
    authoritative state already lives on user_trades.
    """
    try:
        exit_status = _enum_str(updates.get("exit_status"))
        exit_reason = str(updates.get("exit_reason") or "").strip()
        action = "EXIT_READY" if exit_status == ExitStatus.READY.value else "MONITOR"
        reason_code = exit_reason or "monitor_update"
        res = ctx.monitor_resolution

        updated_management = _as_dict(updates.get("trade_management"))
        management = updated_management or _as_dict(ctx.trade_management)
        management_keys = (
            "version",
            "mode",
            "posture",
            "target_expansion_allowed",
            "trail_mode",
            "exit_pressure",
            "active_evidence_action",
            "active_evidence_reason_code",
            "current_target_price",
            "current_stop_price",
            "target_r_multiple",
            "stop_r_multiple",
            "current_profit_r",
            "mfe_profit_r",
            "risk_reduced",
            "expansion_count",
            "last_target_hit_price",
            "max_favorable_price",
            "last_signal_stage",
            "last_signal_confidence",
            "last_signal_quality",
            "last_update_reason",
        )
        compact_management = {
            key: management.get(key)
            for key in management_keys
            if key in management
        }

        write_auditlog(
            entity_type="TRADE",
            entity_id=getattr(ctx.trade, "id", None),
            symbol=getattr(ctx.trade, "symbol", None),
            userid=getattr(ctx.trade, "userid", None),
            evaluation_stage="TRADE_MONITOR",
            previous_state=_exit_status(ctx.trade),
            new_state=exit_status or _exit_status(ctx.trade),
            action=action,
            reason_code=reason_code,
            reason_text=updates.get("exit_rule") or reason_code,
            confidence=None,
            ts=ctx.last_time,
            payload_json={
                "signal_id": getattr(ctx.trade, "signal_id", None),
                "instrument_type": ctx.instrument_type,
                "side": ctx.side,
                "last_price": ctx.last_price,
                "entry_price": ctx.entry_price,
                "basis_price": ctx.basis_price,
                "pnl_per_unit": ctx.pnl_per_unit,
                "pnl_value": ctx.pnl_value,
                "management": compact_management,
                "monitor_resolution": {
                    "preferred_lifecycle": res.preferred_lifecycle,
                    "preferred_signal_id": res.preferred_signal_id,
                    "preferred_side": res.preferred_side,
                    "action": res.action,
                    "same_side_strength": res.same_side_strength,
                    "opposite_side_strength": res.opposite_side_strength,
                    "same_side_actionable": res.same_side_actionable,
                    "opposite_side_actionable": res.opposite_side_actionable,
                    "opposite_side_lifecycle": res.opposite_side_lifecycle,
                },
                "update_fields": sorted(str(key) for key in updates.keys()),
                "exit_status": exit_status or None,
                "exit_reason": exit_reason or None,
                "exit_rule": updates.get("exit_rule"),
            },
        )
    except Exception:
        logger.debug("trade monitor audit failed", exc_info=True)


# =============================================================================
# monitor policy helpers
# =============================================================================

def _trade_extras() -> Dict[str, Any]:
    return MONITOR_CONFIG.model_dump(mode="python")


def _monitor_use_snapshot() -> bool:
    """Return True when replay/snapshot pricing is active.

    EXECUTION_CONFIG.use_snapshot is the single replay switch for the trade
    pipeline.  Replay_summary forces this flag before it runs generator,
    executor, and monitor.  Keeping one switch avoids drift where executor is
    in replay mode but monitor still reads live broker quote timestamps.
    """
    return bool(EXECUTION_CONFIG.use_snapshot)


def _monitor_use_live_quotes() -> bool:
    return bool(MONITOR_CONFIG.use_live_quotes)


def _instrument_type(ut: Any) -> str:
    inst = _enum_str(getattr(ut, "instrument_type", "EQ") or "EQ")
    if inst in ("EQUITY", "CASH"):
        return "EQ"
    if inst in ("FUTURE", "FUTURES"):
        return "FUT"
    if inst in ("CALL", "CE"):
        return "CE"
    if inst in ("PUT", "PE"):
        return "PE"
    return inst or "EQ"


def _is_manual_trade(ut: Any) -> bool:
    signal_id = str(getattr(ut, "signal_id", "") or "").upper().strip()
    source = _enum_str(getattr(ut, "source", "") or "")
    return signal_id.startswith("MANUAL:") or source == "MANUAL"


def _trade_management_cfg() -> Any:
    return getattr(MONITOR_CONFIG, "trade_management", None)


def _tm_cfg_value(name: str) -> Any:
    cfg = _trade_management_cfg()
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.{name} is required")
    return getattr(cfg, name)


def _tm_cfg_bool(name: str) -> bool:
    return bool(_tm_cfg_value(name))


def _group_management_cfg() -> Any:
    cfg = _trade_management_cfg()
    group_cfg = getattr(cfg, "group_management", None) if cfg is not None else None
    if group_cfg is None:
        raise AttributeError("MONITOR_CONFIG.trade_management.group_management is required")
    return group_cfg


def _group_management_enabled() -> bool:
    return bool(getattr(_group_management_cfg(), "enabled"))


def _group_key(ut: Any) -> Optional[Tuple[str, str]]:
    userid = str(getattr(ut, "userid", "") or "").strip()
    signal_id = str(getattr(ut, "signal_id", "") or "").strip()
    if not userid or not signal_id or signal_id.upper().startswith("MANUAL:"):
        return None
    return userid, signal_id


def _select_group_reference_ids(trades: List[Any]) -> Dict[Tuple[str, str], int]:
    if not _group_management_enabled():
        return {}
    priority = [str(x or "").upper().strip() for x in list(getattr(_group_management_cfg(), "reference_priority", []) or [])]
    if not priority:
        raise ValueError("group_management.reference_priority must not be empty")
    rank = {inst: idx for idx, inst in enumerate(priority)}
    grouped: Dict[Tuple[str, str], List[Any]] = {}
    for ut in trades:
        key = _group_key(ut)
        if key is not None:
            grouped.setdefault(key, []).append(ut)

    selected: Dict[Tuple[str, str], int] = {}
    for key, rows in grouped.items():
        candidates = [r for r in rows if _instrument_type(r) in rank]
        if not candidates:
            continue
        candidates.sort(key=lambda r: (rank[_instrument_type(r)], int(getattr(r, "id", 0) or 0)))
        ref_id = int(getattr(candidates[0], "id", 0) or 0)
        if ref_id > 0:
            selected[key] = ref_id
    return selected


def _signal_exit_cfg() -> Any:
    cfg = getattr(MONITOR_CONFIG, "signal_exit", None)
    if cfg is None:
        raise AttributeError("MONITOR_CONFIG.signal_exit is required")
    return cfg


def _signal_exit_enabled() -> bool:
    cfg = _signal_exit_cfg()
    if not hasattr(cfg, "enabled"):
        raise AttributeError("MONITOR_CONFIG.signal_exit.enabled is required")
    return bool(cfg.enabled)


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    val = _to_optional_price(value)
    return val if val is not None and val > 0 else None


def _extract_atr_from_snapshot_dict(snapshot_dict: Dict[str, Any]) -> Optional[Decimal]:
    snap = snapshot_dict or {}
    candidates = [
        (((snap.get("indicators") or {}).get("atr") or {}).get("value")),
    ]
    for value in candidates:
        val = _decimal_or_none(value)
        if val is not None and val > 0:
            return val
    return None


def _json_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(d(value))
    except Exception:
        return None


def _tm_as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            obj = value.model_dump(mode="python", exclude_none=True)
            return dict(obj) if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _adaptive_target_price(*, side: str, basis_price: Decimal, atr: Decimal, multiplier: Decimal) -> Optional[Decimal]:
    basis = d(basis_price)
    dist = d(atr) * d(multiplier)
    if basis <= 0 or dist <= 0:
        return None
    return basis + dist if side == "BUY" else basis - dist


def _build_default_trade_management(
    *,
    side: str,
    basis_price: Decimal,
    snapshot_dict: Dict[str, Any],
    fallback_protective_sl: Optional[Decimal],
    instrument_type: str = "EQ",
) -> Dict[str, Any]:
    return TradeMonHelper.initialize_trade_management(
        side=side,
        instrument_type=instrument_type,
        entry_price=basis_price,
        underlying_atr=extract_underlying_atr(snapshot_dict),
        asof_time=_now_ist(),
    )


def _normalize_trade_management_for_monitor(
    *,
    raw: Any,
    side: str,
    basis_price: Decimal,
    snapshot_dict: Dict[str, Any],
    fallback_protective_sl: Optional[Decimal],
    instrument_type: str = "EQ",
) -> Dict[str, Any]:
    return TradeMonHelper.normalize_trade_management(
        raw=raw,
        side=side,
        instrument_type=instrument_type,
        entry_price=basis_price,
        underlying_atr=extract_underlying_atr(snapshot_dict),
        asof_time=_now_ist(),
    )


class PriceProvider:
    """
    Batch LTP fetcher using KiteConnect.quote().
    """

    def __init__(self):
        self._svc: Optional[KiteConnectService] = None
        self._init_err: Optional[str] = None

    def _ensure(self) -> Optional[KiteConnectService]:
        if self._svc:
            return self._svc

        if self._init_err:
            return None

        data_userid = AppConfig.DATA_USER


        if not data_userid:
            self._init_err = "DATA_USER missing"
            logger.warning("PriceProvider: DATA_USER missing")
            return None

        user = UserSchema.fetch_user(str(data_userid))
        if not user:
            self._init_err = f"DATA_USER not found: {data_userid}"
            logger.warning("PriceProvider: DATA_USER not found: %s", data_userid)
            return None

        api_key = getattr(user, "apikey", None)
        access_token = getattr(user, "access_token", None)

        if not api_key or not access_token:
            self._init_err = "DATA_USER missing apikey/access_token"
            logger.warning("PriceProvider: DATA_USER missing apikey/access_token")
            return None

        try:
            self._svc = KiteConnectService(
                api_key=api_key,
                access_token=access_token,
            )
            return self._svc
        except Exception as exc:
            self._init_err = str(exc)[:200]
            logger.warning("PriceProvider init failed: %s", self._init_err)
            return None

    def fetch_ltps(self, quote_keys: List[str]) -> Dict[str, Tuple[Optional[Decimal], datetime]]:
        out: Dict[str, Tuple[Optional[Decimal], datetime]] = {}
        ts_now = _now_ist()

        if not _monitor_use_live_quotes():
            for key in quote_keys:
                out[key] = (None, ts_now)
            return out

        svc = self._ensure()
        if not svc or not quote_keys:
            for key in quote_keys:
                out[key] = (None, ts_now)
            return out

        try:
            data = svc.fetch_quote(quote_keys) or {}
            if not isinstance(data, dict):
                logger.warning("PriceProvider: non-dict quote response=%s", type(data).__name__)
                for key in quote_keys:
                    out[key] = (None, ts_now)
                return out

            for key in quote_keys:
                rec = data.get(key)

                if not isinstance(rec, dict):
                    logger.warning(
                        "PriceProvider: quote key missing | key=%s response_keys=%s",
                        key,
                        list(data.keys()),
                    )
                    out[key] = (None, ts_now)
                    continue

                raw_ltp = rec.get("last_price")
                ltp = _to_optional_price(raw_ltp)

                rec_ts = rec.get("timestamp") or rec.get("last_trade_time") or ts_now
                if not isinstance(rec_ts, datetime):
                    rec_ts = ts_now

                out[key] = (ltp, rec_ts)

            return out

        except Exception as exc:
            logger.warning("PriceProvider.fetch_ltps failed: %s", str(exc)[:200])
            for key in quote_keys:
                out[key] = (None, ts_now)
            return out


# =============================================================================
# decision state
# =============================================================================

@dataclass
class MonitorResolution:
    preferred_lifecycle: str = ""
    preferred_signal_id: str = ""
    preferred_side: str = ""
    action: str = "WATCH"
    same_side_strength: float = 0.0
    opposite_side_strength: float = 0.0
    same_side_actionable: bool = False
    opposite_side_actionable: bool = False
    opposite_side_lifecycle: str = ""
    summary: str = ""


@dataclass
class TradeMonitorContext:
    trade: Any
    side: str
    instrument_type: str
    last_price: Decimal
    last_time: datetime
    entry_price: Decimal
    quantity: Decimal
    basis_price: Decimal
    pnl_per_unit: Decimal
    pnl_value: Decimal

    trade_management: Dict[str, Any]

    managed_stop_price: Optional[Decimal]
    managed_target_price: Optional[Decimal]

    max_price: Decimal
    min_price: Decimal

    intraday_only: bool
    intraday_exit_time: datetime

    source_signal: Optional[SignalSchema]
    signal_meta: Dict[str, Any]
    snapshot: Optional[SnapshotSchema]
    monitor_resolution: MonitorResolution

    group_role: str = "STANDALONE"
    group_reference_trade_id: Optional[int] = None
    group_reference_instrument: Optional[str] = None
    group_reference_symbol: Optional[str] = None


# =============================================================================
# monitor
# =============================================================================

class TradeMonitor:
    def __init__(self):
        self._cutoff_hms = self._load_intraday_cutoff_hms()
        self._pp = PriceProvider()
        self._pass_ltp_map: Dict[str, Tuple[Optional[Decimal], datetime]] = {}
        self._pass_asof_time: Optional[datetime] = None
        self._pass_group_reference_ids: Dict[Tuple[str, str], int] = {}

        self._dt_keys = {
            "last_time",
            "max_time",
            "min_time",
            "exit_time",
            "exit_intent_time",
        }

        self._float_keys = {
            "last_price",
            "last_pnl",
            "last_pnl_value",
            "max_price",
            "min_price",
            "exit_price",
            "exit_pnl",
        }

    # -----------------------------------------------------------------
    # config
    # -----------------------------------------------------------------
    def _load_intraday_cutoff_hms(self) -> Tuple[int, int, int]:
        default = (15, 20, 0)

        try:
            raw = str(MONITOR_CONFIG.intraday_cutoff_time or "").strip()

            if not raw:
                return default

            t = dtime.fromisoformat(raw)
            return (t.hour, t.minute, t.second)

        except Exception:
            return default

    def _monitor_policy(self) -> Dict[str, Any]:
        try:
            return MONITOR_CONFIG.setup_policy or {}
        except Exception:
            return {}

    def _policy_num(self, key: str, default: Decimal) -> Decimal:
        policy = self._monitor_policy()
        return d(policy.get(key, default))

    def _policy_int(self, key: str, default: int) -> int:
        policy = self._monitor_policy()
        try:
            return int(policy.get(key, default))
        except Exception:
            return default

    # -----------------------------------------------------------------
    # public
    # -----------------------------------------------------------------
    def run_once(self, snapshot_time: Optional[datetime] = None) -> int:
        return self.monitor(snapshot_time=snapshot_time)

    def monitor(self, snapshot_time: Optional[datetime] = None) -> int:
        """
        Monitor open filled positions.

        UserTradeSchema.fetch_open_positions() is expected to return:
        - entry_status = FILLED
        - exit_status not terminal/filled
        """
        trades = UserTradeSchema.fetch_open_positions()

        if not trades:
            logger.info("TradeMonitor: no open positions to monitor.")
            return 0

        self._pass_group_reference_ids = _select_group_reference_ids(trades)

        # Reference FUT/EQ must be evaluated before its followers so any new
        # group stop/target state is available to the option/EQ projections in
        # this same monitor pass. Preserve package chronology using the earliest
        # entry time across all legs, not the individual leg entry timestamp.
        group_first_time: Dict[Tuple[str, str], datetime] = {}
        for row in trades:
            row_key = _group_key(row)
            if row_key is None:
                continue
            row_time = _to_ist_naive(getattr(row, "entry_time", None)) or datetime.min
            previous = group_first_time.get(row_key)
            if previous is None or row_time < previous:
                group_first_time[row_key] = row_time

        def _group_sort_key(ut: Any) -> Tuple[Any, str, int, int]:
            key = _group_key(ut)
            ref_id = self._pass_group_reference_ids.get(key) if key is not None else None
            role_rank = 0 if ref_id and int(getattr(ut, "id", 0) or 0) == ref_id else 1
            entry_sort_time = group_first_time.get(key) if key is not None else None
            entry_sort_time = entry_sort_time or _to_ist_naive(getattr(ut, "entry_time", None)) or datetime.min
            group_label = f"{key[0]}:{key[1]}" if key is not None else f"STANDALONE:{getattr(ut, 'id', 0)}"
            return (entry_sort_time, group_label, role_rank, int(getattr(ut, "id", 0) or 0))

        trades = sorted(trades, key=_group_sort_key)
        quote_keys = [_quote_key(ut) for ut in trades]
        ltp_map = {} if _monitor_use_snapshot() else self._pp.fetch_ltps(quote_keys)
        self._pass_ltp_map = dict(ltp_map)
        self._pass_asof_time = _to_ist_naive(snapshot_time)

        updated_count = 0

        for ut, quote_key in zip(trades, quote_keys):
            try:
                # A previous primary-leg update in this same monitor pass may
                # have already marked this sibling leg READY for exit.  Re-read
                # the row before evaluating stale in-memory state so we do not
                # overwrite RELATED_PRIMARY_EXIT with an independent decision.
                current_ut = UserTradeSchema.fetch_user_trade_by_id(getattr(ut, "id", None))
                if current_ut is not None:
                    ut = current_ut
                if _exit_status(ut) in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value, ExitStatus.FILLED.value):
                    logger.info(
                        "TradeMonitor: skip already exiting trade_id=%s symbol=%s exit_status=%s",
                        getattr(ut, "id", None),
                        getattr(ut, "symbol", None),
                        _exit_status(ut),
                    )
                    continue

                default_ltp_time = _to_ist_naive(snapshot_time) or _now_ist()
                ltp, ltp_time = ltp_map.get(quote_key, (None, default_ltp_time))
                snapshot = None

                if _monitor_use_snapshot() or ltp is None:
                    snapshot = self._fetch_snapshot_fallback(ut, asof_time=snapshot_time)
                    if snapshot:
                        snap_px = self._price_from_snapshot_for_trade(ut, snapshot)
                        if snap_px is not None and snap_px > 0:
                            ltp = snap_px
                            snap_ts = getattr(snapshot, "snapshot_time", None)
                            if isinstance(snap_ts, datetime):
                                ltp_time = snap_ts

                # Even when live quote succeeds, keep the latest equity snapshot
                # available for resolver/materiality decisions. The quote remains
                # the source of price in live mode.
                if snapshot is None:
                    snapshot = self._fetch_snapshot_fallback(ut, asof_time=snapshot_time)

                if ltp is None:
                    logger.info(
                        "TradeMonitor: skip no price | trade_id=%s symbol=%s quote_key=%s",
                        getattr(ut, "id", None),
                        getattr(ut, "symbol", None),
                        quote_key,
                    )
                    continue

                group_key = _group_key(ut)
                reference_trade_id = self._pass_group_reference_ids.get(group_key) if group_key is not None else None
                trade_id = int(getattr(ut, "id", 0) or 0)
                group_role = (
                    "REFERENCE" if reference_trade_id and trade_id == reference_trade_id
                    else "FOLLOWER" if reference_trade_id
                    else "STANDALONE"
                )
                reference_trade = (
                    ut if group_role == "REFERENCE"
                    else UserTradeSchema.fetch_user_trade_by_id(reference_trade_id) if reference_trade_id
                    else None
                )

                source_signal = self._fetch_source_signal(ut)
                ctx = self._build_context(
                    ut=ut,
                    last_price=ltp,
                    last_time=ltp_time,
                    snapshot=snapshot,
                    source_signal=source_signal,
                    group_role=group_role,
                    group_reference_trade=reference_trade,
                )

                updates = self._evaluate_trade(ctx)

                if updates:
                    payload = self._normalize_updates(updates)
                    persisted = UserTradeSchema.update_user_trade_by_id(ut.id, payload)
                    if persisted is None:
                        logger.error(
                            "TradeMonitor: update failed trade_id=%s symbol=%s; audit skipped",
                            getattr(ut, "id", None),
                            getattr(ut, "symbol", None),
                        )
                        continue

                    updated_count += 1
                    updated_count += self._mark_sibling_legs_on_primary_exit(ctx, payload)

                    # Audit is deliberately after the authoritative user_trade
                    # update and sibling-exit persistence.  Audit contention
                    # must not delay or prevent trade-state updates.
                    _audit_trade_monitor(ctx, payload)

                    logger.info(
                        "TradeMonitor: updated trade_id=%s symbol=%s side=%s last=%s pnl=%s exit_status=%s reason=%s",
                        getattr(ut, "id", None),
                        getattr(ut, "symbol", None),
                        ctx.side,
                        ctx.last_price,
                        updates.get("last_pnl_value"),
                        updates.get("exit_status"),
                        updates.get("exit_reason"),
                    )

            except Exception:
                logger.exception(
                    "TradeMonitor: error trade_id=%s symbol=%s",
                    getattr(ut, "id", "?"),
                    getattr(ut, "symbol", "?"),
                )

        logger.info("TradeMonitor: pass updated %d positions", updated_count)
        return updated_count


    def _refresh_group_followers_before_exit(self, ctx: TradeMonitorContext) -> None:
        """Stamp every filled sibling with this same evaluation's own price.

        Reference legs are evaluated first.  Without this refresh, replay would
        close an option at its previous snapshot price.  Each sibling is updated
        from its own quote/snapshot before RELATED_PRIMARY_EXIT is persisted.
        """
        userid = str(getattr(ctx.trade, "userid", "") or "").strip()
        signal_id = str(getattr(ctx.trade, "signal_id", "") or "").strip()
        if not userid or not signal_id:
            return
        siblings = UserTradeSchema.fetch_active_trades_for_signal(userid=userid, signal_id=signal_id)
        for sib in siblings:
            if int(getattr(sib, "id", 0) or 0) == int(getattr(ctx.trade, "id", 0) or 0):
                continue
            if _entry_status(sib) != EntryStatus.FILLED.value:
                continue
            if _exit_status(sib) in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value, ExitStatus.FILLED.value):
                continue

            try:
                price: Optional[Decimal] = None
                price_time = ctx.last_time
                snapshot = None
                if not _monitor_use_snapshot():
                    price, live_time = self._pass_ltp_map.get(_quote_key(sib), (None, ctx.last_time))
                    if isinstance(live_time, datetime):
                        price_time = live_time

                if _monitor_use_snapshot() or price is None:
                    snapshot = self._fetch_snapshot_fallback(sib, asof_time=ctx.last_time)
                    if snapshot is not None:
                        price = self._price_from_snapshot_for_trade(sib, snapshot)
                        snap_time = getattr(snapshot, "snapshot_time", None)
                        if isinstance(snap_time, datetime):
                            price_time = snap_time
                if snapshot is None:
                    snapshot = self._fetch_snapshot_fallback(sib, asof_time=ctx.last_time)
                if price is None or d(price) <= 0:
                    logger.error(
                        "TradeMonitor: group exit follower price unavailable | primary_trade_id=%s sibling_trade_id=%s symbol=%s",
                        getattr(ctx.trade, "id", None),
                        getattr(sib, "id", None),
                        getattr(sib, "symbol", None),
                    )
                    continue

                follower_ctx = self._build_context(
                    ut=sib,
                    last_price=d(price),
                    last_time=price_time,
                    snapshot=snapshot,
                    source_signal=ctx.source_signal or self._fetch_source_signal(sib),
                    group_role="FOLLOWER",
                    group_reference_trade=ctx.trade,
                )
                follower_updates = self._evaluate_group_follower(follower_ctx)
                payload = self._normalize_updates(follower_updates)
                if payload:
                    UserTradeSchema.update_user_trade_by_id(getattr(sib, "id", None), payload)
            except Exception:
                logger.exception(
                    "TradeMonitor: failed to refresh group follower before exit | primary_trade_id=%s sibling_trade_id=%s",
                    getattr(ctx.trade, "id", None),
                    getattr(sib, "id", None),
                )



    def _mark_sibling_legs_on_primary_exit(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> int:
        """Close sibling legs when a primary EQ/FUT leg exits.

        Hedged trade sets are created under one signal_id.  If a primary
        EQ/FUT leg exits because the setup/adaptive stop failed, any remaining
        CE/PE/FUT/EQ sibling must be flattened too.  Option-only exits do not
        force primary-leg closure.
        """
        if _enum_str(updates.get("exit_status")) != ExitStatus.READY.value:
            return 0

        inst = _enum_str(getattr(ctx.trade, "instrument_type", None))
        if inst not in ("EQ", "FUT"):
            return 0
        if _group_management_enabled() and ctx.group_reference_trade_id and ctx.group_role != "REFERENCE":
            return 0

        userid = str(getattr(ctx.trade, "userid", "") or "").strip()
        signal_id = str(getattr(ctx.trade, "signal_id", "") or "").strip()
        if not userid or not signal_id:
            return 0

        reason = str(updates.get("exit_reason") or "PRIMARY_LEG_EXIT").strip()
        rule = str(updates.get("exit_rule") or "primary_leg_exit").strip()
        sibling_reason = "RELATED_PRIMARY_EXIT"
        sibling_rule = f"close_sibling_legs_on_primary_exit:{reason}:{rule}"

        if _group_management_enabled() and ctx.group_role == "REFERENCE":
            self._refresh_group_followers_before_exit(ctx)

        try:
            marked = UserTradeSchema.mark_sibling_trades_exit_for_signal(
                userid=userid,
                signal_id=signal_id,
                exclude_trade_id=getattr(ctx.trade, "id", None),
                reason=sibling_reason,
                rule=sibling_rule,
                ts=ctx.last_time,
            )
        except Exception:
            logger.exception(
                "TradeMonitor: sibling-leg exit marking failed | trade_id=%s signal_id=%s",
                getattr(ctx.trade, "id", None),
                signal_id,
            )
            return 0

        for sib in marked:
            try:
                write_auditlog(
                    entity_type="TRADE",
                    entity_id=getattr(sib, "id", None),
                    symbol=getattr(sib, "symbol", None),
                    userid=getattr(sib, "userid", None),
                    evaluation_stage="TRADE_MONITOR",
                    previous_state=ExitStatus.NONE.value,
                    new_state=ExitStatus.READY.value,
                    action="EXIT_READY",
                    reason_code=sibling_reason,
                    reason_text=sibling_rule,
                    confidence=None,
                    ts=ctx.last_time,
                    payload_json={
                        "signal_id": signal_id,
                        "primary_trade_id": getattr(ctx.trade, "id", None),
                        "primary_symbol": getattr(ctx.trade, "symbol", None),
                        "primary_instrument_type": inst,
                        "primary_exit_reason": reason,
                        "primary_exit_rule": rule,
                        "sibling_trade_id": getattr(sib, "id", None),
                        "sibling_symbol": getattr(sib, "symbol", None),
                        "sibling_instrument_type": getattr(sib, "instrument_type", None),
                        "sibling_observed_exit_price": getattr(sib, "exit_price", None),
                        "group_reference_mfe_r": (ctx.trade_management or {}).get("group_mfe_r"),
                        "group_reference_mae_r": (ctx.trade_management or {}).get("group_mae_r"),
                        "group_stop_profit_r": (ctx.trade_management or {}).get("group_stop_profit_r"),
                        "group_target_r": (ctx.trade_management or {}).get("group_target_r"),
                    },
                )
            except Exception:
                logger.debug("TradeMonitor: sibling-leg audit failed", exc_info=True)

        if marked:
            logger.info(
                "TradeMonitor: marked %d sibling legs for exit | primary_trade_id=%s signal_id=%s reason=%s",
                len(marked),
                getattr(ctx.trade, "id", None),
                signal_id,
                reason,
            )
        return len(marked)

    # -----------------------------------------------------------------
    # data fetch
    # -----------------------------------------------------------------
    def _fetch_snapshot_fallback(self, ut: Any, asof_time: Optional[datetime] = None) -> Optional[SnapshotSchema]:
        try:
            equity_ref = str(getattr(ut, "equity_ref", "") or getattr(ut, "symbol", "") or "").strip()
            if not equity_ref:
                return None
            if asof_time is not None:
                return SnapshotSchema.fetch_latest_for_symbol_asof(equity_ref, asof_time)
            return SnapshotSchema.fetch_latest_for_symbol(equity_ref)
        except Exception:
            logger.exception(
                "TradeMonitor: snapshot fallback failed trade_id=%s symbol=%s",
                getattr(ut, "id", None),
                getattr(ut, "symbol", None),
            )
            return None


    def _price_from_snapshot_for_trade(self, ut: Any, snapshot: SnapshotSchema) -> Optional[Decimal]:
        inst = _instrument_type(ut)
        symbol = str(getattr(ut, "symbol", "") or "").strip().upper()

        if inst == "EQ":
            return _to_optional_price(getattr(snapshot, "close", None))

        deriv = getattr(snapshot, "derivatives", None) or {}
        if not isinstance(deriv, dict):
            return _to_optional_price(getattr(ut, "last_price", None) or getattr(ut, "entry_price", None))

        if inst == "FUT":
            fut = deriv.get("future") or {}
            if isinstance(fut, dict):
                px = fut.get("ltp") or fut.get("last_price")
                val = _to_optional_price(px)
                if val is not None and val > 0:
                    return val

        if inst in ("CE", "PE"):
            containers = [
                deriv.get("option_ladder"),
                deriv.get("options_lite"),
                deriv.get("options"),
            ]

            def scan(value: Any) -> Optional[Decimal]:
                if isinstance(value, dict):
                    # direct symbol keyed map or nested lists
                    if symbol in value and isinstance(value.get(symbol), dict):
                        px = value[symbol].get("ltp") or value[symbol].get("last_price")
                        val = _to_optional_price(px)
                        if val is not None and val > 0:
                            return val

                    for key in ("top_calls", "top_puts", "calls", "puts", "rows", "ladder"):
                        found = scan(value.get(key))
                        if found is not None:
                            return found

                    for row in value.values():
                        found = scan(row)
                        if found is not None:
                            return found

                if isinstance(value, list):
                    for row in value:
                        if isinstance(row, dict):
                            row_symbol = str(row.get("symbol") or row.get("tradingsymbol") or "").strip().upper()
                            if row_symbol == symbol:
                                px = row.get("ltp") or row.get("last_price")
                                val = _to_optional_price(px)
                                if val is not None and val > 0:
                                    return val
                return None

            for container in containers:
                found = scan(container)
                if found is not None:
                    return found

        return _to_optional_price(getattr(ut, "last_price", None) or getattr(ut, "entry_price", None))


    def _fetch_source_signal(self, ut: Any) -> Optional[SignalSchema]:
        signal_id = str(getattr(ut, "signal_id", "") or "").strip()

        if not signal_id:
            return None

        if _is_manual_trade(ut):
            return None

        try:
            return SignalSchema.fetch_by_signal_id(signal_id)
        except Exception:
            logger.exception(
                "TradeMonitor: source signal lookup failed | trade_id=%s signal_id=%s",
                getattr(ut, "id", None),
                signal_id,
            )
            return None

    # -----------------------------------------------------------------
    # context
    # -----------------------------------------------------------------
    def _build_context(
        self,
        *,
        ut: Any,
        last_price: Decimal,
        last_time: datetime,
        snapshot: Optional[SnapshotSchema],
        source_signal: Optional[SignalSchema],
        group_role: str = "STANDALONE",
        group_reference_trade: Optional[Any] = None,
    ) -> TradeMonitorContext:
        side = _trade_side_str(getattr(ut, "trade_type", "BUY"))
        instrument_type = _instrument_type(ut)
        entry_price = d(getattr(ut, "entry_price", 0) or 0)
        quantity = d(getattr(ut, "quantity", 0) or 0)

        exec_mode = _enum_str(getattr(ut, "execution_mode", "VIRTUAL"))
        exec_entry = getattr(ut, "executed_entry_price", None)

        # P&L basis must match the actual filled/virtual execution price
        # whenever it is available. Virtual executor also stamps
        # executed_entry_price, so do not restrict this to REAL trades.
        # Fall back to planned entry_price only when execution price is absent.
        if exec_entry is not None and d(exec_entry) > 0:
            basis_price = d(exec_entry)
        else:
            basis_price = entry_price

        pnl_per_unit = (d(last_price) - basis_price) if side == "BUY" else (basis_price - d(last_price))
        pnl_value = pnl_per_unit * quantity

        snap_dict_for_tm = _normalize_snapshot_dict(snapshot) if snapshot is not None else _normalize_snapshot_dict(getattr(ut, "last_snapshot", None))
        trade_management = _normalize_trade_management_for_monitor(
            raw=getattr(ut, "trade_management", None),
            side=side,
            basis_price=basis_price,
            snapshot_dict=snap_dict_for_tm,
            fallback_protective_sl=None,
            instrument_type=instrument_type,
        )

        lp = d(last_price)
        managed_stop_price = _decimal_or_none((trade_management or {}).get("current_stop_price"))
        managed_target_price = _decimal_or_none((trade_management or {}).get("current_target_price"))

        max_price = d(getattr(ut, "max_price", None) or basis_price)
        min_price = d(getattr(ut, "min_price", None) or basis_price)
        intraday_only = bool(getattr(ut, "intraday_only", False))

        try:
            cutoff = dtime(*self._cutoff_hms)
            lt = last_time if isinstance(last_time, datetime) else _now_ist()
            intraday_exit_time = lt.replace(
                hour=cutoff.hour,
                minute=cutoff.minute,
                second=cutoff.second,
                microsecond=0,
            )
        except Exception:
            intraday_exit_time = last_time if isinstance(last_time, datetime) else _now_ist()

        signal_meta = {}
        if source_signal:
            meta = _as_dict(getattr(source_signal, "meta_json", None))
            signal_meta = _as_dict(meta.get("signal"))

        monitor_resolution = self._build_monitor_resolution(
            ut=ut,
            trade_side=side,
            snapshot=snapshot,
            source_signal=source_signal,
        )

        return TradeMonitorContext(
            trade=ut,
            side=side,
            instrument_type=instrument_type,
            last_price=lp,
            last_time=last_time if isinstance(last_time, datetime) else _now_ist(),
            entry_price=entry_price,
            quantity=quantity,
            basis_price=basis_price,
            pnl_per_unit=pnl_per_unit,
            pnl_value=pnl_value,

            trade_management=trade_management,

            managed_stop_price=managed_stop_price,
            managed_target_price=managed_target_price,

            max_price=max_price,
            min_price=min_price,

            intraday_only=intraday_only,
            intraday_exit_time=intraday_exit_time,

            source_signal=source_signal,
            signal_meta=signal_meta,
            snapshot=snapshot,
            monitor_resolution=monitor_resolution,

            group_role=str(group_role or "STANDALONE").upper().strip(),
            group_reference_trade_id=(int(getattr(group_reference_trade, "id", 0) or 0) or None),
            group_reference_instrument=(_instrument_type(group_reference_trade) if group_reference_trade is not None else None),
            group_reference_symbol=(str(getattr(group_reference_trade, "symbol", "") or "").strip() or None),
        )

    def _fetch_peer_signals(self, ut: Any) -> List[SignalSchema]:
        equity_ref = str(getattr(ut, "equity_ref", "") or getattr(ut, "symbol", "") or "").strip()
        if not equity_ref:
            return []
        try:
            return SignalSchema.list_for_ui(equity_ref=equity_ref, statuses=["OPEN"], limit=20) or []
        except Exception:
            logger.exception(
                "TradeMonitor: peer signal fetch failed | trade_id=%s equity_ref=%s",
                getattr(ut, "id", None),
                equity_ref,
            )
            return []

    def _build_monitor_resolution(
        self,
        *,
        ut: Any,
        trade_side: str,
        snapshot: Optional[SnapshotSchema],
        source_signal: Optional[SignalSchema],
    ) -> MonitorResolution:
        snap_dict = _normalize_snapshot_dict(snapshot)
        peers = self._fetch_peer_signals(ut)
        if source_signal is not None and not any(
            str(getattr(p, "signal_id", "") or "") == str(getattr(source_signal, "signal_id", "") or "")
            for p in peers
        ):
            peers.append(source_signal)

        if not peers:
            return MonitorResolution(summary="No active peer signals found for monitor resolver.")

        try:
            rows = [SignalHelper.signal_to_resolver_input(p) for p in peers]
            symbol = str(getattr(ut, "equity_ref", "") or getattr(ut, "symbol", "") or "").strip().upper()
            ltp = _get_path(snap_dict, ["close"], None)
            resolved = SignalHelper.build_signal_context(
                symbol=symbol,
                ltp=float(ltp or 0.0),
                snapshot_time=getattr(snapshot, "snapshot_time", None),
                snapshot=snap_dict,
                signals=rows,
            ).get("signal_resolution", {}) or {}

            candidates = resolved.get("candidates") or []
            same_strength = 0.0
            opposite_strength = 0.0
            same_actionable = False
            opposite_actionable = False
            opposite_lifecycle = ""

            for cand in candidates:
                side = _enum_str(cand.get("side"))
                strength = float(cand.get("confidence") or 0.0)
                entry_view = _enum_str(cand.get("entry_view"))
                action = _enum_str(cand.get("signal_action"))
                state = _enum_str(cand.get("signal_state"))
                actionable = (
                    entry_view == "ENTER"
                    or (state in ("ACCEPTED", "READY") and action in ("CREATE", "PROMOTE", "REPLACE_CREATE"))
                )

                if side == trade_side:
                    if strength > same_strength:
                        same_strength = strength
                        same_actionable = actionable
                elif side in ("BUY", "SELL"):
                    if strength > opposite_strength:
                        opposite_strength = strength
                        opposite_actionable = actionable
                        opposite_lifecycle = _enum_str(cand.get("lifecycle"))

            return MonitorResolution(
                preferred_lifecycle=_enum_str(resolved.get("preferred_lifecycle")),
                preferred_signal_id=str(resolved.get("preferred_signal_id") or ""),
                preferred_side=_enum_str(resolved.get("side")),
                action=_enum_str(resolved.get("action") or "WATCH"),
                same_side_strength=same_strength,
                opposite_side_strength=opposite_strength,
                same_side_actionable=same_actionable,
                opposite_side_actionable=opposite_actionable,
                opposite_side_lifecycle=opposite_lifecycle,
                summary=str(resolved.get("summary") or ""),
            )

        except Exception:
            logger.exception(
                "TradeMonitor: monitor resolver failed | trade_id=%s symbol=%s",
                getattr(ut, "id", None),
                getattr(ut, "symbol", None),
            )
            return MonitorResolution(summary="Monitor resolver failed.")

    def _snapshot_atr(self, ctx: TradeMonitorContext) -> Optional[Decimal]:
        snap = _normalize_snapshot_dict(ctx.snapshot)
        return _first_number(
            _get_path(snap, ["indicators", "atr", "value"]),
        )

    def _trade_age_minutes(self, ctx: TradeMonitorContext) -> Optional[Decimal]:
        entry_time = getattr(ctx.trade, "entry_exec_time", None) or getattr(ctx.trade, "entry_time", None)
        if not isinstance(entry_time, datetime) or not isinstance(ctx.last_time, datetime):
            return None
        try:
            start = _to_ist_naive(entry_time)
            end = _to_ist_naive(ctx.last_time)
            if not start or not end:
                return None
            return d((end - start).total_seconds() / 60.0)
        except Exception:
            return None

    def _is_same_or_earlier_entry_tick(self, ctx: TradeMonitorContext) -> bool:
        """Prevent immediate same-tick exit after a trade is created/filled.

        The monitor should still update MTM/max-min/trade_management on the
        entry tick, but it should not set exit_status=READY from lifecycle,
        dynamic target, soft stop, or protective SL until the next snapshot/tick.
        This avoids draft/executed rows being created and closed at P&L=0.
        """
        age = self._trade_age_minutes(ctx)
        if age is None:
            return False
        return age <= Decimal("0")

    def _material_price_move(self, ctx: TradeMonitorContext) -> Tuple[bool, Decimal, Decimal]:
        move = abs(ctx.last_price - ctx.basis_price)
        pct_threshold = ctx.basis_price * (self._policy_num("min_price_move_pct", Decimal("0.15")) / Decimal("100"))
        abs_threshold = self._policy_num("min_abs_price_move", Decimal("0"))
        threshold = max(pct_threshold, abs_threshold)

        atr = self._snapshot_atr(ctx)
        if atr is not None and atr > 0:
            threshold = max(threshold, atr * self._policy_num("min_atr_move", Decimal("0.10")))

        if ctx.instrument_type in ("CE", "PE"):
            threshold = max(threshold, self._policy_num("option_min_premium_move", Decimal("0.20")))

        return (move >= threshold, move, threshold)

    def _opposite_side_confirmed(self, ctx: TradeMonitorContext) -> bool:
        res = ctx.monitor_resolution
        threshold = float(self._policy_num("opposite_strength_exit", Decimal("65")))
        gap_threshold = float(self._policy_num("opposite_strength_gap", Decimal("8")))

        if res.preferred_side != ("SELL" if ctx.side == "BUY" else "BUY"):
            return False
        if res.action not in ("ENTER", "MANAGE", "HOLD"):
            return False
        if res.opposite_side_strength < threshold:
            return False
        if (res.opposite_side_strength - res.same_side_strength) < gap_threshold:
            return False

        return True

    def _materiality_ok_for_weakening_exit(self, ctx: TradeMonitorContext) -> bool:
        age = self._trade_age_minutes(ctx)
        min_age = d(self._policy_int("min_trade_age_minutes", 3))
        if age is not None and age < min_age:
            return False

        material, _, _ = self._material_price_move(ctx)
        return material

    # -----------------------------------------------------------------
    # deterministic evaluator
    # -----------------------------------------------------------------
    def _intraday_cutoff_due(self, ctx: TradeMonitorContext) -> bool:
        """Return True when an intraday position must be flattened now.

        This check is intentionally independent of signal lifecycle and
        adaptive trade-management state.  It is an operational safety rule.
        """
        return bool(
            ctx.intraday_only
            and isinstance(ctx.last_time, datetime)
            and isinstance(ctx.intraday_exit_time, datetime)
            and ctx.last_time >= ctx.intraday_exit_time
        )

    def _evaluate_trade(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        # Intraday cutoff is fail-safe and must run before group projection,
        # signal inspection, or adaptive management.  This prevents malformed
        # optional context from keeping a live intraday position open.  The
        # reference leg still marks its siblings READY in the normal persistence
        # path; a follower can also flatten itself if its reference price/context
        # is unavailable at cutoff.
        if self._intraday_cutoff_due(ctx):
            updates: Dict[str, Any] = {}
            self._update_live_metrics(ctx, updates)
            self._update_extremes(ctx, updates)
            updates.update(
                self._exit_payload(
                    ctx,
                    reason="INTRADAY_CUTOFF",
                    rule="intraday_auto_exit_fail_safe",
                )
            )
            if ctx.snapshot is not None:
                updates["last_snapshot"] = _normalize_snapshot_dict(ctx.snapshot)
            logger.warning(
                "TradeMonitor: fail-safe intraday cutoff | trade_id=%s symbol=%s role=%s last_time=%s cutoff=%s",
                getattr(ctx.trade, "id", None),
                getattr(ctx.trade, "symbol", None),
                ctx.group_role,
                ctx.last_time,
                ctx.intraday_exit_time,
            )
            return updates

        if _group_management_enabled() and ctx.group_role == "FOLLOWER":
            return self._evaluate_group_follower(ctx)

        updates: Dict[str, Any] = {}

        self._update_live_metrics(ctx, updates)
        self._update_extremes(ctx, updates)
        self._update_trade_management(ctx, updates)
        self._maybe_expand_after_adaptive_target(ctx, updates)
        exit_update = self._evaluate_exit_intent(ctx)

        if exit_update:
            updates.update(exit_update)

        if ctx.snapshot is not None:
            updates["last_snapshot"] = _normalize_snapshot_dict(ctx.snapshot)

        return updates

    def _evaluate_group_follower(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        """Update a sibling leg without letting it independently exit the group."""
        updates: Dict[str, Any] = {}
        self._update_live_metrics(ctx, updates)
        self._update_extremes(ctx, updates)

        reference_id = ctx.group_reference_trade_id
        reference_trade = UserTradeSchema.fetch_user_trade_by_id(reference_id) if reference_id else None
        reference_tm = _tm_as_dict(getattr(reference_trade, "trade_management", None)) if reference_trade is not None else {}
        if not reference_tm:
            logger.error(
                "TradeMonitor: group follower missing reference management | trade_id=%s reference_trade_id=%s",
                getattr(ctx.trade, "id", None),
                reference_id,
            )
        elif bool(getattr(_group_management_cfg(), "map_levels_to_siblings")):
            projected = TradeMonHelper.project_group_reference_to_follower(
                reference_trade_management=reference_tm,
                follower_trade_management=ctx.trade_management,
                side=ctx.side,
                instrument_type=ctx.instrument_type,
                entry_price=ctx.basis_price,
                underlying_atr=d((ctx.trade_management or {}).get("atr_at_entry") or 0),
                reference_trade_id=reference_id,
                reference_instrument=ctx.group_reference_instrument or _instrument_type(reference_trade),
                reference_symbol=ctx.group_reference_symbol or str(getattr(reference_trade, "symbol", "") or ""),
                asof_time=ctx.last_time,
            )

            atr_unit = d(projected.get("instrument_atr") or 0)
            if atr_unit > 0:
                current_profit_r = (ctx.last_price - ctx.basis_price) / atr_unit if ctx.side == "BUY" else (ctx.basis_price - ctx.last_price) / atr_unit
                favorable = max(ctx.max_price, ctx.last_price) if ctx.side == "BUY" else min(ctx.min_price, ctx.last_price)
                follower_mfe_r = (favorable - ctx.basis_price) / atr_unit if ctx.side == "BUY" else (ctx.basis_price - favorable) / atr_unit
                projected["current_profit_r"] = float(current_profit_r)
                projected["mfe_profit_r"] = float(max(Decimal("0"), follower_mfe_r))
                projected["max_favorable_price"] = float(favorable)
            updates["trade_management"] = projected
            ctx.trade_management.clear()
            ctx.trade_management.update(projected)

        if ctx.snapshot is not None:
            updates["last_snapshot"] = _normalize_snapshot_dict(ctx.snapshot)
        return updates


    def _update_live_metrics(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        updates["last_time"] = ctx.last_time
        updates["last_price"] = ctx.last_price
        updates["last_pnl"] = ctx.pnl_per_unit
        updates["last_pnl_value"] = ctx.pnl_value

    def _update_extremes(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        ut = ctx.trade

        if ctx.last_price > ctx.max_price:
            updates["max_price"] = ctx.last_price
            updates["max_time"] = ctx.last_time
        elif getattr(ut, "max_time", None) is None:
            updates["max_time"] = ctx.last_time

        if ctx.last_price < ctx.min_price:
            updates["min_price"] = ctx.last_price
            updates["min_time"] = ctx.last_time
        elif getattr(ut, "min_time", None) is None:
            updates["min_time"] = ctx.last_time

    def _update_trade_management(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        """Evaluate lifecycle-driven adaptive posture and persist target/stop."""
        favorable_price = max(ctx.max_price, ctx.last_price) if ctx.side == "BUY" else min(ctx.min_price, ctx.last_price)
        adverse_price = min(ctx.min_price, ctx.last_price) if ctx.side == "BUY" else max(ctx.max_price, ctx.last_price)
        decision = TradeMonHelper.evaluate(
            trade=ctx.trade,
            signal=ctx.source_signal,
            side=ctx.side,
            instrument_type=ctx.instrument_type,
            entry_price=ctx.basis_price,
            last_price=ctx.last_price,
            trade_management=ctx.trade_management,
            asof_time=ctx.last_time,
            max_favorable_price=favorable_price,
            manual_trade_context=_is_manual_trade(ctx.trade),
            max_adverse_price=adverse_price,
            group_role=ctx.group_role,
            group_reference_trade_id=ctx.group_reference_trade_id,
            group_reference_instrument=ctx.group_reference_instrument,
            group_reference_symbol=ctx.group_reference_symbol,
        )
        if decision.trade_management != (ctx.trade_management or {}):
            updates["trade_management"] = decision.trade_management
            ctx.trade_management.clear()
            ctx.trade_management.update(decision.trade_management)


    def _maybe_expand_after_adaptive_target(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        """Treat target hit as a lifecycle decision point, not an unconditional exit.

        If current adaptive target is reached and the latest posture is EXPAND,
        lock most of the achieved target as the new stop and extend the target.
        If posture is not EXPAND, _evaluate_exit_intent will exit at target.
        """
        if not _tm_cfg_bool("exit_on_current_target"):
            return
        if not self._dynamic_target_hit(ctx):
            return
        if (ctx.trade_management or {}).get("posture") != TradePosture.EXPAND.value:
            return

        decision = TradeMonHelper.expand_after_target_hit(
            side=ctx.side,
            entry_price=ctx.basis_price,
            last_price=ctx.last_price,
            trade_management=ctx.trade_management,
            asof_time=ctx.last_time,
        )
        if decision.trade_management != (ctx.trade_management or {}):
            updates["trade_management"] = decision.trade_management
            ctx.trade_management.clear()
            ctx.trade_management.update(decision.trade_management)
            logger.info(
                "TradeMonitor: adaptive target expanded | trade_id=%s symbol=%s inst=%s side=%s reason=%s target=%s stop=%s",
                getattr(ctx.trade, "id", None),
                getattr(ctx.trade, "symbol", None),
                ctx.instrument_type,
                ctx.side,
                decision.reason,
                (ctx.trade_management or {}).get("current_target_price"),
                (ctx.trade_management or {}).get("current_stop_price"),
            )


    def _ensure_protective_sl_persisted(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        """Deprecated compatibility hook.

        Clean adaptive management persists the managed stop only inside
        trade_management.current_stop_price.
        """
        return


    def _stamp_target_times(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        """Deprecated compatibility hook. Target milestone times are not used.

        Adaptive target state lives in trade_management and auditlog.
        """
        return


    def _maybe_move_sl_after_t1(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        """Deprecated compatibility hook.

        Legacy fixed-target/trailing-stop logic has been removed from active
        monitor decisions. Use trademon_helper/trade_management instead.
        """
        return


    def _evaluate_exit_intent(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        """
        Return exit-intent updates if an exit condition is met.

        Phase-1 monitor policy:
        - no hedging
        - no partial exit
        - trade_management.current_target_price is the only actionable target
        - trade_management.current_stop_price is the only actionable stop
        - lifecycle deterioration exits derivatives faster than equity
        """
        ut = ctx.trade

        if _entry_status(ut) != EntryStatus.FILLED.value:
            return {}

        xs = _exit_status(ut)
        if xs in (
            ExitStatus.READY.value,
            ExitStatus.SUBMITTED.value,
            ExitStatus.FILLED.value,
        ):
            return {}

        # Keep a second cutoff guard here for direct evaluator callers.  It is
        # deliberately before the entry-tick skip and every adaptive/signal
        # branch: a position created or recovered after the cutoff must still be
        # flattened immediately.
        if self._intraday_cutoff_due(ctx):
            return self._exit_payload(
                ctx,
                reason="INTRADAY_CUTOFF",
                rule="intraday_auto_exit_fail_safe",
            )

        if _tm_cfg_bool("skip_exit_on_entry_tick") and self._is_same_or_earlier_entry_tick(ctx):
            logger.info(
                "TradeMonitor: skip entry-tick exit evaluation | trade_id=%s symbol=%s entry_time=%s last_time=%s last=%s pnl=%s",
                getattr(ut, "id", None),
                getattr(ut, "symbol", None),
                getattr(ut, "entry_time", None),
                ctx.last_time,
                ctx.last_price,
                ctx.pnl_value,
            )
            return {}

        if bool((ctx.trade_management or {}).get("mae_risk_exit_required")):
            return self._exit_payload(
                ctx,
                reason="WEAKENING_RISK_EXIT",
                rule="exit_at_observed_price_when_new_mae_stop_is_already_breached",
            )

        if (ctx.trade_management or {}).get("posture") == TradePosture.EXIT.value:
            return self._exit_payload(
                ctx,
                reason="ADAPTIVE_POSTURE_EXIT",
                rule="exit_on_adaptive_trade_posture",
            )

        # Current adaptive target is checked before signal invalidation so a
        # profitable same-cycle target is captured instead of being overwritten
        # by a signal close/replacement.
        if _tm_cfg_bool("exit_on_current_target") and self._dynamic_target_hit(ctx):
            return self._exit_payload(
                ctx,
                reason="ADAPTIVE_TARGET",
                rule="exit_on_current_adaptive_target",
            )

        signal_exit = self._signal_exit_payload(ctx)
        if signal_exit:
            return signal_exit

        if False and self._soft_stop_hit(ctx):
            return self._exit_payload(
                ctx,
                reason="SOFT_PROFIT_STOP",
                rule="exit_on_adaptive_soft_profit_stop",
            )

        # Protective SL is only a disaster/emergency guard. The preferred exit
        # path is target/signal/soft-profit protection above.
        if _tm_cfg_bool("exit_on_current_stop") and self._protective_sl_hit(ctx):
            return self._exit_payload(
                ctx,
                reason="ADAPTIVE_STOP",
                rule="exit_on_current_adaptive_stop",
            )

        return {}

    def _signal_exit_payload(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        if not _signal_exit_enabled():
            return {}

        lc = ctx.signal_meta or {}
        opp = ctx.source_signal

        if not lc and opp is None:
            return {}

        inst = ctx.instrument_type
        status = _enum_str(getattr(opp, "status", "") if opp is not None else "")
        stage = _enum_str(getattr(opp, "stage", "") if opp is not None else "")

        action = _enum_str(lc.get("signal_action") or "")
        state = _enum_str(lc.get("signal_state") or lc.get("state") or "")
        reason = str(lc.get("signal_reason") or lc.get("reason") or "").strip()

        hard_statuses = {"CLOSED", "REPLACED", "INVALIDATED", "EXPIRED", "CANCELLED"}
        hard_actions = {"INVALIDATE", "CLOSE", "EXIT", "EXPIRE"}
        hard_stages = {"REVERSED", "INVALIDATED", "CLOSED", "EXPIRED"}

        # INVALIDATE_OPPOSITE means this signal invalidated another opposite
        # signal. It should not exit trades that belong to this same signal.
        # Opposite/old trades should exit because their own signal becomes
        # REPLACED/CLOSED/INVALIDATED, not because the new signal carries this
        # transition action.
        if action == "INVALIDATE_OPPOSITE":
            return {}

        if status in hard_statuses or action in hard_actions or stage in hard_stages:
            return self._exit_payload(
                ctx,
                reason="SIGNAL_INVALIDATED",
                rule="exit_on_signal_invalidation",
                extra_reason=reason,
            )

        weakening_actions = {"DOWNGRADE"}
        weakening_stages = {"WEAKENING"}
        is_weakening = action in weakening_actions or stage in weakening_stages

        if not is_weakening:
            return {}

        # Setup-aware monitor policy:
        # Lifecycle weakening by itself is a stress signal, not an automatic exit.
        # Exit only when the opposite side is clearly stronger AND the move is
        # material enough to matter. This prevents a FUT/option from being closed
        # just because the current signal lost some local strength.
        opposite_confirmed = self._opposite_side_confirmed(ctx)
        material_ok = self._materiality_ok_for_weakening_exit(ctx)
        material, move, threshold = self._material_price_move(ctx)
        res = ctx.monitor_resolution

        if not (opposite_confirmed and material_ok):
            logger.info(
                "TradeMonitor: hold on signal weakening | trade_id=%s symbol=%s inst=%s side=%s "
                "stage=%s action=%s same_strength=%.2f opposite_strength=%.2f preferred=%s/%s "
                "material=%s move=%s threshold=%s reason=%s",
                getattr(ctx.trade, "id", None),
                getattr(ctx.trade, "symbol", None),
                inst,
                ctx.side,
                stage,
                action,
                res.same_side_strength,
                res.opposite_side_strength,
                res.preferred_lifecycle,
                res.preferred_side,
                bool(material),
                move,
                threshold,
                reason,
            )
            return {}

        if inst in ("CE", "PE"):
            return self._exit_payload(
                ctx,
                reason="SIGNAL_WEAKENED_OPTION",
                rule="exit_option_on_confirmed_opposite_signal_strength",
                extra_reason=reason,
            )

        if inst == "FUT":
            return self._exit_payload(
                ctx,
                reason="SIGNAL_WEAKENED_FUTURE",
                rule="exit_future_on_confirmed_opposite_signal_strength",
                extra_reason=reason,
            )

        # Equity remains most tolerant; confirmed opposite strength is still
        # needed before lifecycle weakening exits it.
        return self._exit_payload(
            ctx,
            reason="SIGNAL_WEAKENED_EQUITY",
            rule="exit_equity_on_confirmed_opposite_signal_strength",
            extra_reason=reason,
        )

    def _dynamic_target_hit(self, ctx: TradeMonitorContext) -> bool:
        target = _decimal_or_none((ctx.trade_management or {}).get("current_target_price"))
        if target is None or target <= 0:
            return False
        return ctx.last_price >= target if ctx.side == "BUY" else ctx.last_price <= target

    def _soft_stop_hit(self, ctx: TradeMonitorContext) -> bool:
        # Legacy fixed soft-stop concept removed in clean Phase-1.
        return False

    def _protective_sl_hit(self, ctx: TradeMonitorContext) -> bool:
        stop = _decimal_or_none((ctx.trade_management or {}).get("current_stop_price"))
        if stop is None or stop <= 0:
            return False
        return ctx.last_price <= stop if ctx.side == "BUY" else ctx.last_price >= stop

    def _stop_loss_hit(self, ctx: TradeMonitorContext) -> bool:
        # Backward-compatible wrapper for any older internal callers.
        return self._protective_sl_hit(ctx)


    def _exit_payload(
        self,
        ctx: TradeMonitorContext,
        *,
        reason: str,
        rule: str,
        extra_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        updates: Dict[str, Any] = {
            "exit_status": ExitStatus.READY.value,
            "exit_intent_time": ctx.last_time,
            "exit_time": ctx.last_time,
            "exit_price": ctx.last_price,
            "exit_pnl": ctx.pnl_value,
            "exit_reason": reason,
            "exit_rule": rule,
            "last_time": ctx.last_time,
            "last_price": ctx.last_price,
            "last_pnl": ctx.pnl_per_unit,
            "last_pnl_value": ctx.pnl_value,
        }

        # Adaptive target exits are identified through exit_reason plus
        # trade_management/auditlog.
        return updates

    # -----------------------------------------------------------------
    # normalization
    # -----------------------------------------------------------------
    def _normalize_updates(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        for key, value in updates.items():
            if value is None:
                out[key] = None
                continue

            if key in self._dt_keys:
                out[key] = _to_ist_naive(value) if isinstance(value, datetime) else value
                continue

            if isinstance(value, Decimal):
                out[key] = float(value) if key in self._float_keys else float(value)
                continue

            out[key] = value

        return out


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    logger.info("Starting Trade Monitor pass")

    try:
        count = TradeMonitor().monitor()
        logger.info("Monitor updated %d positions", count)
    except Exception:
        logger.exception("Fatal error in Trade Monitor")
        raise
