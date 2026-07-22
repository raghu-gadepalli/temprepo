from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from configs.evidence_config import EVIDENCE_CONFIG
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json
from services.evidence.evidence_score_helper import BUY, SELL, SIDES, opposite_side, require_numeric, require_path, upper
from schemas.stock_setup_state import StockSetupStateSchema


STRICT_MODEL_CONFIG = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class SetupCandidate(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    setup_label: str
    strategy: str
    side: str
    priority: int
    discovered: bool
    price_action_confirmed: bool
    price_action_strength: float
    entry_blocked: bool
    blocked_by: Optional[str] = None
    reason_code: str
    reason_text: str
    evidence_state: str
    risk_flags: List[str] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)


class SetupDiscoveryResult(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    discovered_setups: List[SetupCandidate] = Field(default_factory=list)
    confirmed_setups: List[SetupCandidate] = Field(default_factory=list)
    primary_setup: Optional[SetupCandidate] = None
    supporting_setups: List[SetupCandidate] = Field(default_factory=list)
    decision: str
    evaluator_state: str
    preferred_side: str
    reason_code: str
    reason_text: str
    blocked_by: Optional[str] = None
    price_action_confirmed: bool
    price_action_strength: float
    risk_flags: List[str] = Field(default_factory=list)


class SetupDiscoverer:
    _exhaustion_watch_memory: Dict[str, Dict[str, Any]] = {}

    """Config-driven setup discovery and price-action confirmation.

    Evidence V2 remains setup-driven. There is no resolver here:
    enabled setup helpers discover candidates, price action confirms them, local
    filters can defer, and _select() applies priority/side-conflict rules.
    """

    def __init__(
        self,
        *,
        persist_setup_state: bool = True,
        read_persistent_setup_state: bool = True,
    ) -> None:
        self.cfg = EVIDENCE_CONFIG
        self._persist_setup_state = bool(persist_setup_state)
        self._read_persistent_setup_state = bool(read_persistent_setup_state)

    def discover(self, d: Dict[str, Any]) -> SetupDiscoveryResult:
        return self.discover_from_snapshot(d)

    def discover_from_snapshot(self, d: Dict[str, Any]) -> SetupDiscoveryResult:
        self._snapshot = d
        candidates: List[SetupCandidate] = []
        for setup_name, rule in self.cfg.setup_discovery.setup_rules.items():
            if not rule.enabled:
                continue
            if setup_name == self.cfg.pattern.setup_exhaustion_reversal:
                candidates.extend(self._discover_exhaustion_reversal())
            elif setup_name == self.cfg.pattern.setup_failed_breakout:
                candidates.extend(self._discover_failed_breakout())
            elif setup_name == self.cfg.pattern.setup_range_reabsorption:
                candidates.extend(self._discover_range_reabsorption())
            elif setup_name == self.cfg.pattern.setup_accepted_breakout:
                candidates.extend(self._discover_accepted_breakout())
            else:
                raise ValueError(f"Enabled setup is not implemented in Evidence V2: {setup_name}")
        return self._select(candidates)

    def price_action_confirmation_for_side(self, d: Dict[str, Any], side: str) -> Dict[str, Any]:
        self._snapshot = d
        return self._price_action_confirmation(side)

    @staticmethod
    def _optional_path(d: Dict[str, Any], path: str) -> Any:
        cur: Any = d
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        return cur

    @staticmethod
    def _optional_numeric(d: Dict[str, Any], path: str) -> Optional[float]:
        cur: Any = d
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if cur is None:
            return None
        try:
            return float(cur)
        except Exception as exc:
            raise ValueError(f"Snapshot optional numeric path is not numeric: {path}={cur!r}") from exc

    # ------------------------------------------------------------------
    # Existing frozen setup: EXHAUSTION_REVERSAL
    # ------------------------------------------------------------------
    def _discover_exhaustion_reversal(self) -> List[SetupCandidate]:
        d = self._snapshot
        cfg = self.cfg.exhaustion_reversal
        rule = self.cfg.setup_discovery.setup_rules[self.cfg.pattern.setup_exhaustion_reversal]
        out: List[SetupCandidate] = []

        rsi = require_numeric(d, "indicators.rsi.value")
        bb_pos = require_numeric(d, "indicators.bollinger.position")
        bb_zone = upper(require_path(d, "indicators.bollinger.zone"))
        sod_pos = require_numeric(d, "market_windows.sod.close_position_in_range")
        pos_30m = require_numeric(d, "market_windows.30m.close_position_in_range")
        move_sod = require_numeric(d, "market_windows.sod.move_atr")
        move_30m = require_numeric(d, "market_windows.30m.move_atr")
        vwap_gap_pct = self._optional_numeric(d, "indicators.vwap.distance_pct")
        vwap_gap_abs = abs(vwap_gap_pct) if vwap_gap_pct is not None else None
        vwap_too_close = (
            cfg.vwap_filter_enabled
            and vwap_gap_abs is not None
            and vwap_gap_abs < cfg.min_abs_vwap_gap_pct
        )

        buy_discovered = (
            rsi <= cfg.buy_rsi_max
            and (bb_pos <= cfg.buy_bollinger_position_max or bb_zone in cfg.buy_bollinger_zones)
            and sod_pos <= cfg.buy_sod_position_max
            and pos_30m <= cfg.buy_30m_position_max
            and (move_sod <= cfg.buy_min_down_move_atr or move_30m <= cfg.buy_min_down_move_atr)
        )
        sell_discovered = (
            rsi >= cfg.sell_rsi_min
            and (bb_pos >= cfg.sell_bollinger_position_min or bb_zone in cfg.sell_bollinger_zones)
            and sod_pos >= cfg.sell_sod_position_min
            and pos_30m >= cfg.sell_30m_position_min
            and (move_sod >= cfg.sell_min_up_move_atr or move_30m >= cfg.sell_min_up_move_atr)
        )

        if buy_discovered:
            out.append(self._exhaustion_candidate_for_side(
                side=BUY,
                rule_priority=rule.priority,
                rule_strategy=rule.strategy,
                base_data={
                    "rsi": rsi,
                    "bollinger_position": bb_pos,
                    "bollinger_zone": bb_zone,
                    "sod_position": sod_pos,
                    "position_30m": pos_30m,
                    "sod_move_atr": move_sod,
                    "move_30m_atr": move_30m,
                    "vwap_gap_pct": vwap_gap_pct,
                    "vwap_available": vwap_gap_pct is not None,
                    "vwap_too_close": vwap_too_close,
                },
            ))
        if sell_discovered:
            out.append(self._exhaustion_candidate_for_side(
                side=SELL,
                rule_priority=rule.priority,
                rule_strategy=rule.strategy,
                base_data={
                    "rsi": rsi,
                    "bollinger_position": bb_pos,
                    "bollinger_zone": bb_zone,
                    "sod_position": sod_pos,
                    "position_30m": pos_30m,
                    "sod_move_atr": move_sod,
                    "move_30m_atr": move_30m,
                    "vwap_gap_pct": vwap_gap_pct,
                    "vwap_available": vwap_gap_pct is not None,
                    "vwap_too_close": vwap_too_close,
                },
            ))

        # A strong extreme candle can legitimately be only WATCH on candle N
        # because it closes near the high/low.  If the next 1-3 candles produce
        # a large reversal break, promote the watched extreme even if the current
        # RSI/BB reading has cooled and normal discovery no longer triggers.
        out.extend(self._discover_watched_exhaustion_promotions(
            rule_priority=rule.priority,
            rule_strategy=rule.strategy,
            current_base_data={
                "rsi": rsi,
                "bollinger_position": bb_pos,
                "bollinger_zone": bb_zone,
                "sod_position": sod_pos,
                "position_30m": pos_30m,
                "sod_move_atr": move_sod,
                "move_30m_atr": move_30m,
                "vwap_gap_pct": vwap_gap_pct,
                "vwap_available": vwap_gap_pct is not None,
                "vwap_too_close": vwap_too_close,
            },
        ))
        return self._enforce_exhaustion_most_adverse_watch_context(out)

    def _enforce_exhaustion_most_adverse_watch_context(self, candidates: List[SetupCandidate]) -> List[SetupCandidate]:
        """Force same-side exhaustion candidates to use the strongest WATCH extreme.

        July-9 SOLARINDS exposed a subtle candidate-selection gap: an older,
        weaker BUY WATCH could remain unblocked while a later/deeper BUY WATCH
        was correctly blocked as FIRST_MOVE_ALREADY_CONSUMED.  The selector then
        chose the older unblocked candidate and created a signal anyway.

        For one symbol/setup/side, exhaustion has only one active tradable
        context: the most adverse WATCH extreme inside the valid window.  BUY
        must use the lowest watched low; SELL must use the highest watched high.
        This helper re-evaluates every same-side candidate against that extreme
        before _select() is allowed to choose a primary setup.
        """
        if not candidates:
            return candidates

        best_by_side: Dict[str, Dict[str, Any]] = {}
        for candidate in candidates:
            if candidate.setup_label != self.cfg.pattern.setup_exhaustion_reversal:
                continue
            watch = self._candidate_watched_extreme(candidate)
            if not isinstance(watch, dict):
                continue
            side_u = upper(candidate.side)
            current = best_by_side.get(side_u)
            if current is None:
                best_by_side[side_u] = dict(watch)
            else:
                best_by_side[side_u] = self._most_adverse_exhaustion_watch(side=side_u, watches=[current, watch]) or current

        if not best_by_side:
            return candidates

        normalized: List[SetupCandidate] = []
        for candidate in candidates:
            if candidate.setup_label != self.cfg.pattern.setup_exhaustion_reversal:
                normalized.append(candidate)
                continue
            best_watch = best_by_side.get(upper(candidate.side))
            if not isinstance(best_watch, dict):
                normalized.append(candidate)
                continue
            normalized.append(self._candidate_with_watch_context(candidate, best_watch))
        return normalized

    def _candidate_watched_extreme(self, candidate: SetupCandidate) -> Optional[Dict[str, Any]]:
        data = candidate.data if isinstance(candidate.data, dict) else {}
        setup_inputs = data.get("setup_inputs") if isinstance(data.get("setup_inputs"), dict) else {}
        watch = setup_inputs.get("watched_extreme")
        if isinstance(watch, dict):
            return dict(watch)
        promotion = data.get("watch_promotion") if isinstance(data.get("watch_promotion"), dict) else {}
        watch = promotion.get("watched_extreme") if isinstance(promotion, dict) else None
        if isinstance(watch, dict):
            return dict(watch)
        return None

    def _candidate_with_watch_context(self, candidate: SetupCandidate, watch: Dict[str, Any]) -> SetupCandidate:
        side_u = upper(candidate.side)
        pa = dict(candidate.data.get("price_action") or {}) if isinstance(candidate.data, dict) else {}
        if not pa:
            return candidate

        data = dict(candidate.data or {})
        setup_inputs = dict(data.get("setup_inputs") or {})
        setup_inputs["watched_extreme"] = dict(watch)
        setup_inputs["selected_watched_extreme_policy"] = {
            "policy": "MOST_ADVERSE_VALID_WATCH_EXTREME_BY_SIDE",
            "rule": "BUY uses the lowest watched low; SELL uses the highest watched high before CREATE selection.",
            "selected_watch_snapshot_time": str(watch.get("snapshot_time")) if watch.get("snapshot_time") is not None else None,
        }

        setup_levels = self._apply_exhaustion_signal_reference(
            dict(data.get("setup_levels") or {}),
            side=side_u,
            watch=watch,
            pa=pa,
        )
        if self._watch_reference_price(side=side_u, watch=watch) is not None:
            setup_levels.update({
                "watch_snapshot_time": str(watch.get("snapshot_time")) if watch.get("snapshot_time") is not None else None,
                "selected_watched_extreme_policy": "MOST_ADVERSE_VALID_WATCH_EXTREME_BY_SIDE",
            })

        location = dict(data.get("entry_location_filter") or {})
        first_move_filter = self._first_move_consumed_filter(side=side_u, watch=watch, pa=pa)
        setup_inputs["first_move_consumed_filter"] = first_move_filter
        if not bool(first_move_filter.get("passes", True)):
            location = self._with_location_block(
                location,
                str(first_move_filter.get("code") or self.cfg.exhaustion_reversal.first_move_consumed_code),
                first_move_consumed_filter=first_move_filter,
                selected_watched_extreme_policy=setup_inputs["selected_watched_extreme_policy"],
            )

        data["setup_inputs"] = setup_inputs
        data["entry_location_filter"] = location
        data["setup_levels"] = setup_levels

        blocked = bool(location.get("blocked"))
        update: Dict[str, Any] = {
            "data": data,
            "entry_blocked": blocked,
            "blocked_by": location.get("blocked_by"),
            "risk_flags": list(location.get("risk_flags") or []),
        }
        if blocked:
            update.update({
                "reason_code": self.cfg.reason.blocked_by_location_code,
                "reason_text": (
                    f"{candidate.side} exhaustion reversal price action confirmed, but CREATE is deferred: "
                    "the selected WATCH extreme shows the first move is already consumed."
                ),
                "evidence_state": "ENTRY_DEFERRED",
            })

        # Keep the current-state table aligned with the candidate we will allow
        # _select() to see.  Without this write an older direct candidate can
        # leave stock_setup_state as CONFIRMED even though the normalized
        # candidate is now blocked by the strongest WATCH context.
        if candidate.price_action_confirmed or isinstance(self._candidate_watched_extreme(candidate), dict):
            self._write_exhaustion_candidate_state(
                side=side_u,
                watch=watch,
                pa=pa,
                location=location,
                reason_code=update.get("reason_code", candidate.reason_code),
            )

        return candidate.model_copy(update=update)

    def _watch_reference_price(self, *, side: str, watch: Dict[str, Any]) -> Optional[float]:
        side_u = upper(side)
        if side_u == BUY:
            return self._as_float(watch.get("low"))
        if side_u == SELL:
            return self._as_float(watch.get("high"))
        return None

    def _exhaustion_signal_reference_payload(
        self,
        *,
        side: str,
        watch: Optional[Dict[str, Any]] = None,
        pa: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return the setup-level invalidation reference for exhaustion.

        WATCH high/low remains the quality reference used by first-move-consumed.
        Signal invalidation must use the true adverse extreme observed before/at
        confirmation.  For a SELL, a later confirmation candle may make a higher
        wick than the original RSI/BB WATCH row; invalidating on the older lower
        WATCH close/high exits on normal noise.  BUY is symmetrical.
        """
        side_u = upper(side)
        watch_ref = self._watch_reference_price(side=side_u, watch=watch) if isinstance(watch, dict) else None
        confirmation_ref = None
        if isinstance(pa, dict):
            confirmation_ref = self._as_float(pa.get("low") if side_u == BUY else pa.get("high"))

        refs = [x for x in (watch_ref, confirmation_ref) if x is not None]
        selected = None
        selected_source = None
        if refs:
            if side_u == BUY:
                selected = min(refs)
                selected_source = "watch_or_confirmation_extreme_low"
                if watch_ref is not None and selected == watch_ref and (confirmation_ref is None or watch_ref < confirmation_ref):
                    selected_source = "watch_extreme_low"
                elif confirmation_ref is not None and selected == confirmation_ref and (watch_ref is None or confirmation_ref < watch_ref):
                    selected_source = "confirmation_extreme_low"
            elif side_u == SELL:
                selected = max(refs)
                selected_source = "watch_or_confirmation_extreme_high"
                if watch_ref is not None and selected == watch_ref and (confirmation_ref is None or watch_ref > confirmation_ref):
                    selected_source = "watch_extreme_high"
                elif confirmation_ref is not None and selected == confirmation_ref and (watch_ref is None or confirmation_ref > watch_ref):
                    selected_source = "confirmation_extreme_high"

        return {
            "reference_price": selected,
            "reference_source": selected_source,
            "signal_invalidation_reference_price": selected,
            "signal_invalidation_reference_source": selected_source,
            "initial_stop_reference_price": selected,
            "initial_stop_reference_source": selected_source,
            "watched_extreme_price": watch_ref,
            "confirmation_reference_price": confirmation_ref,
            "signal_invalidation_reference_policy": (
                "SELL uses the highest adverse extreme from WATCH high and confirmation high; "
                "BUY uses the lowest adverse extreme from WATCH low and confirmation low. "
                "The trade manager still owns SL buffers and trailing."
            ),
        }

    def _apply_exhaustion_signal_reference(
        self,
        setup_levels: Dict[str, Any],
        *,
        side: str,
        watch: Optional[Dict[str, Any]] = None,
        pa: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        out = dict(setup_levels or {})
        ref = self._exhaustion_signal_reference_payload(side=side, watch=watch, pa=pa)
        if ref.get("reference_price") is not None:
            out.update(ref)
        return out

    def _most_adverse_exhaustion_watch(self, *, side: str, watches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        side_u = upper(side)
        valid: List[Dict[str, Any]] = []
        for watch in watches:
            if not isinstance(watch, dict):
                continue
            ref = self._watch_reference_price(side=side_u, watch=watch)
            if ref is None:
                continue
            valid.append(dict(watch))
        if not valid:
            return None
        if side_u == BUY:
            return sorted(valid, key=lambda w: self._watch_reference_price(side=side_u, watch=w) or float("inf"))[0]
        if side_u == SELL:
            return sorted(valid, key=lambda w: self._watch_reference_price(side=side_u, watch=w) or float("-inf"), reverse=True)[0]
        return valid[0]

    @staticmethod
    def _to_ist_naive_dt(value: Any) -> Optional[datetime]:
        return to_ist_naive(value)

    def _snapshot_dt(self) -> Optional[datetime]:
        return self._to_ist_naive_dt(self._optional_path(self._snapshot, "snapshot_time"))

    def _exhaustion_watch_key(self, side: str) -> Optional[str]:
        symbol = str(self._optional_path(self._snapshot, "symbol") or "").strip().upper()
        ts = self._snapshot_dt()
        if not symbol or ts is None:
            return None
        return f"{symbol}|{ts.date().isoformat()}|{upper(side)}"

    def _snapshot_symbol(self) -> str:
        return str(self._optional_path(self._snapshot, "symbol") or "").strip().upper()

    def _snapshot_equity_ref(self) -> str:
        # Snapshot payloads for underlyings normally use symbol as equity_ref;
        # keep this helper for future derivative/index normalization.
        return str(
            self._optional_path(self._snapshot, "equity_ref")
            or self._optional_path(self._snapshot, "equityRef")
            or self._optional_path(self._snapshot, "symbol")
            or ""
        ).strip().upper()

    def _instrument_profile_is_index(self) -> bool:
        profile = getattr(self.cfg, "instrument_profile", None)
        if not profile or not bool(getattr(profile, "enabled", False)):
            return False
        symbol = self._snapshot_symbol()
        equity_ref = self._snapshot_equity_ref()
        index_symbols = {str(x).strip().upper() for x in getattr(profile, "index_symbols", ())}
        return symbol in index_symbols or equity_ref in index_symbols

    def _watch_lookback_bars_for_snapshot(self) -> int:
        cfg = self.cfg.exhaustion_reversal
        if self._instrument_profile_is_index():
            return max(1, int(getattr(self.cfg.instrument_profile, "index_watch_extreme_lookback_bars", cfg.watch_extreme_lookback_bars) or cfg.watch_extreme_lookback_bars))
        return max(1, int(getattr(cfg, "watch_extreme_lookback_bars", 5) or 5))

    def _watch_valid_minutes_for_snapshot(self) -> float:
        cfg = self.cfg.exhaustion_reversal
        if self._instrument_profile_is_index():
            return float(getattr(self.cfg.instrument_profile, "index_watch_extreme_valid_minutes", cfg.watch_extreme_valid_minutes) or cfg.watch_extreme_valid_minutes)
        return float(getattr(cfg, "watch_extreme_valid_minutes", 15.5) or 15.5)

    def _setup_state_enabled(self) -> bool:
        cfg = getattr(self.cfg, "setup_state", None)
        return bool(cfg and getattr(cfg, "enabled", False))

    def _setup_state_read_watch_enabled(self) -> bool:
        cfg = getattr(self.cfg, "setup_state", None)
        return bool(
            self._read_persistent_setup_state
            and self._setup_state_enabled()
            and getattr(cfg, "read_watch_enabled", False)
        )

    def _setup_state_write_enabled(self) -> bool:
        cfg = getattr(self.cfg, "setup_state", None)
        return bool(
            self._persist_setup_state
            and self._setup_state_enabled()
            and getattr(cfg, "write_enabled", False)
        )

    def _safe_setup_state_fetch(self, *, side: str, setup_label: Optional[str] = None) -> Optional[Any]:
        if not self._setup_state_read_watch_enabled():
            return None
        ts = self._snapshot_dt()
        equity_ref = self._snapshot_equity_ref()
        if ts is None or not equity_ref:
            return None
        try:
            return StockSetupStateSchema.fetch_state(
                trading_day=ts.date(),
                equity_ref=equity_ref,
                setup=setup_label or self.cfg.pattern.setup_exhaustion_reversal,
                side=upper(side),
            )
        except Exception:
            if bool(getattr(self.cfg.setup_state, "fail_silently", True)):
                return None
            raise

    def _safe_setup_state_upsert(self, payload: Dict[str, Any]) -> None:
        if not self._setup_state_write_enabled():
            return
        try:
            StockSetupStateSchema.upsert_state(payload)
        except Exception:
            if bool(getattr(self.cfg.setup_state, "fail_silently", True)):
                return
            raise

    def _setup_state_terminal_states(self) -> set[str]:
        cfg = self.cfg.setup_state
        configured = {upper(x) for x in getattr(cfg, "terminal_states", ()) if upper(x)}
        configured.update({
            upper(getattr(cfg, "consumed_state", "CONSUMED")),
            upper(getattr(cfg, "invalidated_state", "INVALIDATED")),
            upper(getattr(cfg, "expired_state", "EXPIRED")),
            upper(getattr(cfg, "cooldown_state", "COOLDOWN")),
            upper(getattr(cfg, "signal_created_state", "SIGNAL_CREATED")),
            upper(getattr(cfg, "dropped_state", "DROPPED")),
        })
        return {x for x in configured if x}

    def _current_snapshot_fresh_extreme_against_state(self, *, side: str, row: Any) -> bool:
        """Return True only when price has made a fresh adverse extreme.

        For BUY exhaustion, a fresh reset requires a lower low than the prior
        watched/consumed reference. For SELL exhaustion, it requires a higher
        high. This prevents repeated same-side CREATE from the same old extreme
        while still allowing a genuinely new exhaustion event later in the day.
        """
        side_u = upper(side)
        ref = self._as_float(getattr(row, "reference_price", None))
        if ref is None:
            ref = self._as_float(getattr(row, "discovery_extreme_price", None))
        if ref is None:
            state_json = getattr(row, "state_json", None) or {}
            watch = state_json.get("watch") if isinstance(state_json, dict) else None
            if isinstance(watch, dict):
                ref = self._as_float(watch.get("low") if side_u == BUY else watch.get("high"))
        if ref is None:
            return False

        current_low = self._optional_numeric(self._snapshot, "bar.low")
        current_high = self._optional_numeric(self._snapshot, "bar.high")
        atr = self._optional_numeric(self._snapshot, "indicators.atr.value")
        tolerance = 0.0
        if atr is not None and atr > 0:
            tolerance = float(getattr(self.cfg.setup_state, "same_side_reset_fresh_extreme_buffer_atr", 1.00) or 0.0) * atr

        if side_u == BUY:
            return current_low is not None and current_low < ref - tolerance
        if side_u == SELL:
            return current_high is not None and current_high > ref + tolerance
        return False

    def _same_side_reset_cooling_observed(self, *, side: str, row: Any) -> bool:
        """Return True if price cooled out of exhaustion after the terminal row.

        A same-side reset should not be a marginal new high/low immediately after
        a failed reversal.  Require at least one completed candle after the
        consumed/terminal update where RSI/BB moved out of the side's extreme
        WATCH zone before accepting a fresh same-side exhaustion event.
        """
        if not bool(getattr(self.cfg.setup_state, "same_side_reset_requires_cooling", True)):
            return True

        side_u = upper(side)
        current_ts = self._snapshot_dt()
        since = (
            self._to_ist_naive_dt(getattr(row, "last_seen_time", None))
            or self._to_ist_naive_dt(getattr(row, "confirmation_time", None))
            or self._to_ist_naive_dt(getattr(row, "first_seen_time", None))
        )
        if current_ts is None or since is None:
            return False

        rows = self._recent_snapshot_dicts_for_setup_watch(
            lookback_bars=max(1, int(getattr(self.cfg.setup_state, "same_side_reset_cooling_lookback_bars", 20) or 20))
        )
        cfg = self.cfg.exhaustion_reversal
        sell_zones = {upper(x) for x in getattr(cfg, "watch_extreme_sell_bollinger_zones", ())}
        buy_zones = {upper(x) for x in getattr(cfg, "watch_extreme_buy_bollinger_zones", ())}

        for snap in rows:
            snap_ts = self._dt_from_snapshot_dict(snap)
            if snap_ts is None or snap_ts <= since or snap_ts >= current_ts:
                continue
            rsi = self._as_float(self._path_from(snap, "indicators.rsi.value"))
            bb_pos = self._as_float(self._path_from(snap, "indicators.bollinger.position"))
            bb_zone = upper(self._path_from(snap, "indicators.bollinger.zone") or "")
            if rsi is None or bb_pos is None:
                continue

            if side_u == SELL:
                cooled = (
                    rsi < float(getattr(cfg, "watch_extreme_sell_rsi_min", 70.0))
                    and bb_pos < float(getattr(cfg, "watch_extreme_sell_bollinger_position_min", 1.0))
                    and bb_zone not in sell_zones
                )
            elif side_u == BUY:
                cooled = (
                    rsi > float(getattr(cfg, "watch_extreme_buy_rsi_max", 30.0))
                    and bb_pos > float(getattr(cfg, "watch_extreme_buy_bollinger_position_max", 0.0))
                    and bb_zone not in buy_zones
                )
            else:
                cooled = False

            if cooled:
                return True
        return False

    def _same_side_terminal_reset_filter(self, *, side: str, row: Any) -> Dict[str, Any]:
        """Explain whether a terminal same-side setup is allowed to reset."""
        now = self._snapshot_dt()
        terminal_snapshot_time = self._to_ist_naive_dt(getattr(row, "last_seen_time", None))
        cooldown_minutes = float(getattr(self.cfg.setup_state, "same_side_cooldown_minutes", 30.0) or 0.0)
        minutes_since_terminal = None
        if isinstance(now, datetime) and isinstance(terminal_snapshot_time, datetime):
            minutes_since_terminal = max(0.0, (now - terminal_snapshot_time).total_seconds() / 60.0)

        cooldown_passed = minutes_since_terminal is None or minutes_since_terminal >= cooldown_minutes
        fresh_extreme = self._current_snapshot_fresh_extreme_against_state(side=side, row=row)
        cooling_seen = self._same_side_reset_cooling_observed(side=side, row=row)
        passes = bool(cooldown_passed and fresh_extreme and cooling_seen)

        return {
            "passes": passes,
            "code": getattr(self.cfg.setup_state, "same_side_reset_code", "EXHAUSTION_REVERSAL_SAME_SIDE_RESET_ALLOWED") if passes else getattr(self.cfg.setup_state, "same_side_no_reset_code", "CREATE_BLOCKED_SAME_SIDE_NO_FRESH_RESET"),
            "side": upper(side),
            "state": upper(getattr(row, "state", "")),
            "signal_id": getattr(row, "signal_id", None),
            "cooldown_minutes": cooldown_minutes,
            "minutes_since_terminal": round(float(minutes_since_terminal), 2) if minutes_since_terminal is not None else None,
            "cooldown_passed": bool(cooldown_passed),
            "fresh_extreme_present": bool(fresh_extreme),
            "fresh_extreme_buffer_atr": float(getattr(self.cfg.setup_state, "same_side_reset_fresh_extreme_buffer_atr", 1.00) or 0.0),
            "cooling_seen": bool(cooling_seen),
            "reset_requires_cooling": bool(getattr(self.cfg.setup_state, "same_side_reset_requires_cooling", True)),
            "rule": (
                "A consumed/terminal same-side exhaustion setup may reset only after cooldown, "
                "a meaningful fresh adverse extreme, and RSI/BB cooling out of exhaustion."
            ),
        }

    def _transition_setup_state(
        self,
        *,
        side: str,
        state: str,
        reason: str,
        signal_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        setup_label: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> None:
        if not self._setup_state_write_enabled():
            return
        ts = self._snapshot_dt()
        equity_ref = self._snapshot_equity_ref()
        symbol = self._snapshot_symbol()
        if ts is None:
            raise ValueError("setup-state transition requires snapshot.snapshot_time")
        if not equity_ref:
            raise ValueError("setup-state transition requires snapshot equity_ref/symbol")
        try:
            StockSetupStateSchema.transition_state(
                trading_day=ts.date(),
                equity_ref=equity_ref,
                symbol=symbol or equity_ref,
                setup=setup_label or self.cfg.pattern.setup_exhaustion_reversal,
                side=upper(side),
                state=state,
                state_reason=reason,
                ts=ts,
                signal_id=signal_id,
                expires_at=expires_at,
                state_json_update={"terminal_transition": sanitize_json(details or {})},
            )
        except Exception:
            if bool(getattr(self.cfg.setup_state, "fail_silently", True)):
                return
            raise

    def _setup_state_terminal_gate(self, *, side: str, mark_cooldown: bool = False) -> Optional[Dict[str, Any]]:
        if not self._setup_state_enabled():
            return None
        cfg = self.cfg.setup_state
        row = self._safe_setup_state_fetch(side=side)
        if row is None:
            return None

        now = self._snapshot_dt()
        state = upper(getattr(row, "state", ""))
        expires_at = self._to_ist_naive_dt(getattr(row, "expires_at", None))
        terminal_states = self._setup_state_terminal_states()
        expired_now = bool(expires_at is not None and now is not None and now > expires_at)

        # Active WATCH/PENDING rows can expire.  Already-terminal rows must not
        # bounce EXPIRED -> COOLDOWN -> EXPIRED on every later snapshot.
        if state not in terminal_states and expired_now and state not in {upper(getattr(cfg, "expired_state", "EXPIRED"))}:
            self._transition_setup_state(
                side=side,
                state=getattr(cfg, "expired_state", "EXPIRED"),
                reason=getattr(cfg, "expired_reason_code", "EXHAUSTION_REVERSAL_SETUP_STATE_EXPIRED"),
                signal_id=getattr(row, "signal_id", None),
                details={
                    "previous_state": state,
                    "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None,
                    "snapshot_time": now.isoformat() if isinstance(now, datetime) else None,
                },
            )
            state = upper(getattr(cfg, "expired_state", "EXPIRED"))

        if state not in terminal_states:
            return None

        reset_filter = self._same_side_terminal_reset_filter(side=side, row=row)
        if bool(reset_filter.get("passes")) or not bool(getattr(cfg, "block_terminal_without_fresh_extreme", True)):
            return None

        code = reset_filter.get("code") or getattr(cfg, "cooldown_reason_code", "EXHAUSTION_REVERSAL_SETUP_STATE_COOLDOWN")
        gate = {
            "blocked": True,
            "code": code,
            "side": upper(side),
            "state": state,
            "signal_id": getattr(row, "signal_id", None),
            "reference_price": self._as_float(getattr(row, "reference_price", None)),
            "reference_source": getattr(row, "reference_source", None),
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None,
            "reset_filter": reset_filter,
            "fresh_extreme_required": True,
            "fresh_extreme_present": bool(reset_filter.get("fresh_extreme_present")),
            "rule": "A terminal same-side setup may not CREATE again until cooldown, cooling, and a meaningful fresh adverse extreme reset are all present.",
        }
        if mark_cooldown and state in {
            upper(getattr(cfg, "consumed_state", "CONSUMED")),
            upper(getattr(cfg, "invalidated_state", "INVALIDATED")),
            upper(getattr(cfg, "signal_created_state", "SIGNAL_CREATED")),
            upper(getattr(cfg, "dropped_state", "DROPPED")),
        }:
            self._transition_setup_state(
                side=side,
                state=getattr(cfg, "cooldown_state", "COOLDOWN"),
                reason=code,
                signal_id=getattr(row, "signal_id", None),
                details=gate,
            )
        return gate

    def _watch_from_persistent_state(self, *, side: str) -> Optional[Dict[str, Any]]:
        row = self._safe_setup_state_fetch(side=side)
        if row is None:
            return None
        state_cfg = self.cfg.setup_state
        state = upper(getattr(row, "state", ""))
        active_states = {
            upper(state_cfg.watch_state),
            upper(state_cfg.confirmed_pending_state),
            upper(getattr(state_cfg, "confirmed_deferred_state", "CONFIRMED_DEFERRED")),
        }
        if state not in active_states:
            return None
        now = self._snapshot_dt()
        expires_at = self._to_ist_naive_dt(getattr(row, "expires_at", None))
        if now is None or expires_at is None or now > expires_at:
            self._transition_setup_state(
                side=side,
                state=getattr(state_cfg, "expired_state", "EXPIRED"),
                reason=getattr(state_cfg, "expired_reason_code", "EXHAUSTION_REVERSAL_SETUP_STATE_EXPIRED"),
                signal_id=getattr(row, "signal_id", None),
                details={
                    "previous_state": state,
                    "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None,
                    "snapshot_time": now.isoformat() if isinstance(now, datetime) else None,
                },
            )
            return None
        state_json = getattr(row, "state_json", None) or {}
        watch = state_json.get("watch") if isinstance(state_json, dict) else None
        if not isinstance(watch, dict):
            return None
        watch = dict(watch)
        watch.setdefault("source", "STOCK_SETUP_STATE")
        watch.setdefault("side", upper(side))
        first_seen = self._to_ist_naive_dt(getattr(row, "first_seen_time", None))
        watch_ts = self._to_ist_naive_dt(watch.get("snapshot_time")) or first_seen
        if isinstance(watch_ts, datetime):
            # JSON persistence converts datetimes to ISO strings. Always restore
            # the original WATCH timestamp before freshness/expiry calculations.
            watch["snapshot_time"] = watch_ts
            if now is not None:
                age_minutes = max(0.0, (now - watch_ts).total_seconds() / 60.0)
                watch["age_minutes"] = round(age_minutes, 2)
                watch["age_bars"] = max(0, int(round(age_minutes / 3.0)))
        return watch

    def _write_exhaustion_watch_state(self, *, side: str, watch: Dict[str, Any], state_reason: str) -> None:
        ts = self._snapshot_dt()
        equity_ref = self._snapshot_equity_ref()
        symbol = self._snapshot_symbol()
        if ts is None:
            raise ValueError("EXHAUSTION_REVERSAL WATCH persistence requires snapshot.snapshot_time")
        if not equity_ref:
            raise ValueError("EXHAUSTION_REVERSAL WATCH persistence requires equity_ref/symbol")
        valid_minutes = self._watch_valid_minutes_for_snapshot()
        watch_ts = self._to_ist_naive_dt(watch.get("snapshot_time"))
        event_time = self._to_ist_naive_dt(watch.get("event_time"))
        event_key = str(watch.get("event_key") or "").strip()
        if watch_ts is None or event_time is None or not event_key:
            raise ValueError(
                "EXHAUSTION_REVERSAL WATCH persistence requires immutable "
                "snapshot_time, event_time, and event_key"
            )
        watch["snapshot_time"] = watch_ts
        watch["event_time"] = event_time
        # Expiry belongs to the original watched extreme, not the later candle
        # that happens to rewrite/re-evaluate the same setup state.
        expires_at = watch_ts + timedelta(minutes=valid_minutes)
        side_u = upper(side)
        reference_price = self._as_float(watch.get("low") if side_u == BUY else watch.get("high"))
        self._safe_setup_state_upsert({
            "trading_day": ts.date(),
            "equity_ref": equity_ref,
            "symbol": symbol or equity_ref,
            "lifecycle": getattr(self.cfg, "lifecycle_name", "DEFAULT"),
            "setup": self.cfg.pattern.setup_exhaustion_reversal,
            "side": side_u,
            "state": self.cfg.setup_state.watch_state,
            "state_reason": state_reason,
            "first_seen_time": watch_ts,
            "last_seen_time": ts,
            "expires_at": expires_at,
            "age_bars": watch.get("age_bars"),
            "discovery_price": watch.get("close"),
            "discovery_extreme_price": reference_price,
            "reference_price": reference_price,
            "reference_source": "watch_extreme_low" if side_u == BUY else "watch_extreme_high",
            "signal_id": None,
            "state_json": {
                "event_key": event_key,
                "event_source": watch.get("source") or "EXHAUSTION_REVERSAL_WATCH",
                "event_time": event_time,
                "watch": dict(watch),
                "valid_minutes": valid_minutes,
                "source": "SETUP_DISCOVERY_HELPER",
            },
        })

    def _write_exhaustion_candidate_state(
        self,
        *,
        side: str,
        watch: Dict[str, Any],
        pa: Dict[str, Any],
        location: Dict[str, Any],
        reason_code: str,
    ) -> None:
        ts = self._snapshot_dt()
        equity_ref = self._snapshot_equity_ref()
        symbol = self._snapshot_symbol()
        if ts is None:
            raise ValueError("EXHAUSTION_REVERSAL candidate persistence requires snapshot.snapshot_time")
        if not equity_ref:
            raise ValueError("EXHAUSTION_REVERSAL candidate persistence requires equity_ref/symbol")
        side_u = upper(side)
        if bool(pa.get("confirmed")):
            state = (
                self.cfg.setup_state.confirmed_state
                if not bool(location.get("blocked"))
                else getattr(self.cfg.setup_state, "confirmed_deferred_state", "CONFIRMED_DEFERRED")
            )
        else:
            state = self.cfg.setup_state.watch_state
        reference_payload = self._exhaustion_signal_reference_payload(side=side_u, watch=watch, pa=pa)
        reference_price = self._as_float(reference_payload.get("reference_price"))
        watched_reference_price = self._as_float(reference_payload.get("watched_extreme_price"))
        confirmation_reference_price = self._as_float(reference_payload.get("confirmation_reference_price"))
        valid_minutes = self._watch_valid_minutes_for_snapshot()
        first_seen = self._to_ist_naive_dt(watch.get("snapshot_time"))
        event_time = self._to_ist_naive_dt(watch.get("event_time"))
        event_key = str(watch.get("event_key") or "").strip()
        if first_seen is None or event_time is None or not event_key:
            raise ValueError(
                "EXHAUSTION_REVERSAL candidate persistence requires immutable "
                "snapshot_time, event_time, and event_key"
            )
        watch["snapshot_time"] = first_seen
        watch["event_time"] = event_time
        # Never extend an old WATCH just because confirmation is evaluated later.
        expires_at = first_seen + timedelta(minutes=valid_minutes)
        if ts > expires_at:
            self._transition_setup_state(
                side=side_u,
                state=getattr(self.cfg.setup_state, "expired_state", "EXPIRED"),
                reason=getattr(self.cfg.setup_state, "expired_reason_code", "EXHAUSTION_REVERSAL_SETUP_STATE_EXPIRED"),
                details={
                    "event_time": first_seen.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "snapshot_time": ts.isoformat(),
                    "attempted_state": state,
                    "rule": "Expired exhaustion events cannot be promoted or resurrected.",
                },
            )
            return
        self._safe_setup_state_upsert({
            "trading_day": ts.date(),
            "equity_ref": equity_ref,
            "symbol": symbol or equity_ref,
            "lifecycle": getattr(self.cfg, "lifecycle_name", "DEFAULT"),
            "setup": self.cfg.pattern.setup_exhaustion_reversal,
            "side": side_u,
            "state": state,
            "state_reason": reason_code,
            "first_seen_time": first_seen,
            "last_seen_time": ts,
            "expires_at": expires_at,
            "age_bars": watch.get("age_bars"),
            "discovery_price": watch.get("close"),
            "discovery_extreme_price": watched_reference_price,
            "confirmation_price": pa.get("close"),
            "confirmation_time": ts if bool(pa.get("confirmed")) else None,
            "reference_price": reference_price,
            "reference_source": reference_payload.get("reference_source"),
            "signal_id": None,
            "state_json": {
                "event_key": event_key,
                "event_source": watch.get("source") or "EXHAUSTION_REVERSAL_WATCH",
                "event_time": event_time,
                "watch": dict(watch),
                "watched_extreme_price": watched_reference_price,
                "watched_extreme_time": first_seen.isoformat() if isinstance(first_seen, datetime) else str(first_seen),
                "confirmation_reference_price": confirmation_reference_price,
                "confirmation_reference_time": ts.isoformat(),
                "signal_invalidation_reference_price": reference_price,
                "signal_invalidation_reference_source": reference_payload.get("reference_source"),
                "signal_invalidation_reference_policy": reference_payload.get("signal_invalidation_reference_policy"),
                "price_action": dict(pa),
                "entry_location_filter": dict(location),
                "source": "SETUP_DISCOVERY_HELPER",
            },
        })

    def _is_extreme_exhaustion_watch_location(self, *, side: str, base_data: Dict[str, Any]) -> bool:
        cfg = self.cfg.exhaustion_reversal
        side_u = upper(side)
        rsi = self._as_float(base_data.get("rsi"))
        bb_pos = self._as_float(base_data.get("bollinger_position"))
        bb_zone = upper(base_data.get("bollinger_zone") or "")
        if rsi is None or bb_pos is None:
            return False
        if side_u == SELL:
            return (
                rsi >= float(cfg.watch_extreme_sell_rsi_min)
                and (
                    bb_pos >= float(cfg.watch_extreme_sell_bollinger_position_min)
                    or bb_zone in {upper(x) for x in cfg.watch_extreme_sell_bollinger_zones}
                )
            )
        if side_u == BUY:
            return (
                rsi <= float(cfg.watch_extreme_buy_rsi_max)
                and (
                    bb_pos <= float(cfg.watch_extreme_buy_bollinger_position_max)
                    or bb_zone in {upper(x) for x in cfg.watch_extreme_buy_bollinger_zones}
                )
            )
        return False

    @staticmethod
    def _snapshot_as_dict(x: Any) -> Dict[str, Any]:
        if isinstance(x, dict):
            return x
        if hasattr(x, "model_dump"):
            try:
                return x.model_dump(mode="python")
            except TypeError:
                return x.model_dump()
        if hasattr(x, "dict"):
            return x.dict()
        return {}

    @staticmethod
    def _path_from(row: Dict[str, Any], path: str) -> Any:
        cur: Any = row
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        return cur

    def _dt_from_snapshot_dict(self, row: Dict[str, Any]) -> Optional[datetime]:
        return self._to_ist_naive_dt(row.get("snapshot_time"))

    def _recent_snapshot_dicts_for_setup_watch(self, *, lookback_bars: int) -> List[Dict[str, Any]]:
        """Return recent completed snapshots before the current snapshot.

        This common setup-event memory source is used by EXHAUSTION_REVERSAL and
        FAILED_BREAKOUT. It reconstructs WATCH context from persisted/replayed
        snapshots instead of relying only on an in-process dictionary, keeping
        DISCOVER -> WATCH -> CONFIRM stable across replay loops, service restarts,
        and workers that instantiate a fresh discoverer per snapshot.
        """
        d = self._snapshot
        symbol = str(self._optional_path(d, "symbol") or "").strip().upper()
        current_ts = self._snapshot_dt()
        if not symbol or current_ts is None or lookback_bars <= 0:
            return []

        rows: List[Dict[str, Any]] = []

        raw_recent = d.get("_recent_snapshots")
        if isinstance(raw_recent, list):
            for item in raw_recent:
                rd = self._snapshot_as_dict(item)
                if rd:
                    rows.append(rd)

        # Fallback/source of truth for backtests and live services: fetch the
        # previous snapshots for this symbol from the DB.  Import locally so this
        # helper remains cheap and unit-testable when DB modules are unavailable.
        if len(rows) < lookback_bars:
            try:
                from schemas.snapshot import SnapshotSchema

                fetched = SnapshotSchema.fetch_recent_today_for_symbol_before_time(
                    symbol,
                    current_ts,
                    limit=int(lookback_bars) + 1,
                    ascending=True,
                )
                for item in fetched:
                    rd = self._snapshot_as_dict(item)
                    if rd:
                        rows.append(rd)
            except Exception:
                # Discovery must never fail because recent-memory lookup failed.
                pass

        # Keep only snapshots strictly before the current bar; the current bar is
        # evaluated separately by _price_action_confirmation().
        dedup: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            ts = self._dt_from_snapshot_dict(row)
            if ts is None or ts >= current_ts:
                continue
            row_symbol = str(row.get("symbol") or symbol).strip().upper()
            if row_symbol and row_symbol != symbol:
                continue
            dedup[ts.isoformat()] = row

        ordered = [dedup[k] for k in sorted(dedup.keys())]
        return ordered[-int(lookback_bars):]

    def _exhaustion_watch_from_snapshot(self, row: Dict[str, Any], *, side: str, age_bars: int) -> Optional[Dict[str, Any]]:
        """Build a WATCH-memory object from a prior RSI/BB extreme snapshot.

        This is intentionally broader than normal CREATE discovery: RSI/BB
        extreme only says “watch this location”; it does not create a signal.
        CREATE still requires current price-action confirmation.
        """
        side_u = upper(side)
        rsi = self._as_float(self._path_from(row, "indicators.rsi.value"))
        bb_pos = self._as_float(self._path_from(row, "indicators.bollinger.position"))
        bb_zone = upper(self._path_from(row, "indicators.bollinger.zone") or "")
        base_data = {
            "rsi": rsi,
            "bollinger_position": bb_pos,
            "bollinger_zone": bb_zone,
        }
        if not self._is_extreme_exhaustion_watch_location(side=side_u, base_data=base_data):
            return None

        ts = self._dt_from_snapshot_dict(row)
        current_ts = self._snapshot_dt()
        if ts is None or current_ts is None or ts >= current_ts:
            return None
        age_minutes = (current_ts - ts).total_seconds() / 60.0
        valid_minutes = float(self.cfg.exhaustion_reversal.watch_extreme_valid_minutes)
        if age_minutes <= 0 or age_minutes > valid_minutes:
            return None

        return {
            "setup_label": self.cfg.pattern.setup_exhaustion_reversal,
            "side": side_u,
            "symbol": str(row.get("symbol") or self._optional_path(self._snapshot, "symbol") or "").strip().upper(),
            "snapshot_time": ts,
            "event_time": ts,
            "event_key": f"{self._snapshot_symbol()}|{ts.date().isoformat()}|{side_u}|{ts.isoformat()}",
            "age_bars": int(age_bars),
            "age_minutes": round(float(age_minutes), 2),
            "open": self._as_float(self._path_from(row, "bar.open")),
            "high": self._as_float(self._path_from(row, "bar.high")),
            "low": self._as_float(self._path_from(row, "bar.low")),
            "close": self._as_float(self._path_from(row, "bar.close")),
            "rsi": rsi,
            "bollinger_position": bb_pos,
            "bollinger_zone": bb_zone,
            "sod_position": self._as_float(self._path_from(row, "market_windows.sod.close_position_in_range")),
            "position_30m": self._as_float(self._path_from(row, "market_windows.30m.close_position_in_range")),
            "sod_move_atr": self._as_float(self._path_from(row, "market_windows.sod.move_atr")),
            "move_30m_atr": self._as_float(self._path_from(row, "market_windows.30m.move_atr")),
            "vwap_gap_pct": self._as_float(self._path_from(row, "indicators.vwap.distance_pct")),
            "valid_minutes": valid_minutes,
            "source": "RECENT_SNAPSHOT_EXTREME",
            "promotion_policy": "RSI_BB_EXTREME_DISCOVER_THEN_PRICE_ACTION_CONFIRM_WITHIN_LOOKBACK",
        }

    def _recent_extreme_exhaustion_watch(self, *, side: str) -> Optional[Dict[str, Any]]:
        watches: List[Dict[str, Any]] = []

        persistent_watch = self._watch_from_persistent_state(side=side)
        if persistent_watch is not None:
            watches.append(persistent_watch)

        # Do not reconstruct a consumed/invalidated/expired watch from old
        # snapshots.  This was the main July-9 churn path: after a setup created
        # a signal, the recent-snapshot fallback could re-use the same RSI/BB
        # extreme and create another same-side signal once the previous signal
        # exited.  A fresh adverse extreme will reset the row via normal WATCH
        # discovery and then reconstruction is allowed again.
        if self._setup_state_terminal_gate(side=side, mark_cooldown=False) is not None:
            return self._most_adverse_exhaustion_watch(side=side, watches=watches)

        lookback = self._watch_lookback_bars_for_snapshot()
        recent = self._recent_snapshot_dicts_for_setup_watch(lookback_bars=lookback)
        for age_bars, row in enumerate(reversed(recent), start=1):
            watch = self._exhaustion_watch_from_snapshot(row, side=side, age_bars=age_bars)
            if watch is not None:
                watches.append(watch)

        # Use the strongest watched exhaustion, not the first/oldest/memory row.
        # BUY should measure from the lowest watched low; SELL from the highest
        # watched high.  This prevents an older weaker WATCH from bypassing the
        # first-move-consumed guard when a later deeper extreme exists.
        return self._most_adverse_exhaustion_watch(side=side, watches=watches)

    def _remember_exhaustion_watch_if_extreme(self, *, side: str, base_data: Dict[str, Any], pa: Dict[str, Any]) -> None:
        cfg = self.cfg.exhaustion_reversal
        if not bool(cfg.watch_extreme_promotion_enabled):
            return
        # Only remember true WATCH cases: strong location exists, but the current
        # candle itself has not yet confirmed reversal price action.
        if bool(pa.get("confirmed")):
            return
        if not self._is_extreme_exhaustion_watch_location(side=side, base_data=base_data):
            return

        # Terminal same-side state may be reset only by a fresh adverse extreme.
        # If the current extreme is merely the old consumed/invalidated area, do
        # not overwrite CONSUMED/COOLDOWN back to WATCH.
        if self._setup_state_terminal_gate(side=side, mark_cooldown=False) is not None:
            return

        key = self._exhaustion_watch_key(side)
        ts = self._snapshot_dt()
        if key is None or ts is None:
            return
        watch = {
            "setup_label": self.cfg.pattern.setup_exhaustion_reversal,
            "side": upper(side),
            "symbol": str(self._optional_path(self._snapshot, "symbol") or "").strip().upper(),
            "snapshot_time": ts,
            "event_time": ts,
            "event_key": f"{key}|{ts.isoformat()}",
            "open": self._optional_numeric(self._snapshot, "bar.open"),
            "high": self._optional_numeric(self._snapshot, "bar.high"),
            "low": self._optional_numeric(self._snapshot, "bar.low"),
            "close": self._optional_numeric(self._snapshot, "bar.close"),
            "rsi": self._as_float(base_data.get("rsi")),
            "bollinger_position": self._as_float(base_data.get("bollinger_position")),
            "bollinger_zone": upper(base_data.get("bollinger_zone") or ""),
            "sod_position": self._as_float(base_data.get("sod_position")),
            "position_30m": self._as_float(base_data.get("position_30m")),
            "sod_move_atr": self._as_float(base_data.get("sod_move_atr")),
            "move_30m_atr": self._as_float(base_data.get("move_30m_atr")),
            "price_action_at_watch": dict(pa),
            "valid_minutes": self._watch_valid_minutes_for_snapshot(),
            "promotion_policy": "WAIT_FOR_REVERSAL_CANDLE_BREAK_OF_EXTREME_CANDLE",
        }
        self._exhaustion_watch_memory[key] = watch
        self._write_exhaustion_watch_state(
            side=side,
            watch=watch,
            state_reason="EXHAUSTION_REVERSAL_EXTREME_WATCH",
        )

    def _watched_extreme_is_fresh(self, watch: Dict[str, Any]) -> bool:
        ts = self._snapshot_dt()
        watch_ts = self._to_ist_naive_dt(watch.get("snapshot_time"))
        if ts is None or watch_ts is None:
            return False
        watch["snapshot_time"] = watch_ts
        age_minutes = (ts - watch_ts).total_seconds() / 60.0
        return 0.0 < age_minutes <= float(watch.get("valid_minutes") or self._watch_valid_minutes_for_snapshot())

    def _watched_extreme_reset_filter(self, *, side: str, watch: Dict[str, Any]) -> Dict[str, Any]:
        """Return whether a WATCH extreme is still the active price-action reference.

        If price closes beyond the watched extreme after the watched candle and
        before the current confirming candle, the old WATCH is stale.  Do not promote
        from it; wait for a fresh rejection/reclaim around the newer extreme.
        This is price-action only and deliberately avoids HMA/ADX/slow structure.
        """
        cfg = self.cfg.exhaustion_reversal
        side_u = upper(side)
        watch_ts = self._to_ist_naive_dt(watch.get("snapshot_time"))
        current_ts = self._snapshot_dt()
        if watch_ts is not None:
            watch["snapshot_time"] = watch_ts
        watched_high = self._as_float(watch.get("high"))
        watched_low = self._as_float(watch.get("low"))
        atr = self._optional_numeric(self._snapshot, "indicators.atr.value")
        tolerance = 0.0
        if atr is not None and atr > 0:
            tolerance = float(getattr(cfg, "watch_extreme_reset_tolerance_atr", 0.10) or 0.0) * atr

        if not isinstance(watch_ts, datetime) or current_ts is None:
            return {"passes": True, "reason": "WATCH_TIMESTAMP_UNAVAILABLE"}

        rows = self._recent_snapshot_dicts_for_setup_watch(
            lookback_bars=self._watch_lookback_bars_for_snapshot()
        )
        stale_rows: List[Dict[str, Any]] = []
        for row in rows:
            row_ts = self._dt_from_snapshot_dict(row)
            if row_ts is None or row_ts <= watch_ts or row_ts >= current_ts:
                continue
            row_close = self._as_float(self._path_from(row, "bar.close"))
            if side_u == BUY and watched_low is not None and row_close is not None and row_close < watched_low - tolerance:
                stale_rows.append({
                    "snapshot_time": row_ts.isoformat(),
                    "close": row_close,
                    "watched_low": watched_low,
                })
            elif side_u == SELL and watched_high is not None and row_close is not None and row_close > watched_high + tolerance:
                stale_rows.append({
                    "snapshot_time": row_ts.isoformat(),
                    "close": row_close,
                    "watched_high": watched_high,
                })

        if not stale_rows:
            return {
                "passes": True,
                "side": side_u,
                "watch_snapshot_time": watch_ts.isoformat(),
                "tolerance_points": round(float(tolerance), 4),
            }

        return {
            "passes": False,
            "code": "EXHAUSTION_REVERSAL_WATCH_EXTREME_RESET_BY_NEW_EXTREME",
            "side": side_u,
            "watch_snapshot_time": watch_ts.isoformat(),
            "tolerance_points": round(float(tolerance), 4),
            "newer_adverse_extremes": stale_rows,
            "rule": (
                "Do not promote stale exhaustion WATCH memory after a later completed candle "
                "has closed beyond the watched extreme before confirmation."
            ),
        }

    def _first_move_consumed_filter(self, *, side: str, watch: Optional[Dict[str, Any]], pa: Dict[str, Any]) -> Dict[str, Any]:
        """Block CREATE when the reversal already travelled too far from WATCH.

        Exhaustion reversal is meant to capture the first tradable move back
        toward value.  When confirmation arrives after a large move from the
        original WATCH high/low, the easy part is already consumed and many July
        9 signals had little/no MFE.  This uses the preserved WATCH extreme, not
        the confirmation candle reference.
        """
        cfg = self.cfg.exhaustion_reversal
        side_u = upper(side)
        if not bool(getattr(cfg, "first_move_consumed_guard_enabled", True)):
            return {"passes": True, "reason": "FIRST_MOVE_CONSUMED_GUARD_DISABLED"}
        if not isinstance(watch, dict):
            return {"passes": True, "reason": "WATCH_EXTREME_UNAVAILABLE"}

        close = self._as_float(pa.get("close"))
        atr = self._optional_numeric(self._snapshot, "indicators.atr.value")
        watched_high = self._as_float(watch.get("high"))
        watched_low = self._as_float(watch.get("low"))
        max_move_atr = float(getattr(cfg, "max_confirmation_move_from_watch_atr", 1.25) or 0.0)

        move_points = None
        if side_u == SELL and watched_high is not None and close is not None:
            move_points = max(0.0, watched_high - close)
        elif side_u == BUY and watched_low is not None and close is not None:
            move_points = max(0.0, close - watched_low)

        move_atr = None
        if move_points is not None and atr is not None and atr > 0:
            move_atr = move_points / atr

        if move_atr is None:
            return {
                "passes": True,
                "reason": "FIRST_MOVE_CONSUMED_DATA_UNAVAILABLE",
                "side": side_u,
                "watched_high": watched_high,
                "watched_low": watched_low,
                "current_close": close,
                "atr": atr,
                "max_confirmation_move_from_watch_atr": max_move_atr,
            }

        passes = move_atr <= max_move_atr
        return {
            "passes": bool(passes),
            "code": getattr(cfg, "first_move_consumed_code", "CREATE_BLOCKED_FIRST_MOVE_ALREADY_CONSUMED"),
            "side": side_u,
            "watched_high": watched_high,
            "watched_low": watched_low,
            "watch_snapshot_time": str(watch.get("snapshot_time")) if watch.get("snapshot_time") is not None else None,
            "current_close": close,
            "move_from_watch_extreme_points": round(float(move_points), 4),
            "move_from_watch_extreme_atr": round(float(move_atr), 4),
            "max_confirmation_move_from_watch_atr": max_move_atr,
            "rule": (
                "CREATE is blocked when confirmation has already moved too far from the original "
                "WATCH extreme, because the first mean-reversion move is likely consumed."
            ),
        }

    def _watch_relative_price_action_confirmation(
        self,
        *,
        side: str,
        watch: Dict[str, Any],
        pa: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Confirm a timely rejection/reclaim relative to a stored WATCH extreme.

        The generic price-action helper judges the latest candle/window in
        isolation. That misses sequences where candle N creates the exhaustion
        extreme, candle N+1 rejects sharply, and candle N+2 is only a small
        pause/bounce. This helper uses the completed move away from the WATCH
        extreme, while retaining the first-move-consumed upper bound.
        """
        cfg = self.cfg.exhaustion_reversal
        side_u = upper(side)
        if not bool(getattr(cfg, "watch_relative_confirmation_enabled", True)):
            return {
                "passes": False,
                "code": "WATCH_RELATIVE_CONFIRMATION_DISABLED",
                "missing_requirements": ["WATCH_RELATIVE_CONFIRMATION_DISABLED"],
            }

        atr = self._optional_numeric(self._snapshot, "indicators.atr.value")
        current_ts = self._snapshot_dt()
        watch_ts = self._to_ist_naive_dt(watch.get("snapshot_time"))
        close = self._as_float(pa.get("close"))
        current_open = self._as_float(pa.get("open"))
        current_high = self._as_float(pa.get("high"))
        current_low = self._as_float(pa.get("low"))
        position_15m = self._as_float(pa.get("position_15m"))
        watched_high = self._as_float(watch.get("high"))
        watched_low = self._as_float(watch.get("low"))
        watched_close = self._as_float(watch.get("close"))

        missing: List[str] = []
        if atr is None or atr <= 0:
            missing.append("ATR_UNAVAILABLE")
        if current_ts is None or watch_ts is None or current_ts <= watch_ts:
            missing.append("WATCH_TIME_UNAVAILABLE_OR_NOT_PRIOR")

        age_minutes = None
        age_bars = None
        if current_ts is not None and watch_ts is not None and current_ts > watch_ts:
            age_minutes = (current_ts - watch_ts).total_seconds() / 60.0
            age_bars = max(1, int(round(age_minutes / 3.0)))
            min_age = max(1, int(getattr(cfg, "watch_relative_min_age_bars", 1) or 1))
            max_age = max(min_age, int(getattr(cfg, "watch_relative_max_age_bars", 3) or 3))
            if age_bars < min_age:
                missing.append("WATCH_RELATIVE_AGE_LT_MIN_BARS")
            if age_bars > max_age:
                missing.append("WATCH_RELATIVE_AGE_GT_MAX_BARS")
        else:
            min_age = max(1, int(getattr(cfg, "watch_relative_min_age_bars", 1) or 1))
            max_age = max(min_age, int(getattr(cfg, "watch_relative_max_age_bars", 3) or 3))

        move_from_extreme_points = None
        move_from_extreme_atr = None
        close_displacement_points = None
        close_displacement_atr = None
        extreme_separation_points = None
        extreme_separation_atr = None
        directional_current_candle = False
        position_ok = False
        adverse_close_invalid = False

        if atr is not None and atr > 0 and close is not None:
            if side_u == SELL:
                if watched_high is not None:
                    move_from_extreme_points = watched_high - close
                    move_from_extreme_atr = move_from_extreme_points / atr
                    adverse_close_invalid = close > watched_high
                if watched_close is not None:
                    close_displacement_points = watched_close - close
                    close_displacement_atr = close_displacement_points / atr
                if watched_high is not None and current_high is not None:
                    extreme_separation_points = watched_high - current_high
                    extreme_separation_atr = extreme_separation_points / atr
                directional_current_candle = bool(
                    current_open is not None and close < current_open
                )
                position_ok = bool(
                    position_15m is not None
                    and position_15m <= float(cfg.watch_relative_sell_15m_position_max)
                )
            elif side_u == BUY:
                if watched_low is not None:
                    move_from_extreme_points = close - watched_low
                    move_from_extreme_atr = move_from_extreme_points / atr
                    adverse_close_invalid = close < watched_low
                if watched_close is not None:
                    close_displacement_points = close - watched_close
                    close_displacement_atr = close_displacement_points / atr
                if watched_low is not None and current_low is not None:
                    extreme_separation_points = current_low - watched_low
                    extreme_separation_atr = extreme_separation_points / atr
                directional_current_candle = bool(
                    current_open is not None and close > current_open
                )
                position_ok = bool(
                    position_15m is not None
                    and position_15m >= float(cfg.watch_relative_buy_15m_position_min)
                )
            else:
                missing.append("UNSUPPORTED_SIDE")

        min_move = float(cfg.watch_relative_min_move_from_extreme_atr)
        max_move = float(cfg.watch_relative_max_move_from_extreme_atr)
        min_close_displacement = float(
            cfg.watch_relative_min_close_displacement_from_watch_close_atr
        )
        min_extreme_separation = float(cfg.watch_relative_min_extreme_separation_atr)

        if move_from_extreme_atr is None or move_from_extreme_atr < min_move:
            missing.append("WATCH_RELATIVE_MOVE_LT_MIN_ATR")
        if move_from_extreme_atr is not None and move_from_extreme_atr > max_move:
            missing.append("WATCH_RELATIVE_MOVE_GT_MAX_ATR_FIRST_MOVE_CONSUMED")
        if close_displacement_atr is None or close_displacement_atr < min_close_displacement:
            missing.append("WATCH_RELATIVE_CLOSE_DISPLACEMENT_LT_MIN_ATR")
        if adverse_close_invalid:
            missing.append("WATCH_RELATIVE_CLOSE_BEYOND_WATCH_EXTREME")
        if not position_ok:
            missing.append("WATCH_RELATIVE_15M_POSITION_NOT_REJECTED")
        separation_ok = bool(
            extreme_separation_atr is not None
            and extreme_separation_atr >= min_extreme_separation
        )
        if not directional_current_candle and not separation_ok:
            missing.append("WATCH_RELATIVE_NO_DIRECTIONAL_CANDLE_OR_EXTREME_SEPARATION")

        reset_filter = self._watched_extreme_reset_filter(side=side_u, watch=watch)
        if not bool(reset_filter.get("passes", True)):
            missing.append("WATCH_EXTREME_STALE_NEW_ADVERSE_EXTREME")

        passes = not missing
        strength = float(pa.get("strength") or 0.0)
        if passes and move_from_extreme_atr is not None:
            strength = max(
                strength,
                min(
                    100.0,
                    float(cfg.watch_relative_strength_base)
                    + (min(move_from_extreme_atr, max_move) * float(cfg.watch_relative_strength_per_atr)),
                ),
            )

        return {
            "passes": passes,
            "code": (
                cfg.watch_relative_confirmation_code
                if passes
                else "WATCH_RELATIVE_CONFIRMATION_NOT_READY"
            ),
            "side": side_u,
            "watch_snapshot_time": watch_ts.isoformat() if isinstance(watch_ts, datetime) else None,
            "current_snapshot_time": current_ts.isoformat() if isinstance(current_ts, datetime) else None,
            "watch_age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
            "watch_age_bars": age_bars,
            "min_age_bars": min_age,
            "max_age_bars": max_age,
            "watched_high": watched_high,
            "watched_low": watched_low,
            "watched_close": watched_close,
            "current_open": current_open,
            "current_high": current_high,
            "current_low": current_low,
            "current_close": close,
            "position_15m": position_15m,
            "position_ok": position_ok,
            "directional_current_candle": directional_current_candle,
            "extreme_separation_ok": separation_ok,
            "move_from_watch_extreme_points": (
                round(float(move_from_extreme_points), 4)
                if move_from_extreme_points is not None
                else None
            ),
            "move_from_watch_extreme_atr": (
                round(float(move_from_extreme_atr), 4)
                if move_from_extreme_atr is not None
                else None
            ),
            "close_displacement_from_watch_close_points": (
                round(float(close_displacement_points), 4)
                if close_displacement_points is not None
                else None
            ),
            "close_displacement_from_watch_close_atr": (
                round(float(close_displacement_atr), 4)
                if close_displacement_atr is not None
                else None
            ),
            "extreme_separation_points": (
                round(float(extreme_separation_points), 4)
                if extreme_separation_points is not None
                else None
            ),
            "extreme_separation_atr": (
                round(float(extreme_separation_atr), 4)
                if extreme_separation_atr is not None
                else None
            ),
            "min_move_from_extreme_atr": min_move,
            "max_move_from_extreme_atr": max_move,
            "min_close_displacement_from_watch_close_atr": min_close_displacement,
            "min_extreme_separation_atr": min_extreme_separation,
            "strength": round(strength, 2),
            "missing_requirements": missing,
            "watch_reset_filter": reset_filter,
            "rule": (
                "A recent exhaustion WATCH confirms when the completed sequence has moved "
                "meaningfully but not too far from the watched extreme, displaced from the "
                "WATCH close, and either the current candle is directional or the entire "
                "current candle is separated from the adverse extreme."
            ),
        }

    def _watched_extreme_promotion_filter(self, *, side: str, watch: Dict[str, Any], pa: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.cfg.exhaustion_reversal
        side_u = upper(side)
        current_move_atr = self._as_float(pa.get("current_move_atr"))
        current_close_position = self._as_float(pa.get("current_close_position"))
        close = self._as_float(pa.get("close"))
        watched_high = self._as_float(watch.get("high"))
        watched_low = self._as_float(watch.get("low"))
        atr = self._optional_numeric(self._snapshot, "indicators.atr.value")

        move_from_watch_extreme_points = None
        move_from_watch_extreme_atr = None
        if close is not None and atr is not None and atr > 0:
            if side_u == SELL and watched_high is not None:
                move_from_watch_extreme_points = max(0.0, watched_high - close)
            elif side_u == BUY and watched_low is not None:
                move_from_watch_extreme_points = max(0.0, close - watched_low)
            if move_from_watch_extreme_points is not None:
                move_from_watch_extreme_atr = move_from_watch_extreme_points / atr

        if not bool(pa.get("confirmed")):
            return {
                "passes": False,
                "missing_requirements": ["CURRENT_PRICE_ACTION_NOT_CONFIRMED"],
                "side": side_u,
                "watch": dict(watch),
            }

        watch_relative = (
            pa.get("watch_relative_confirmation")
            if isinstance(pa.get("watch_relative_confirmation"), dict)
            else {}
        )
        watch_relative_confirmed = bool(
            pa.get("watch_relative_confirmed")
            and watch_relative.get("passes")
        )

        reset_filter = self._watched_extreme_reset_filter(side=side_u, watch=watch)
        if not bool(reset_filter.get("passes", True)):
            return {
                "passes": False,
                "missing_requirements": ["WATCH_EXTREME_STALE_NEW_ADVERSE_EXTREME"],
                "side": side_u,
                "watch": dict(watch),
                "watch_reset_filter": reset_filter,
            }

        missing: List[str] = []
        min_move_atr = float(cfg.watch_promotion_min_move_atr)
        if side_u == SELL:
            current_candle_move_ok = current_move_atr is not None and current_move_atr <= -min_move_atr
            extreme_move_ok = move_from_watch_extreme_atr is not None and move_from_watch_extreme_atr >= min_move_atr
            strong_reversal_move = current_candle_move_ok or extreme_move_ok
            close_at_reversal_end = (
                watch_relative_confirmed
                or current_close_position is None
                or current_close_position <= float(cfg.watch_promotion_sell_close_position_max)
            )
            breaks_extreme_candle = (
                not bool(cfg.watch_promotion_require_extreme_candle_break)
                or watched_low is None
                or (close is not None and close < watched_low)
            )
            if close is not None and watched_high is not None and close > watched_high:
                missing.append("CONFIRM_CLOSE_ABOVE_WATCH_HIGH")
            if not strong_reversal_move:
                missing.append("LARGE_RED_REVERSAL_MOVE_FROM_CURRENT_OR_WATCH_EXTREME")
            if not close_at_reversal_end:
                missing.append("CLOSE_NEAR_LOW")
            if not breaks_extreme_candle:
                missing.append("NO_BREAK_OF_WATCH_CANDLE_LOW")
        elif side_u == BUY:
            current_candle_move_ok = current_move_atr is not None and current_move_atr >= min_move_atr
            extreme_move_ok = move_from_watch_extreme_atr is not None and move_from_watch_extreme_atr >= min_move_atr
            strong_reversal_move = current_candle_move_ok or extreme_move_ok
            close_at_reversal_end = (
                watch_relative_confirmed
                or current_close_position is None
                or current_close_position >= float(cfg.watch_promotion_buy_close_position_min)
            )
            breaks_extreme_candle = (
                not bool(cfg.watch_promotion_require_extreme_candle_break)
                or watched_high is None
                or (close is not None and close > watched_high)
            )
            if close is not None and watched_low is not None and close < watched_low:
                missing.append("CONFIRM_CLOSE_BELOW_WATCH_LOW")
            if not strong_reversal_move:
                missing.append("LARGE_GREEN_REVERSAL_MOVE_FROM_CURRENT_OR_WATCH_EXTREME")
            if not close_at_reversal_end:
                missing.append("CLOSE_NEAR_HIGH")
            if not breaks_extreme_candle:
                missing.append("NO_BREAK_OF_WATCH_CANDLE_HIGH")
        else:
            return {"passes": False, "missing_requirements": ["UNSUPPORTED_SIDE"]}

        passes = not missing
        return {
            "passes": passes,
            "missing_requirements": missing,
            "side": side_u,
            "current_move_atr": current_move_atr,
            "current_close_position": current_close_position,
            "current_close": close,
            "watched_high": watched_high,
            "watched_low": watched_low,
            "move_from_watch_extreme_points": round(float(move_from_watch_extreme_points), 4) if move_from_watch_extreme_points is not None else None,
            "move_from_watch_extreme_atr": round(float(move_from_watch_extreme_atr), 4) if move_from_watch_extreme_atr is not None else None,
            "min_reversal_move_atr": min_move_atr,
            "sell_close_position_max": float(cfg.watch_promotion_sell_close_position_max),
            "buy_close_position_min": float(cfg.watch_promotion_buy_close_position_min),
            "require_extreme_candle_break": bool(cfg.watch_promotion_require_extreme_candle_break),
            "watch_reset_filter": reset_filter,
            "watch_age_bars": (
                watch_relative.get("watch_age_bars")
                if watch_relative_confirmed
                else watch.get("age_bars")
            ),
            "watch_age_minutes": (
                watch_relative.get("watch_age_minutes")
                if watch_relative_confirmed
                else watch.get("age_minutes")
            ),
            "watch_relative_confirmed": watch_relative_confirmed,
            "watch_relative_confirmation": watch_relative,
            "watch_source": watch.get("source"),
            "rule": (
                "A prior RSI/BB extreme WATCH can promote only when a later candle "
                "confirms with reversal price action, closes near the reversal end, "
                "and has a meaningful move either on the current candle or from the watched extreme."
            ),
        }

    def _watch_promotion_strong_window_confirmation(
        self,
        *,
        side: str,
        promotion_filter: Dict[str, Any],
        pa: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Allow valid delayed WATCH confirmations without making weak bounces CREATE.

        Direct single-candle quality blocks are still useful for BIOCON-style
        weak bounces.  But a MUTHOOTFIN-style setup can have a modest current
        candle while the whole watch window has already rejected strongly from
        the watched high/low.  This helper gives that full-window rejection a
        separate, price-action-only path.
        """
        cfg = self.cfg.exhaustion_reversal
        side_u = upper(side)
        min_move_atr = float(cfg.watch_promotion_strong_window_min_move_atr)
        min_age_bars = int(cfg.watch_promotion_strong_window_min_age_bars)
        move_from_watch_extreme_atr = self._as_float(promotion_filter.get("move_from_watch_extreme_atr"))
        watch_age_bars = self._as_int(promotion_filter.get("watch_age_bars"))
        current_close_position = self._as_float(promotion_filter.get("current_close_position"))

        missing: List[str] = []
        if not bool(cfg.watch_promotion_strong_window_enabled):
            missing.append("STRONG_WINDOW_CONFIRMATION_DISABLED")
        if not bool(pa.get("confirmed")):
            missing.append("PRICE_ACTION_NOT_CONFIRMED")
        if not bool(promotion_filter.get("passes")):
            missing.append("WATCH_PROMOTION_FILTER_NOT_PASSED")
        if move_from_watch_extreme_atr is None or move_from_watch_extreme_atr < min_move_atr:
            missing.append("WATCH_WINDOW_MOVE_LT_MIN_ATR")
        if watch_age_bars is None or watch_age_bars < min_age_bars:
            missing.append("WATCH_AGE_LT_MIN_BARS")

        if side_u == SELL:
            close_near_reversal_end = (
                current_close_position is not None
                and current_close_position <= float(cfg.watch_promotion_sell_close_position_max)
            )
            if not close_near_reversal_end:
                missing.append("SELL_CLOSE_NOT_NEAR_LOW")
        elif side_u == BUY:
            close_near_reversal_end = (
                current_close_position is not None
                and current_close_position >= float(cfg.watch_promotion_buy_close_position_min)
            )
            if not close_near_reversal_end:
                missing.append("BUY_CLOSE_NOT_NEAR_HIGH")
        else:
            close_near_reversal_end = False
            missing.append("UNSUPPORTED_SIDE")

        passes = not missing
        return {
            "passes": passes,
            "code": cfg.watch_promotion_strong_window_code if passes else "WATCH_STRONG_WINDOW_CONFIRMATION_NOT_READY",
            "side": side_u,
            "move_from_watch_extreme_atr": move_from_watch_extreme_atr,
            "min_move_atr": min_move_atr,
            "watch_age_bars": watch_age_bars,
            "min_age_bars": min_age_bars,
            "current_close_position": current_close_position,
            "close_near_reversal_end": bool(close_near_reversal_end),
            "missing_requirements": missing,
            "rule": (
                "A WATCH promotion that is weak on the current candle alone can still CREATE "
                "only when the full watch-window rejection/reclaim is strong enough, old enough, "
                "and closes near the reversal end. This preserves delayed true exhaustion reversals "
                "without allowing small pause candles to become entries."
            ),
        }

    def _discover_watched_exhaustion_promotions(
        self,
        *,
        rule_priority: int,
        rule_strategy: str,
        current_base_data: Dict[str, Any],
    ) -> List[SetupCandidate]:
        cfg = self.cfg.exhaustion_reversal
        if not bool(cfg.watch_extreme_promotion_enabled):
            return []

        out: List[SetupCandidate] = []
        # Check both sides because the current candle may no longer satisfy the
        # original discovery conditions; the side comes from the remembered or
        # reconstructed WATCH extreme.
        for side in SIDES:
            key = self._exhaustion_watch_key(side)
            watch = self._exhaustion_watch_memory.get(key) if key is not None else None
            if isinstance(watch, dict) and not self._watched_extreme_is_fresh(watch):
                if key is not None:
                    self._exhaustion_watch_memory.pop(key, None)
                watch = None

            # Main path: reconstruct DISCOVER/WATCH memory from recent persisted
            # snapshots.  This catches AUBANK-type sequences where the extreme
            # candle was WATCH and the confirming red/green candle arrives after
            # RSI/BB have cooled.
            if not isinstance(watch, dict):
                watch = self._recent_extreme_exhaustion_watch(side=side)
            if not isinstance(watch, dict):
                continue

            pa = self._price_action_confirmation(side)
            watch_relative_confirmation = self._watch_relative_price_action_confirmation(
                side=side,
                watch=watch,
                pa=pa,
            )
            if bool(watch_relative_confirmation.get("passes")):
                # Treat the watch-to-current sequence as multi-candle price action.
                # This does not pretend the latest pause/bounce candle itself is
                # directional; it records the explicit confirmation mode.
                pa = dict(pa)
                pa.update({
                    "confirmed": True,
                    "raw_confirmed": True,
                    "multi_candle_confirmed": True,
                    "watch_relative_confirmed": True,
                    "watch_relative_confirmation": watch_relative_confirmation,
                    "confirmation_mode": "WATCH_RELATIVE_REJECTION_RECLAIM",
                    "strength": max(
                        float(pa.get("strength") or 0.0),
                        float(watch_relative_confirmation.get("strength") or 0.0),
                    ),
                })
            else:
                pa = dict(pa)
                pa["watch_relative_confirmed"] = False
                pa["watch_relative_confirmation"] = watch_relative_confirmation

            promotion_filter = self._watched_extreme_promotion_filter(side=side, watch=watch, pa=pa)
            if not bool(promotion_filter.get("passes")):
                reset_filter = promotion_filter.get("watch_reset_filter")
                if isinstance(reset_filter, dict) and not bool(reset_filter.get("passes", True)):
                    self._transition_setup_state(
                        side=side,
                        state=getattr(self.cfg.setup_state, "invalidated_state", "INVALIDATED"),
                        reason=getattr(self.cfg.setup_state, "invalidated_reason_code", "EXHAUSTION_REVERSAL_SETUP_STATE_INVALIDATED"),
                        details={
                            "watch": dict(watch),
                            "promotion_filter": promotion_filter,
                            "reset_filter": reset_filter,
                        },
                    )
                    if key is not None:
                        self._exhaustion_watch_memory.pop(key, None)
                # Keep in-memory watch alive until expiry for normal not-ready
                # cases. Snapshot-backed watch will be reconstructed on the next
                # bar if still valid and not terminal.
                continue

            setup_levels = self._apply_exhaustion_signal_reference(
                self._exhaustion_setup_levels(side),
                side=side,
                watch=watch,
                pa=pa,
            )
            if self._watch_reference_price(side=upper(side), watch=watch) is not None:
                setup_levels["watch_snapshot_time"] = str(watch.get("snapshot_time")) if watch.get("snapshot_time") is not None else None
            vwap_gap_pct = current_base_data.get("vwap_gap_pct")
            vwap_gap_abs = abs(vwap_gap_pct) if isinstance(vwap_gap_pct, (int, float)) else None
            vwap_too_close = (
                cfg.vwap_filter_enabled
                and vwap_gap_abs is not None
                and vwap_gap_abs < cfg.min_abs_vwap_gap_pct
            )
            location = self._entry_location_filter(
                side,
                vwap_blocked=bool(vwap_too_close),
                vwap_gap_pct=vwap_gap_pct,
                price_action=pa,
            )

            first_move_filter = self._first_move_consumed_filter(side=side, watch=watch, pa=pa)
            if not bool(first_move_filter.get("passes", True)):
                location = self._with_location_block(
                    location,
                    str(first_move_filter.get("code") or cfg.first_move_consumed_code),
                    first_move_consumed_filter=first_move_filter,
                )

            # WATCH promotion must not bypass price-action quality.  A weak
            # one-candle bounce/rejection after an RSI/BB extreme remains WATCH /
            # DEFER, unless the full watch window itself has produced a strong
            # rejection/reclaim from the watched extreme.  This is deliberately
            # price-action-only: no HMA, ADX, or slow structure hard block.
            watch_pa_quality_block = None
            strong_window_confirmation = None
            pa_quality_block = self._single_candle_reclaim_quality_blocker(
                setup_label=self.cfg.pattern.setup_exhaustion_reversal,
                side=side,
                pa=pa,
                min_move_atr=cfg.single_candle_strong_reversal_min_move_atr,
                buy_15m_position_min=cfg.single_candle_buy_15m_position_min,
                sell_15m_position_max=cfg.single_candle_sell_15m_position_max,
            )
            if pa_quality_block is not None:
                strong_window_confirmation = self._watch_promotion_strong_window_confirmation(
                    side=side,
                    promotion_filter=promotion_filter,
                    pa=pa,
                )
                if not bool(strong_window_confirmation.get("passes")):
                    watch_pa_quality_block = dict(pa_quality_block)
                    watch_pa_quality_block["code"] = cfg.watch_promotion_weak_single_candle_code
                    watch_pa_quality_block["direct_price_action_quality_code"] = pa_quality_block.get("code")
                    watch_pa_quality_block["watch_promotion_filter"] = promotion_filter
                    watch_pa_quality_block["strong_window_confirmation"] = strong_window_confirmation
                    watch_pa_quality_block["rule"] = (
                        "WATCH promotion cannot CREATE from a weak single-candle bounce/rejection. "
                        "It requires either direct strong single-candle reclaim/breakdown quality "
                        "or a strong full-window rejection/reclaim from the watched extreme."
                    )
                    location = self._with_location_block(
                        location,
                        cfg.watch_promotion_weak_single_candle_code,
                        watch_promotion_price_action_quality_filter=watch_pa_quality_block,
                    )


            confirmed = bool(pa.get("confirmed"))
            blocked = bool(location.get("blocked"))
            if confirmed and not blocked:
                code = (
                    cfg.watch_relative_confirmation_code
                    if bool(pa.get("watch_relative_confirmed"))
                    else self.cfg.reason.create_code
                )
            else:
                code = self.cfg.reason.blocked_by_location_code
            text = (
                f"{side} exhaustion reversal promoted from prior RSI/BB extreme WATCH: "
                "a later completed sequence confirmed rejection/reclaim price action."
            )
            if watch_pa_quality_block is not None:
                text = (
                    f"{side} watched exhaustion reversal remains deferred: "
                    "WATCH promotion was only a weak single-candle reversal."
                )
            elif blocked:
                text = f"{side} watched exhaustion reversal confirmed, but entry is deferred by location/tradability filter."

            setup_inputs = dict(current_base_data)
            setup_inputs["watched_extreme"] = dict(watch)
            setup_inputs["watch_promotion_filter"] = promotion_filter
            setup_inputs["watch_promotion_code"] = (
                cfg.watch_relative_confirmation_code
                if bool(pa.get("watch_relative_confirmed"))
                else cfg.watch_promotion_code
            )
            setup_inputs["watch_relative_confirmation"] = watch_relative_confirmation
            if strong_window_confirmation is not None:
                setup_inputs["watch_promotion_strong_window_confirmation"] = strong_window_confirmation
            if watch_pa_quality_block is not None:
                setup_inputs["watch_promotion_price_action_quality_filter"] = watch_pa_quality_block
            if first_move_filter is not None:
                setup_inputs["first_move_consumed_filter"] = first_move_filter
            self._write_exhaustion_candidate_state(
                side=side,
                watch=watch,
                pa=pa,
                location=location,
                reason_code=code,
            )
            out.append(SetupCandidate(
                setup_label=self.cfg.pattern.setup_exhaustion_reversal,
                strategy=rule_strategy,
                side=side,
                priority=rule_priority,
                discovered=True,
                price_action_confirmed=confirmed,
                price_action_strength=float(pa.get("strength") or 0.0),
                entry_blocked=blocked,
                blocked_by=location.get("blocked_by"),
                reason_code=code,
                reason_text=text,
                evidence_state="ENTRY_DEFERRED" if blocked else "ENTRY_READY",
                risk_flags=list(location.get("risk_flags") or []),
                data={
                    "setup_inputs": setup_inputs,
                    "price_action": pa,
                    "entry_location_filter": location,
                    "setup_levels": setup_levels,
                    "watch_promotion": {
                        "code": (
                            cfg.watch_relative_confirmation_code
                            if bool(pa.get("watch_relative_confirmed"))
                            else cfg.watch_promotion_code
                        ),
                        "watched_extreme": dict(watch),
                        "promotion_filter": promotion_filter,
                        "watch_relative_confirmation": watch_relative_confirmation,
                        "source": watch.get("source") or "IN_MEMORY_WATCH",
                    },
                },
            ))
            # Consume the in-memory watch once it has produced a valid candidate so
            # it cannot generate repeated same-side entries from the same extreme
            # event.  Snapshot-backed reconstruction remains naturally bounded by
            # the active-signal/same-day guards and the 5-candle lookback.
            if key is not None:
                self._exhaustion_watch_memory.pop(key, None)
        return out


    def _exhaustion_candidate_for_side(self, *, side: str, rule_priority: int, rule_strategy: str, base_data: Dict[str, Any]) -> SetupCandidate:
        pa = self._price_action_confirmation(side)
        vwap_blocked = bool(base_data.get("vwap_too_close"))
        setup_levels = self._exhaustion_setup_levels(side)
        location = self._entry_location_filter(
            side,
            vwap_blocked=vwap_blocked,
            vwap_gap_pct=base_data.get("vwap_gap_pct"),
            price_action=pa,
        )
        setup_label = self.cfg.pattern.setup_exhaustion_reversal

        terminal_gate = self._setup_state_terminal_gate(side=side, mark_cooldown=True)
        if terminal_gate is not None:
            location = self._with_location_block(
                location,
                terminal_gate["code"],
                setup_state_terminal_gate=terminal_gate,
            )

        # If this CREATE happens after an earlier RSI/BB WATCH extreme, keep
        # that original extreme as the quality/reference context.  This prevents
        # SOLARINDS-type entries where the confirmation candle low/high overwrote
        # the real watched low/high, hiding that the first move was already gone.
        watched_extreme_for_entry = None
        if terminal_gate is None:
            watched_extreme_for_entry = self._recent_extreme_exhaustion_watch(side=side)
        if isinstance(watched_extreme_for_entry, dict):
            setup_levels = self._apply_exhaustion_signal_reference(
                setup_levels,
                side=side,
                watch=watched_extreme_for_entry,
                pa=pa,
            )
            if self._watch_reference_price(side=upper(side), watch=watched_extreme_for_entry) is not None:
                setup_levels["watch_snapshot_time"] = (
                    str(watched_extreme_for_entry.get("snapshot_time"))
                    if watched_extreme_for_entry.get("snapshot_time") is not None
                    else None
                )
            first_move_filter = self._first_move_consumed_filter(side=side, watch=watched_extreme_for_entry, pa=pa)
            if not bool(first_move_filter.get("passes", True)):
                location = self._with_location_block(
                    location,
                    str(first_move_filter.get("code") or self.cfg.exhaustion_reversal.first_move_consumed_code),
                    first_move_consumed_filter=first_move_filter,
                )
        else:
            first_move_filter = None

        self._remember_exhaustion_watch_if_extreme(side=side, base_data=base_data, pa=pa)

        # Common price-action quality gate: no CREATE-capable setup should rely on
        # a marginal one-candle flip when there is no multi-candle confirmation.
        # For exhaustion reversal this does NOT use HMA; it only requires the
        # single candle itself to be a meaningful reclaim/breakdown.
        pa_quality_block = self._single_candle_reclaim_quality_blocker(
            setup_label=setup_label,
            side=side,
            pa=pa,
            min_move_atr=self.cfg.exhaustion_reversal.single_candle_strong_reversal_min_move_atr,
            buy_15m_position_min=self.cfg.exhaustion_reversal.single_candle_buy_15m_position_min,
            sell_15m_position_max=self.cfg.exhaustion_reversal.single_candle_sell_15m_position_max,
        )
        if pa_quality_block is not None:
            location = self._with_location_block(location, pa_quality_block["code"], price_action_quality_filter=pa_quality_block)


        confirmed = bool(pa["confirmed"])
        blocked = bool(location["blocked"])
        risk_flags = list(location["risk_flags"])
        if not confirmed:
            state = "SETUP_DISCOVERED"
            code = self.cfg.reason.setup_not_confirmed_code
            text = f"{side} exhaustion reversal discovered, but price action has not confirmed."
        elif blocked:
            state = "ENTRY_DEFERRED"
            code = self.cfg.reason.blocked_by_location_code
            text = f"{side} exhaustion reversal price action confirmed, but entry is deferred by common tradability/location filter."
        else:
            state = "ENTRY_READY"
            code = self.cfg.reason.create_code
            text = f"{side} exhaustion reversal has reversal confirmation; target/risk-reward is handled by trade manager."

        if terminal_gate is None and (
            isinstance(watched_extreme_for_entry, dict)
            or self._is_extreme_exhaustion_watch_location(side=side, base_data=base_data)
        ):
            ts = self._snapshot_dt()
            event_key_prefix = self._exhaustion_watch_key(side)
            if ts is None or event_key_prefix is None:
                raise ValueError(
                    "EXHAUSTION_REVERSAL direct candidate requires immutable "
                    "snapshot_time, symbol, and side"
                )
            watch = dict(watched_extreme_for_entry) if isinstance(watched_extreme_for_entry, dict) else {
                "setup_label": self.cfg.pattern.setup_exhaustion_reversal,
                "side": upper(side),
                "symbol": self._snapshot_symbol(),
                "snapshot_time": ts,
                "event_time": ts,
                "event_key": f"{event_key_prefix}|{ts.isoformat()}",
                "age_bars": 0,
                "age_minutes": 0.0,
                "open": self._optional_numeric(self._snapshot, "bar.open"),
                "high": self._optional_numeric(self._snapshot, "bar.high"),
                "low": self._optional_numeric(self._snapshot, "bar.low"),
                "close": self._optional_numeric(self._snapshot, "bar.close"),
                "rsi": self._as_float(base_data.get("rsi")),
                "bollinger_position": self._as_float(base_data.get("bollinger_position")),
                "bollinger_zone": upper(base_data.get("bollinger_zone") or ""),
                "sod_position": self._as_float(base_data.get("sod_position")),
                "position_30m": self._as_float(base_data.get("position_30m")),
                "sod_move_atr": self._as_float(base_data.get("sod_move_atr")),
                "move_30m_atr": self._as_float(base_data.get("move_30m_atr")),
                "move_60m_atr": self._as_float(base_data.get("move_60m_atr")),
                "vwap_gap_pct": self._as_float(base_data.get("vwap_gap_pct")),
                "valid_minutes": self._watch_valid_minutes_for_snapshot(),
                "source": "DIRECT_CURRENT_EXTREME",
                "promotion_policy": "CURRENT_EXTREME_PRICE_ACTION_CONFIRMED_OR_DEFERRED",
            }
            self._write_exhaustion_candidate_state(
                side=side,
                watch=watch,
                pa=pa,
                location=location,
                reason_code=code,
            )

        return SetupCandidate(
            setup_label=setup_label,
            strategy=rule_strategy,
            side=side,
            priority=rule_priority,
            discovered=True,
            price_action_confirmed=confirmed,
            price_action_strength=float(pa["strength"]),
            entry_blocked=blocked,
            blocked_by=location["blocked_by"],
            reason_code=code,
            reason_text=text,
            evidence_state=state,
            risk_flags=risk_flags,
            data={
                "setup_inputs": {
                    **base_data,
                    **({"watched_extreme": dict(watched_extreme_for_entry)} if isinstance(watched_extreme_for_entry, dict) else {}),
                    **({"first_move_consumed_filter": first_move_filter} if first_move_filter is not None else {}),
                },
                "price_action": pa,
                "entry_location_filter": location,
                "setup_levels": setup_levels,
            },
        )

    # ------------------------------------------------------------------
    # Strategy-neutral snapshot structure -> Evidence breakout observations
    # ------------------------------------------------------------------
    def _recent_close_series(self, d: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return ordered timestamped closes from the compact snapshot contract.

        Snapshot generation owns only causal market structure and a short close
        history. Evidence owns all breakout buffers, acceptance counts, reclaims
        and setup interpretation.
        """
        raw = self._optional_path(d, "structure.recent_closes") or []
        out: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                close = self._as_float(item.get("close"))
                ts = self._to_ist_naive_dt(item.get("time"))
                if close is None or ts is None:
                    continue
                out.append({"time": ts, "close": float(close)})

        # Defensive compatibility for a partially regenerated snapshot. The
        # current bar is enough for a developing observation, but mature/failed
        # acceptance still requires the configured close history.
        if not out:
            close = self._as_float(self._path_from(d, "bar.close"))
            ts = self._dt_from_snapshot_dict(d)
            if close is not None and ts is not None:
                out.append({"time": ts, "close": float(close)})

        out.sort(key=lambda x: x["time"])
        return out

    def _structural_level_candidates(self, d: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build causal structural levels available to Evidence.

        ORB/PDH/PDL and the current accepted range are distinct structures even
        when two boundaries happen to print at the same price.  Exact duplicate
        representations of the *same reference_id* are merged, but coincident
        ORB and dynamic-range prices remain separate observations.  Rank 10 is
        reserved for the current accepted range, so it is preferred over fixed
        ORB/previous-day references when both are equally eligible.
        """
        anchors = self._optional_path(d, "structure.anchors") or {}
        levels = self._optional_path(d, "levels") or {}
        opening = levels.get("opening_range") if isinstance(levels, dict) else {}
        prev_day = levels.get("prev_day") if isinstance(levels, dict) else {}
        accepted = self._accepted_range_context(d)
        coincident_tolerance = max(
            1e-6,
            float(
                getattr(
                    self.cfg.accepted_breakout,
                    "coincident_level_tolerance_points",
                    0.05,
                )
                or 0.0
            ),
        )

        out: List[Dict[str, Any]] = []

        def add(
            *,
            reference_id: str,
            level_type: str,
            price: Any,
            side: str,
            source: str,
            rank: int,
            aliases: Optional[List[str]] = None,
            range_context: Optional[Dict[str, Any]] = None,
        ) -> None:
            px = self._as_float(price)
            if px is None or side not in SIDES:
                return
            alias_values = [level_type, *(aliases or [])]
            for existing in out:
                # Merge only duplicate representations of the same causal
                # reference.  Coincident ORB and dynamic-range levels are not
                # aliases: they define different value structures and events.
                if existing.get("side") != side:
                    continue
                if str(existing.get("reference_id") or "") != str(reference_id):
                    continue
                if abs(float(existing["price"]) - float(px)) > coincident_tolerance:
                    continue
                merged = list(existing.get("aliases") or [])
                for alias in alias_values:
                    if alias and alias not in merged:
                        merged.append(alias)
                existing["aliases"] = merged
                existing["coincident_level_tolerance_points"] = coincident_tolerance
                existing.setdefault("sources", [])
                if source not in existing["sources"]:
                    existing["sources"].append(source)
                if range_context:
                    existing["range_context"] = {
                        **dict(existing.get("range_context") or {}),
                        **dict(range_context),
                    }
                return

            out.append({
                "reference_id": reference_id,
                "level_type": level_type,
                "price": float(px),
                "side": side,
                "source": source,
                "sources": [source],
                "rank": int(rank),
                "aliases": list(dict.fromkeys(alias_values)),
                "range_context": dict(range_context or {}),
                "coincident_level_tolerance_points": coincident_tolerance,
            })

        orb_ready = bool(
            (anchors.get("orb_ready") if isinstance(anchors, dict) else False)
            or (opening.get("ready") if isinstance(opening, dict) else False)
        )
        orb_high = (
            anchors.get("orb_high") if isinstance(anchors, dict) else None
        ) or (opening.get("high") if isinstance(opening, dict) else None)
        orb_low = (
            anchors.get("orb_low") if isinstance(anchors, dict) else None
        ) or (opening.get("low") if isinstance(opening, dict) else None)
        pdh = (
            anchors.get("pdh") if isinstance(anchors, dict) else None
        ) or (prev_day.get("high") if isinstance(prev_day, dict) else None)
        pdl = (
            anchors.get("pdl") if isinstance(anchors, dict) else None
        ) or (prev_day.get("low") if isinstance(prev_day, dict) else None)

        orb_range_context = {
            "range_id": "ORB",
            "version": 1,
            "source": "ORB",
            "range_type": "OPENING_RANGE",
            "high": self._as_float(orb_high),
            "low": self._as_float(orb_low),
            "start_time": opening.get("start_time") if isinstance(opening, dict) else None,
            "end_time": opening.get("end_time") if isinstance(opening, dict) else None,
            "established_at": opening.get("established_at") if isinstance(opening, dict) else None,
            "breakout_eligible": bool(orb_ready),
        }
        previous_day_range_context = {
            "range_id": "PREVIOUS_DAY",
            "version": 1,
            "source": "PREVIOUS_DAY",
            "range_type": "PREVIOUS_DAY_RANGE",
            "high": self._as_float(pdh),
            "low": self._as_float(pdl),
            "breakout_eligible": True,
        }

        if orb_ready:
            add(
                reference_id="ORB_HIGH",
                level_type="ORB_HIGH",
                price=orb_high,
                side=BUY,
                source="ORB",
                rank=20,
                range_context=orb_range_context,
            )
            add(
                reference_id="ORB_LOW",
                level_type="ORB_LOW",
                price=orb_low,
                side=SELL,
                source="ORB",
                rank=20,
                range_context=orb_range_context,
            )
        add(
            reference_id="PDH",
            level_type="PREVIOUS_DAY_HIGH",
            price=pdh,
            side=BUY,
            source="PREVIOUS_DAY",
            rank=30,
            range_context=previous_day_range_context,
        )
        add(
            reference_id="PDL",
            level_type="PREVIOUS_DAY_LOW",
            price=pdl,
            side=SELL,
            source="PREVIOUS_DAY",
            rank=30,
            range_context=previous_day_range_context,
        )

        if bool(accepted.get("breakout_eligible")):
            source = upper(accepted.get("source") or "ACCEPTED_RANGE")
            range_id = str(accepted.get("range_id") or "ACTIVE_RANGE")
            version = int(self._as_int(accepted.get("version")) or 0)
            range_context = {
                "range_id": accepted.get("range_id"),
                "version": version,
                "source": accepted.get("source"),
                "range_type": accepted.get("range_type"),
                "high": accepted.get("high"),
                "low": accepted.get("low"),
                "width_pct": accepted.get("width_pct"),
                "width_atr": accepted.get("width_atr"),
                "quality": accepted.get("quality"),
                "start_time": accepted.get("start_time"),
                "end_time": accepted.get("end_time"),
                "established_at": accepted.get("established_at"),
                "evidence_cutoff": accepted.get("evidence_cutoff"),
                "bars": accepted.get("bars"),
                "provisional": accepted.get("provisional"),
                "breakout_eligible": True,
            }
            if source == "ORB":
                high_type, low_type = "ORB_HIGH", "ORB_LOW"
                high_id, low_id = "ORB_HIGH", "ORB_LOW"
            elif source == "INTRADAY_BALANCE":
                high_type, low_type = "DYNAMIC_RANGE_HIGH", "DYNAMIC_RANGE_LOW"
                high_id = f"{range_id}:V{version}:HIGH"
                low_id = f"{range_id}:V{version}:LOW"
            else:
                high_type, low_type = "ACCEPTED_RANGE_HIGH", "ACCEPTED_RANGE_LOW"
                high_id = f"{range_id}:V{version}:HIGH"
                low_id = f"{range_id}:V{version}:LOW"
            add(
                reference_id=high_id,
                level_type=high_type,
                price=accepted.get("high"),
                side=BUY,
                source="STRUCTURE_ACCEPTED_RANGE",
                rank=10,
                aliases=["ACCEPTED_RANGE_HIGH"],
                range_context=range_context,
            )
            add(
                reference_id=low_id,
                level_type=low_type,
                price=accepted.get("low"),
                side=SELL,
                source="STRUCTURE_ACCEPTED_RANGE",
                rank=10,
                aliases=["ACCEPTED_RANGE_LOW"],
                range_context=range_context,
            )

        return sorted(out, key=lambda x: (int(x.get("rank") or 999), str(x.get("reference_id") or "")))

    @staticmethod
    def _trailing_count(values: List[float], predicate: Any) -> int:
        count = 0
        for value in reversed(values):
            if not predicate(value):
                break
            count += 1
        return count

    @staticmethod
    def _max_consecutive(values: List[float], predicate: Any) -> tuple[int, int | None, int | None]:
        best = 0
        best_start: int | None = None
        best_end: int | None = None
        current = 0
        current_start = 0
        for idx, value in enumerate(values):
            if predicate(value):
                if current == 0:
                    current_start = idx
                current += 1
                if current > best:
                    best = current
                    best_start = current_start
                    best_end = idx
            else:
                current = 0
        return best, best_start, best_end

    def _level_breakout_observation(self, d: Dict[str, Any], level: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        atr = self._optional_numeric(d, "indicators.atr.value")
        if atr is None or atr <= 0:
            return None
        closes = self._recent_close_series(d)
        if not closes:
            return None

        side = upper(level.get("side"))
        price = self._as_float(level.get("price"))
        if side not in SIDES or price is None:
            return None

        cfg = self.cfg.accepted_breakout
        range_type = upper((level.get("range_context") or {}).get("range_type") or "")
        is_micro = range_type == "MICRO_COMPRESSION"
        attempt_buffer = float(
            cfg.micro_compression_attempt_buffer_atr if is_micro else cfg.attempt_buffer_atr
        )
        acceptance_buffer = float(
            cfg.micro_compression_acceptance_buffer_atr if is_micro else cfg.acceptance_buffer_atr
        )
        strong_threshold = float(
            cfg.micro_compression_strong_displacement_min_atr
            if is_micro
            else cfg.strong_displacement_min_atr
        )
        reclaim_buffer = float(cfg.reclaim_buffer_atr)
        min_acceptance = max(1, int(cfg.min_bars_outside))

        observations: List[Dict[str, Any]] = []
        favorable_offsets: List[float] = []
        for item in closes:
            close = float(item["close"])
            offset = ((close - price) / atr) if side == BUY else ((price - close) / atr)
            favorable_offsets.append(float(offset))
            observations.append({
                "time": item["time"],
                "close": close,
                "offset_atr": round(float(offset), 6),
            })

        current_offset = favorable_offsets[-1]
        attempt_run = self._trailing_count(favorable_offsets, lambda x: x >= attempt_buffer)
        acceptance_run = self._trailing_count(favorable_offsets, lambda x: x >= acceptance_buffer)
        strong_current = current_offset >= strong_threshold

        reclaimed_run = self._trailing_count(favorable_offsets, lambda x: x <= -reclaim_buffer)
        failed = False
        prior_acceptance_run = 0
        prior_strong = False
        prior_start_idx: int | None = None
        prior_end_idx: int | None = None
        failed_time = None
        if reclaimed_run >= max(1, int(self.cfg.failed_breakout.min_bars_reclaimed)):
            pre_reclaim = favorable_offsets[:-reclaimed_run]
            # The breakout episode is the most recent non-reclaimed segment. A
            # close can soften below the acceptance buffer without invalidating
            # the episode until it actually crosses back through the level.
            segment_start = len(pre_reclaim)
            while segment_start > 0 and pre_reclaim[segment_start - 1] > -reclaim_buffer:
                segment_start -= 1
            segment = pre_reclaim[segment_start:]
            prior_acceptance_run, rel_start, rel_end = self._max_consecutive(
                segment, lambda x: x >= acceptance_buffer
            )
            prior_strong = any(x >= strong_threshold for x in segment)
            if prior_acceptance_run >= min_acceptance or prior_strong:
                failed = True
                if rel_start is not None:
                    prior_start_idx = segment_start + rel_start
                elif segment:
                    prior_start_idx = segment_start + max(
                        range(len(segment)), key=lambda i: segment[i]
                    )
                if rel_end is not None:
                    prior_end_idx = segment_start + rel_end
                else:
                    prior_end_idx = len(pre_reclaim) - 1 if pre_reclaim else None
                failed_time = observations[len(observations) - reclaimed_run]["time"]

        status = "NONE"
        acceptance_path = "NONE"
        bars_outside = 0
        attempt_time = None
        accepted_time = None
        bars_reclaimed = 0

        if failed:
            status = "FAILED_BREAKOUT"
            acceptance_path = "FAILED_AFTER_ACCEPTANCE"
            bars_outside = max(prior_acceptance_run, 1 if prior_strong else 0)
            bars_reclaimed = reclaimed_run
            if prior_start_idx is not None:
                attempt_time = observations[prior_start_idx]["time"]
            if prior_end_idx is not None:
                accepted_time = observations[prior_end_idx]["time"]
        elif acceptance_run >= min_acceptance:
            status = "ACCEPTED_BREAKOUT"
            acceptance_path = "MULTI_CLOSE_ACCEPTANCE"
            bars_outside = acceptance_run
            start_idx = len(observations) - acceptance_run
            attempt_time = observations[start_idx]["time"]
            accepted_time = observations[start_idx + min_acceptance - 1]["time"]
        elif strong_current:
            status = "ACCEPTED_BREAKOUT"
            acceptance_path = "STRONG_DISPLACEMENT_ACCEPTANCE"
            bars_outside = max(1, acceptance_run)
            attempt_time = observations[-1]["time"]
            accepted_time = observations[-1]["time"]
        elif attempt_run >= 1:
            status = "BREAKOUT_TESTING"
            acceptance_path = "EARLY_MAJOR_LEVEL_BREAK"
            bars_outside = attempt_run
            attempt_time = observations[len(observations) - attempt_run]["time"]
        elif current_offset > 0:
            status = "BREAKOUT_ATTEMPT"
            acceptance_path = "LEVEL_TEST"
            attempt_time = observations[-1]["time"]

        return {
            **level,
            "status": status,
            "acceptance_path": acceptance_path,
            "bars_outside": int(bars_outside),
            "bars_reclaimed": int(bars_reclaimed),
            "attempt_time": attempt_time,
            "accepted_time": accepted_time,
            "failed_time": failed_time,
            "current_offset_atr": round(float(current_offset), 6),
            "break_distance_atr": max(0.0, round(float(current_offset), 6)),
            "attempt_buffer_atr": attempt_buffer,
            "acceptance_buffer_atr": acceptance_buffer,
            "strong_displacement_min_atr": strong_threshold,
            "strong_displacement": bool(strong_current),
            "recent_close_observations": observations,
        }

    def _derived_breakout_observations(self, d: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for level in self._structural_level_candidates(d):
            observation = self._level_breakout_observation(d, level)
            if observation is not None:
                out.append(observation)
        return out

    def _primary_breakout_observation(self, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        priority = {
            "FAILED_BREAKOUT": 0,
            "ACCEPTED_BREAKOUT": 1,
            "BREAKOUT_TESTING": 2,
            "BREAKOUT_ATTEMPT": 3,
            "NONE": 4,
        }
        observations = self._derived_breakout_observations(d)
        active = [o for o in observations if upper(o.get("status")) != "NONE"]
        if not active:
            return None
        return sorted(
            active,
            key=lambda o: (
                priority.get(upper(o.get("status")), 9),
                int(o.get("rank") or 999),
                -abs(float(o.get("current_offset_atr") or 0.0)),
            ),
        )[0]

    # ------------------------------------------------------------------
    # New setup family 1: FAILED_BREAKOUT
    # ------------------------------------------------------------------
    def _failed_breakout_watch_event_key(self, watch: Dict[str, Any]) -> str:
        """Identify the causal failure, not mutable range context.

        This mirrors ACCEPTED_BREAKOUT: reference + original side + original
        event time define the event. Accepted-range identity and measurements
        are immutable payload context, but a later range refresh must not create
        a new failed-breakout event.
        """
        event_time = self._to_ist_naive_dt(watch.get("event_time"))
        side = upper(watch.get("breakout_side") or "")
        reference_id = str(watch.get("reference_id") or "")
        if event_time is None or side not in SIDES or not reference_id:
            raise ValueError(
                "FAILED_BREAKOUT event identity requires reference_id, breakout_side, and event_time"
            )
        return "|".join([reference_id, side, event_time.isoformat()])

    def _failed_breakout_same_live_event(
        self,
        *,
        stored_watch: Dict[str, Any],
        incoming_watch: Dict[str, Any],
        expires_at: Optional[datetime] = None,
    ) -> bool:
        """Match an active event using the ACCEPTED_BREAKOUT convention."""
        stored_key = str(stored_watch.get("event_key") or "")
        incoming_key = str(incoming_watch.get("event_key") or "")
        if stored_key and incoming_key and stored_key == incoming_key:
            return True

        stored_reference = str(stored_watch.get("reference_id") or "")
        incoming_reference = str(incoming_watch.get("reference_id") or "")
        stored_side = upper(stored_watch.get("breakout_side") or "")
        incoming_side = upper(incoming_watch.get("breakout_side") or "")
        stored_time = self._to_ist_naive_dt(stored_watch.get("event_time"))
        incoming_time = self._to_ist_naive_dt(incoming_watch.get("event_time"))
        if (
            not stored_reference
            or stored_reference != incoming_reference
            or stored_side not in SIDES
            or stored_side != incoming_side
            or stored_time is None
            or incoming_time is None
            or incoming_time < stored_time
        ):
            return False

        live_until = self._to_ist_naive_dt(expires_at)
        if live_until is None:
            valid_minutes = float(
                stored_watch.get("valid_minutes")
                or getattr(self.cfg.failed_breakout, "watch_event_valid_minutes", 15.5)
                or 15.5
            )
            live_until = stored_time + timedelta(minutes=valid_minutes)
        return incoming_time <= live_until

    def _failed_breakout_value_range_context(
        self,
        row: Dict[str, Any],
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return the value structure owned by the failed reference.

        Dynamic-range failures return into the frozen dynamic range. ORB
        failures return into the ORB range. Previous-day failures use the
        previous-day range when both boundaries are available.  Only legacy
        observations without a usable reference context fall back to the
        current accepted range.
        """
        context = observation.get("range_context")
        if isinstance(context, dict):
            high = self._as_float(context.get("high"))
            low = self._as_float(context.get("low"))
            if high is not None and low is not None and high > low:
                return {
                    **dict(context),
                    "high": high,
                    "low": low,
                    "version": self._as_int(context.get("version")),
                    "width_pct": self._as_float(context.get("width_pct")),
                    "width_atr": self._as_float(context.get("width_atr")),
                    "quality": self._as_float(context.get("quality")),
                    "bars": self._as_int(context.get("bars")),
                    "provisional": bool(context.get("provisional", False)),
                    "breakout_eligible": bool(context.get("breakout_eligible", True)),
                }

        # Compatibility for old/replayed snapshots created before structural
        # levels carried their own range context.
        level_type = upper(observation.get("level_type") or "")
        levels = self._optional_path(row, "levels") or {}
        if level_type in {"ORB_HIGH", "ORB_LOW"}:
            opening = levels.get("opening_range") if isinstance(levels, dict) else {}
            high = self._as_float(opening.get("high") if isinstance(opening, dict) else None)
            low = self._as_float(opening.get("low") if isinstance(opening, dict) else None)
            if high is not None and low is not None and high > low:
                return {
                    "range_id": "ORB",
                    "version": 1,
                    "source": "ORB",
                    "range_type": "OPENING_RANGE",
                    "high": high,
                    "low": low,
                    "start_time": opening.get("start_time"),
                    "end_time": opening.get("end_time"),
                    "established_at": opening.get("established_at"),
                    "breakout_eligible": True,
                }
        if level_type in {"PREVIOUS_DAY_HIGH", "PREVIOUS_DAY_LOW"}:
            prev_day = levels.get("prev_day") if isinstance(levels, dict) else {}
            high = self._as_float(prev_day.get("high") if isinstance(prev_day, dict) else None)
            low = self._as_float(prev_day.get("low") if isinstance(prev_day, dict) else None)
            if high is not None and low is not None and high > low:
                return {
                    "range_id": "PREVIOUS_DAY",
                    "version": 1,
                    "source": "PREVIOUS_DAY",
                    "range_type": "PREVIOUS_DAY_RANGE",
                    "high": high,
                    "low": low,
                    "breakout_eligible": True,
                }
        return self._accepted_range_context(row)

    def _failed_breakout_watch_from_observation(
        self,
        row: Dict[str, Any],
        observation: Dict[str, Any],
        *,
        age_bars: int,
        source: str,
    ) -> Optional[Dict[str, Any]]:
        cfg = self.cfg.failed_breakout
        breakout_side = upper(observation.get("side"))
        if breakout_side not in SIDES:
            return None
        candidate_side = opposite_side(breakout_side)

        event_time = (
            self._to_ist_naive_dt(observation.get("failed_time"))
            or self._dt_from_snapshot_dict(row)
        )
        current_time = self._snapshot_dt()
        if event_time is None or current_time is None or event_time > current_time:
            return None

        age_minutes = max(0.0, (current_time - event_time).total_seconds() / 60.0)
        valid_minutes = float(getattr(cfg, "watch_event_valid_minutes", 15.5) or 15.5)
        if age_minutes > valid_minutes:
            return None

        reference_level = {
            "reference_id": observation.get("reference_id"),
            "level_type": observation.get("level_type"),
            "price": observation.get("price"),
            "side": breakout_side,
            "source": observation.get("source"),
            "sources": observation.get("sources", []),
            "aliases": observation.get("aliases", []),
            "rank": int(self._as_int(observation.get("rank")) or 999),
            "role": "FAILED_BREAKOUT_REFERENCE",
            "range_context": observation.get("range_context", {}),
        }
        accepted = self._failed_breakout_value_range_context(row, observation)
        event_atr = self._optional_numeric(row, "indicators.atr.value")
        accepted_high = self._as_float(accepted.get("high"))
        accepted_low = self._as_float(accepted.get("low"))
        accepted_range_width_points = (
            accepted_high - accepted_low
            if accepted_high is not None and accepted_low is not None and accepted_high > accepted_low
            else None
        )
        accepted_range_width_atr = (
            accepted_range_width_points / event_atr
            if accepted_range_width_points is not None and event_atr is not None and event_atr > 0
            else None
        )

        watch = {
            "setup_label": self.cfg.pattern.setup_failed_breakout,
            "symbol": str(row.get("symbol") or self._snapshot_symbol()).strip().upper(),
            "event_time": event_time,
            "event_snapshot_time": self._dt_from_snapshot_dict(row) or event_time,
            "event_status": "FAILED_BREAKOUT",
            "breakout_side": breakout_side,
            "candidate_side": candidate_side,
            "breakout_reason": "exact_level_acceptance_reclaimed",
            "attempt_time": observation.get("attempt_time"),
            "accepted_time": observation.get("accepted_time"),
            "failed_time": observation.get("failed_time") or event_time,
            "bars_outside": int(self._as_int(observation.get("bars_outside")) or 0),
            "bars_reclaimed": int(self._as_int(observation.get("bars_reclaimed")) or 0),
            "accepted_range": accepted,
            "accepted_range_id": accepted.get("range_id"),
            "accepted_range_version": accepted.get("version"),
            "accepted_range_width_points": accepted_range_width_points,
            "accepted_range_width_atr": accepted_range_width_atr,
            "accepted_range_measurement_atr": event_atr,
            "value_structure_source": accepted.get("source"),
            "value_structure_type": accepted.get("range_type"),
            "failed_level": reference_level,
            "reference_id": observation.get("reference_id"),
            "level_type": reference_level.get("level_type"),
            "level_price": reference_level.get("price"),
            "level_source": reference_level.get("source"),
            "level_rank": reference_level.get("rank"),
            "event_close": self._as_float(self._path_from(row, "bar.close")),
            "event_high": self._as_float(self._path_from(row, "bar.high")),
            "event_low": self._as_float(self._path_from(row, "bar.low")),
            "event_atr": event_atr,
            "age_bars": int(age_bars),
            "age_minutes": round(float(age_minutes), 2),
            "valid_bars": int(getattr(cfg, "watch_event_lookback_bars", 5) or 5),
            "valid_minutes": valid_minutes,
            "event_source": "EVIDENCE_NEUTRAL_LEVEL_OBSERVATION",
            "source": source,
            "evaluation_source": source,
            "policy": "EVIDENCE_DERIVED_FAILED_BREAKOUT_THEN_CONFIRM_WITHIN_5_CANDLES",
            "level_observation": observation,
        }
        watch["event_key"] = self._failed_breakout_watch_event_key(watch)
        return watch

    def _failed_breakout_watches_from_snapshot(
        self,
        row: Dict[str, Any],
        *,
        age_bars: int,
        source: str,
    ) -> List[Dict[str, Any]]:
        failed = [
            item
            for item in self._derived_breakout_observations(row)
            if upper(item.get("status")) == "FAILED_BREAKOUT"
        ]
        failed.sort(
            key=lambda item: (
                self._to_ist_naive_dt(item.get("failed_time")) or datetime.min,
                -int(item.get("rank") or 999),
                str(item.get("reference_id") or ""),
            ),
            reverse=True,
        )
        watches: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for observation in failed:
            watch = self._failed_breakout_watch_from_observation(
                row,
                observation,
                age_bars=age_bars,
                source=source,
            )
            if not isinstance(watch, dict):
                continue
            event_key = str(watch.get("event_key") or "")
            if not event_key or event_key in seen:
                continue
            seen.add(event_key)
            watches.append(watch)
        return watches

    def _failed_breakout_watch_from_snapshot(
        self,
        row: Dict[str, Any],
        *,
        age_bars: int,
        source: str,
    ) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper returning the highest-priority current event."""
        watches = self._failed_breakout_watches_from_snapshot(
            row,
            age_bars=age_bars,
            source=source,
        )
        return watches[0] if watches else None

    def _failed_breakout_state_row(self, *, candidate_side: str) -> Optional[Any]:
        return self._safe_setup_state_fetch(
            side=candidate_side,
            setup_label=self.cfg.pattern.setup_failed_breakout,
        )

    def _failed_breakout_same_event_is_terminal(self, watch: Dict[str, Any]) -> bool:
        if not self._setup_state_read_watch_enabled():
            return False
        row = self._failed_breakout_state_row(candidate_side=upper(watch.get("candidate_side")))
        if row is None:
            return False
        state_json = getattr(row, "state_json", None) or {}
        if not isinstance(state_json, dict):
            return False

        terminal_states = set(self._setup_state_terminal_states())
        # SUPERSEDED is terminal for the specific archived event even though it
        # is not a terminal state for the reusable setup/side row itself.
        terminal_states.add("SUPERSEDED")

        # First inspect the event currently occupying the reusable row.
        state = upper(getattr(row, "state", ""))
        prior_watch = state_json.get("watch")
        if state in terminal_states and isinstance(prior_watch, dict):
            if self._failed_breakout_same_live_event(
                stored_watch=prior_watch,
                incoming_watch=watch,
                expires_at=self._to_ist_naive_dt(getattr(row, "expires_at", None)),
            ):
                return True

        # stock_setup_state is one row per setup/side, so a newer structural
        # event can replace the current row while an older ORB/dynamic event is
        # archived in event_history.  A consumed/invalidated/expired/superseded
        # event must remain terminal there and must never be rediscovered merely
        # because another reference currently occupies the row.
        incoming_key = str(watch.get("event_key") or "")
        if not incoming_key:
            return False
        for archived in state_json.get("event_history") or []:
            if not isinstance(archived, dict):
                continue
            archived_state = upper(archived.get("final_state") or "")
            archived_key = str(archived.get("event_key") or "")
            if archived_state in terminal_states and archived_key == incoming_key:
                return True
        return False

    def _failed_breakout_watch_from_state_row(self, row: Any) -> Optional[Dict[str, Any]]:
        """Rehydrate one immutable FAILED_BREAKOUT event from setup state.

        The original failed level, accepted range, event ATR and event key are
        kept exactly as discovered.  Only watch age/source are refreshed for
        the current snapshot.
        """
        state_json = getattr(row, "state_json", None) or {}
        watch = state_json.get("watch") if isinstance(state_json, dict) else None
        if not isinstance(watch, dict):
            return None
        watch = dict(watch)
        event_time = self._to_ist_naive_dt(watch.get("event_time"))
        now = self._snapshot_dt()
        if event_time is None or now is None or event_time > now:
            return None
        age_minutes = max(0.0, (now - event_time).total_seconds() / 60.0)
        valid_minutes = float(
            watch.get("valid_minutes")
            or getattr(self.cfg.failed_breakout, "watch_event_valid_minutes", 15.5)
            or 15.5
        )
        if age_minutes > valid_minutes:
            return None
        watch["event_time"] = event_time
        watch["age_minutes"] = round(age_minutes, 2)
        watch["age_bars"] = max(0, int(round(age_minutes / 3.0)))
        watch["valid_bars"] = int(
            watch.get("valid_bars")
            or getattr(self.cfg.failed_breakout, "watch_event_lookback_bars", 5)
            or 5
        )
        watch["valid_minutes"] = valid_minutes
        watch.setdefault("event_source", "EVIDENCE_NEUTRAL_LEVEL_OBSERVATION")
        watch["evaluation_source"] = "PERSISTED_FAILED_BREAKOUT_EVENT"
        return watch

    def _failed_breakout_active_persisted_watches(self) -> List[Dict[str, Any]]:
        if not self._setup_state_read_watch_enabled():
            return []
        active_states = {
            upper(getattr(self.cfg.setup_state, "watch_state", "WATCH")),
            upper(getattr(self.cfg.setup_state, "confirmed_state", "CONFIRMED")),
            upper(getattr(self.cfg.setup_state, "confirmed_pending_state", "CONFIRMED_PENDING")),
            upper(getattr(self.cfg.setup_state, "confirmed_deferred_state", "CONFIRMED_DEFERRED")),
        }
        watches: List[Dict[str, Any]] = []
        for candidate_side in SIDES:
            row = self._failed_breakout_state_row(candidate_side=candidate_side)
            if row is None or upper(getattr(row, "state", "")) not in active_states:
                continue
            watch = self._failed_breakout_watch_from_state_row(row)
            if watch is not None:
                watches.append(watch)
        watches.sort(
            key=lambda item: (
                self._to_ist_naive_dt(item.get("event_time")) or datetime.min,
                -int(self._as_int(item.get("level_rank")) or 999),
            ),
            reverse=True,
        )
        return watches

    def _failed_breakout_active_persisted_watch(self) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper returning the newest persisted event."""
        watches = self._failed_breakout_active_persisted_watches()
        return watches[0] if watches else None

    def _failed_breakout_watch_invalidation_evaluation(
        self,
        watch: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Evaluate reacceptance outside the failed level using frozen event ATR."""
        policy = self.cfg.signal_invalidation.failed_breakout
        breakout_side = upper(watch.get("breakout_side"))
        level = self._as_float(watch.get("level_price"))
        event_atr = self._as_float(watch.get("event_atr"))
        event_time = self._to_ist_naive_dt(watch.get("event_time"))
        result: Dict[str, Any] = {
            "mode": getattr(policy, "mode", "REACCEPT_OUTSIDE_FAILED_LEVEL"),
            "breakout_side": breakout_side,
            "level_price": level,
            "event_atr": event_atr,
            "event_time": event_time.isoformat() if isinstance(event_time, datetime) else None,
            "invalidated": False,
        }
        if breakout_side not in SIDES or level is None or event_atr is None or event_atr <= 0:
            result["evaluation_available"] = False
            return result

        closes: List[Dict[str, Any]] = []
        offsets: List[float] = []
        for item in self._recent_close_series(self._snapshot):
            item_time = self._to_ist_naive_dt(item.get("time"))
            close = self._as_float(item.get("close"))
            if item_time is None or close is None:
                continue
            if event_time is not None and item_time < event_time:
                continue
            offset = (
                (close - level) / event_atr
                if breakout_side == BUY
                else (level - close) / event_atr
            )
            closes.append({"time": item_time.isoformat(), "close": close})
            offsets.append(float(offset))

        buffer_atr = max(0.0, float(getattr(policy, "buffer_atr", 0.20) or 0.20))
        strong_atr = max(
            buffer_atr,
            float(getattr(policy, "strong_single_close_atr", 0.50) or 0.50),
        )
        required = max(1, int(getattr(policy, "required_consecutive_closes", 2) or 2))
        current_offset = offsets[-1] if offsets else None
        consecutive = 0
        for value in reversed(offsets):
            if value >= buffer_atr:
                consecutive += 1
            else:
                break
        strong_single = current_offset is not None and current_offset >= strong_atr
        invalidated = bool(strong_single or consecutive >= required)
        result.update({
            "evaluation_available": bool(offsets),
            "recent_closes": closes,
            "offsets_atr": offsets,
            "current_offset_atr": current_offset,
            "buffer_atr": buffer_atr,
            "strong_single_close_atr": strong_atr,
            "required_consecutive_closes": required,
            "consecutive_outside_closes": consecutive,
            "strong_outside_close": strong_single,
            "invalidated": invalidated,
        })
        return result

    def _refresh_failed_breakout_watch_states(self) -> None:
        """Invalidate or expire persisted watches even without a fresh event row."""
        if not self._setup_state_read_watch_enabled():
            return
        now = self._snapshot_dt()
        active_states = {
            upper(getattr(self.cfg.setup_state, "watch_state", "WATCH")),
            upper(getattr(self.cfg.setup_state, "confirmed_state", "CONFIRMED")),
            upper(getattr(self.cfg.setup_state, "confirmed_pending_state", "CONFIRMED_PENDING")),
            upper(getattr(self.cfg.setup_state, "confirmed_deferred_state", "CONFIRMED_DEFERRED")),
        }
        for candidate_side in SIDES:
            row = self._failed_breakout_state_row(candidate_side=candidate_side)
            if row is None or upper(getattr(row, "state", "")) not in active_states:
                continue
            state_json = getattr(row, "state_json", None) or {}
            watch = state_json.get("watch") if isinstance(state_json, dict) else None
            if not isinstance(watch, dict):
                continue
            evaluation = self._failed_breakout_watch_invalidation_evaluation(watch)
            expires_at = self._to_ist_naive_dt(getattr(row, "expires_at", None))
            if bool(evaluation.get("invalidated")):
                self._transition_setup_state(
                    side=candidate_side,
                    setup_label=self.cfg.pattern.setup_failed_breakout,
                    state=getattr(self.cfg.setup_state, "invalidated_state", "INVALIDATED"),
                    reason=getattr(
                        self.cfg.failed_breakout,
                        "watch_invalidated_reason_code",
                        "FAILED_BREAKOUT_EVENT_WATCH_INVALIDATED",
                    ),
                    signal_id=getattr(row, "signal_id", None),
                    expires_at=expires_at,
                    details={
                        "event": "FAILED_BREAKOUT_WATCH_INVALIDATED",
                        "event_key": watch.get("event_key"),
                        "snapshot_time": now.isoformat() if isinstance(now, datetime) else None,
                        "evaluation": evaluation,
                    },
                )
            elif expires_at is not None and now is not None and now > expires_at:
                self._transition_setup_state(
                    side=candidate_side,
                    setup_label=self.cfg.pattern.setup_failed_breakout,
                    state=getattr(self.cfg.setup_state, "expired_state", "EXPIRED"),
                    reason=getattr(
                        self.cfg.failed_breakout,
                        "watch_expired_reason_code",
                        "FAILED_BREAKOUT_EVENT_WATCH_EXPIRED",
                    ),
                    signal_id=getattr(row, "signal_id", None),
                    expires_at=expires_at,
                    details={
                        "event": "FAILED_BREAKOUT_WATCH_EXPIRED",
                        "event_key": watch.get("event_key"),
                        "snapshot_time": now.isoformat(),
                        "expires_at": expires_at.isoformat(),
                    },
                )

    def _expire_stale_failed_breakout_states(self) -> None:
        if not self._setup_state_read_watch_enabled() or not self._setup_state_write_enabled():
            return
        ts = self._snapshot_dt()
        equity_ref = self._snapshot_equity_ref()
        if ts is None:
            raise ValueError("FAILED_BREAKOUT expiry sweep requires snapshot.snapshot_time")
        if not equity_ref:
            raise ValueError("FAILED_BREAKOUT expiry sweep requires equity_ref/symbol")
        try:
            rows = StockSetupStateSchema.fetch_states_for_symbol(
                trading_day=ts.date(),
                equity_ref=equity_ref,
            )
        except Exception:
            if bool(getattr(self.cfg.setup_state, "fail_silently", True)):
                return
            raise

        active_states = {
            upper(getattr(self.cfg.setup_state, "watch_state", "WATCH")),
            upper(getattr(self.cfg.setup_state, "confirmed_state", "CONFIRMED")),
            upper(getattr(self.cfg.setup_state, "confirmed_pending_state", "CONFIRMED_PENDING")),
            upper(getattr(self.cfg.setup_state, "confirmed_deferred_state", "CONFIRMED_DEFERRED")),
        }
        for row in rows:
            if upper(getattr(row, "setup", "")) != upper(self.cfg.pattern.setup_failed_breakout):
                continue
            if upper(getattr(row, "state", "")) not in active_states:
                continue
            expires_at = self._to_ist_naive_dt(getattr(row, "expires_at", None))
            if expires_at is None or ts <= expires_at:
                continue
            self._transition_setup_state(
                side=upper(getattr(row, "side", "")),
                setup_label=self.cfg.pattern.setup_failed_breakout,
                state=getattr(self.cfg.setup_state, "expired_state", "EXPIRED"),
                reason=getattr(self.cfg.failed_breakout, "watch_expired_reason_code", "FAILED_BREAKOUT_EVENT_WATCH_EXPIRED"),
                signal_id=getattr(row, "signal_id", None),
                expires_at=expires_at,
                details={
                    "event": "FAILED_BREAKOUT_WATCH_EXPIRED",
                    "expires_at": expires_at.isoformat(),
                    "snapshot_time": ts.isoformat(),
                },
            )

    def _recent_failed_breakout_watches(self) -> List[Dict[str, Any]]:
        cfg = self.cfg.failed_breakout
        if not bool(getattr(cfg, "watch_event_enabled", True)):
            return self._failed_breakout_watches_from_snapshot(
                self._snapshot,
                age_bars=0,
                source="CURRENT_FAILED_BREAKOUT_EVENT",
            )

        persisted_watches = self._failed_breakout_active_persisted_watches()
        current_watches = self._failed_breakout_watches_from_snapshot(
            self._snapshot,
            age_bars=0,
            source="CURRENT_FAILED_BREAKOUT_EVENT",
        )
        resolved: List[Dict[str, Any]] = []
        for current in current_watches:
            if self._failed_breakout_same_event_is_terminal(current):
                continue
            persisted = next(
                (
                    item
                    for item in persisted_watches
                    if self._failed_breakout_same_live_event(
                        stored_watch=item,
                        incoming_watch=current,
                    )
                ),
                None,
            )
            resolved.append(dict(persisted or current))

        if resolved:
            current_sides = {upper(item.get("candidate_side")) for item in resolved}
            resolved.extend(
                dict(item)
                for item in persisted_watches
                if upper(item.get("candidate_side")) not in current_sides
            )
            resolved.sort(
                key=lambda item: (
                    int(self._as_int(item.get("level_rank")) or 999),
                    -(
                        self._to_ist_naive_dt(item.get("event_time"))
                        or datetime(1970, 1, 1)
                    ).timestamp(),
                    str(item.get("reference_id") or ""),
                )
            )
            return resolved

        if persisted_watches:
            return persisted_watches

        lookback = max(1, int(getattr(cfg, "watch_event_lookback_bars", 5) or 5))
        recent = self._recent_snapshot_dicts_for_setup_watch(lookback_bars=lookback)
        for age_bars, row in enumerate(reversed(recent), start=1):
            watches = self._failed_breakout_watches_from_snapshot(
                row,
                age_bars=age_bars,
                source="RECENT_SNAPSHOT_FAILED_BREAKOUT_EVENT",
            )
            eligible = [
                watch
                for watch in watches
                if not self._failed_breakout_same_event_is_terminal(watch)
            ]
            if eligible:
                return eligible

        self._expire_stale_failed_breakout_states()
        return []

    def _recent_failed_breakout_watch(self) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper returning the preferred current event."""
        watches = self._recent_failed_breakout_watches()
        return watches[0] if watches else None

    def _write_failed_breakout_candidate_state(
        self,
        *,
        watch: Dict[str, Any],
        candidate: SetupCandidate,
    ) -> None:
        if not self._setup_state_write_enabled():
            return
        ts = self._snapshot_dt()
        equity_ref = self._snapshot_equity_ref()
        symbol = self._snapshot_symbol()
        event_time = self._to_ist_naive_dt(watch.get("event_time"))
        if ts is None:
            raise ValueError("FAILED_BREAKOUT persistence requires snapshot.snapshot_time")
        if event_time is None:
            raise ValueError("FAILED_BREAKOUT persistence requires immutable event_time")
        if not equity_ref:
            raise ValueError("FAILED_BREAKOUT persistence requires equity_ref/symbol")

        cfg = self.cfg.failed_breakout
        pa = dict(candidate.data.get("price_action") or {})
        location = dict(candidate.data.get("entry_location_filter") or {})
        setup_levels = dict(candidate.data.get("setup_levels") or {})
        confirmed = bool(candidate.price_action_confirmed)
        blocked = bool(candidate.entry_blocked)
        if confirmed:
            state = (
                getattr(self.cfg.setup_state, "confirmed_state", "CONFIRMED")
                if not blocked
                else getattr(self.cfg.setup_state, "confirmed_deferred_state", "CONFIRMED_DEFERRED")
            )
            reason = getattr(cfg, "watch_confirmed_reason_code", "FAILED_BREAKOUT_EVENT_WATCH_CONFIRMED")
        else:
            state = getattr(self.cfg.setup_state, "watch_state", "WATCH")
            reason = getattr(cfg, "watch_state_reason_code", "FAILED_BREAKOUT_EVENT_WATCH")
        expires_at = event_time + timedelta(
            minutes=float(getattr(cfg, "watch_event_valid_minutes", 15.5) or 15.5)
        )
        reference_price = self._as_float(setup_levels.get("reference_price") or watch.get("level_price"))

        self._safe_setup_state_upsert({
            "trading_day": ts.date(),
            "equity_ref": equity_ref,
            "symbol": symbol or equity_ref,
            "lifecycle": getattr(self.cfg, "lifecycle_name", "DEFAULT"),
            "setup": self.cfg.pattern.setup_failed_breakout,
            "side": upper(candidate.side),
            "state": state,
            "state_reason": reason,
            "first_seen_time": event_time,
            "last_seen_time": ts,
            "expires_at": expires_at,
            "age_bars": int(watch.get("age_bars") or 0),
            "discovery_price": watch.get("event_close"),
            "discovery_extreme_price": watch.get("level_price"),
            "confirmation_price": pa.get("close") if confirmed else None,
            "confirmation_time": ts if confirmed else None,
            "reference_price": reference_price,
            "reference_source": setup_levels.get("reference_source") or watch.get("level_source"),
            "signal_id": None,
            "state_json": {
                "event_key": watch.get("event_key"),
                "event_source": watch.get("event_source") or watch.get("source"),
                "event_time": watch.get("event_time"),
                "watch": dict(watch),
                "current_evaluation": {
                    "snapshot_time": ts.isoformat(),
                    "price_action_confirmed": confirmed,
                    "entry_blocked": blocked,
                    "blocked_by": candidate.blocked_by,
                    "reason_code": candidate.reason_code,
                    "price_action": pa,
                    "entry_location_filter": location,
                    "setup_levels": setup_levels,
                },
                "source": "SETUP_DISCOVERY_HELPER",
            },
        })

    def _failed_breakout_candidate_from_watch(
        self,
        watch: Dict[str, Any],
        *,
        persist_state: bool = True,
    ) -> Optional[SetupCandidate]:
        """Evaluate one immutable FAILED_BREAKOUT event on the current snapshot.

        Production discovery and read-only diagnostics use this same evaluator.
        The caller owns event selection/lifecycle; this method recalculates only
        current confirmation and location against the frozen WATCH payload.
        """
        cfg = self.cfg.failed_breakout
        rule = self.cfg.setup_discovery.setup_rules[self.cfg.pattern.setup_failed_breakout]

        breakout_side = upper(watch.get("breakout_side"))
        candidate_side = upper(watch.get("candidate_side"))
        if breakout_side not in SIDES or candidate_side not in SIDES:
            return None

        reference_level = watch.get("failed_level")
        accepted = watch.get("accepted_range")
        if not isinstance(reference_level, dict) or not isinstance(accepted, dict):
            return None

        close = require_numeric(self._snapshot, "bar.close")
        inside_range = self._is_inside_accepted_range(close=close, accepted=accepted)
        bars_reclaimed = int(self._as_int(watch.get("bars_reclaimed")) or 0)
        current_observation = self._primary_breakout_observation(self._snapshot)
        current_status = upper((current_observation or {}).get("status") or "NONE")
        watch_invalidation = self._failed_breakout_watch_invalidation_evaluation(watch)
        setup_inputs = {
            "setup_family": self.cfg.pattern.setup_failed_breakout,
            "breakout_status": watch.get("event_status") or "FAILED_BREAKOUT",
            "current_structure_breakout_status": current_status,
            "breakout_side": breakout_side,
            "candidate_side": candidate_side,
            "breakout_reason": watch.get("breakout_reason"),
            "attempt_time": watch.get("attempt_time"),
            "accepted_time": watch.get("accepted_time"),
            "failed_time": watch.get("failed_time"),
            "bars_outside": int(self._as_int(watch.get("bars_outside")) or 0),
            "bars_reclaimed": bars_reclaimed,
            "min_bars_reclaimed": cfg.min_bars_reclaimed,
            "close": close,
            "inside_accepted_range": inside_range,
            "accepted_range": accepted,
            "accepted_range_id": accepted.get("range_id"),
            "accepted_range_version": accepted.get("version"),
            "accepted_range_width_points": watch.get("accepted_range_width_points"),
            "accepted_range_width_atr": watch.get("accepted_range_width_atr"),
            "accepted_range_measurement_atr": watch.get("accepted_range_measurement_atr"),
            "failed_level": reference_level,
            "level_observation": watch.get("level_observation"),
            "reference_id": reference_level.get("reference_id") or watch.get("reference_id"),
            "level_type": reference_level.get("level_type"),
            "level_price": reference_level.get("price"),
            "level_source": reference_level.get("source"),
            "level_rank": int(self._as_int(reference_level.get("rank") or watch.get("level_rank")) or 999),
            "value_structure_source": watch.get("value_structure_source") or accepted.get("source"),
            "value_structure_type": watch.get("value_structure_type") or accepted.get("range_type"),
            "failed_breakout_watch": dict(watch),
            "watch_event_time": watch.get("event_time"),
            "watch_age_bars": int(watch.get("age_bars") or 0),
            "watch_age_minutes": self._as_float(watch.get("age_minutes")),
            "watch_valid_bars": int(watch.get("valid_bars") or cfg.watch_event_lookback_bars),
            "watch_valid_minutes": self._as_float(watch.get("valid_minutes")),
            "watch_source": watch.get("event_source") or watch.get("source"),
            "watch_evaluation_source": watch.get("evaluation_source") or watch.get("source"),
            "watch_event_key": watch.get("event_key"),
            "watch_event_atr": watch.get("event_atr"),
            "watch_invalidation": watch_invalidation,
        }

        age_bars = int(watch.get("age_bars") or 0)
        candidate = self._structural_reversal_candidate_for_side(
            setup_label=self.cfg.pattern.setup_failed_breakout,
            side=candidate_side,
            original_breakout_side=breakout_side,
            rule_priority=rule.priority,
            rule_strategy=rule.strategy,
            atr_buffer=cfg.invalidation_atr_buffer,
            require_inside_accepted_range=cfg.require_inside_accepted_range,
            min_bars_reclaimed=cfg.min_bars_reclaimed,
            bars_reclaimed=bars_reclaimed,
            inside_range=inside_range,
            accepted_range=accepted,
            frozen_accepted_range_width_points=self._as_float(watch.get("accepted_range_width_points")),
            frozen_accepted_range_width_atr=self._as_float(watch.get("accepted_range_width_atr")),
            level_price=float(reference_level["price"]),
            base_data=setup_inputs,
            block_entry_if_opposite_exhaustion=cfg.block_entry_if_opposite_exhaustion,
            allow_create=rule.allow_create,
            not_confirmed_text=(
                f"{candidate_side} failed breakout event is being watched "
                f"({age_bars}/{cfg.watch_event_lookback_bars} bars), but price action has not confirmed."
            ),
            blocked_text=(
                f"{candidate_side} failed breakout watch is price-action confirmed but deferred "
                "by the unchanged sustain/location filters."
            ),
            ready_text=(
                f"{candidate_side} failed breakout event confirmed within the "
                f"{cfg.watch_event_lookback_bars}-bar watch window; entry location is acceptable."
            ),
        )
        if persist_state:
            self._write_failed_breakout_candidate_state(watch=watch, candidate=candidate)
        return candidate

    def _failed_breakout_candidate_sort_key(self, candidate: SetupCandidate) -> tuple:
        location = candidate.data.get("entry_location_filter", {}) if isinstance(candidate.data, dict) else {}
        inputs = candidate.data.get("setup_inputs", {}) if isinstance(candidate.data, dict) else {}
        rank = self._as_int(inputs.get("level_rank")) or 999
        entry_distance = self._as_float(location.get("entry_distance_from_level_atr"))
        return (
            1 if candidate.entry_blocked else 0,
            rank,
            entry_distance if entry_distance is not None else 999.0,
            -float(candidate.price_action_strength or 0.0),
        )

    def _discover_failed_breakout(self) -> List[SetupCandidate]:
        self._refresh_failed_breakout_watch_states()
        watches = self._recent_failed_breakout_watches()
        if not watches:
            return []

        pairs: List[tuple[SetupCandidate, Dict[str, Any]]] = []
        for watch in watches:
            candidate = self._failed_breakout_candidate_from_watch(
                watch,
                persist_state=False,
            )
            if candidate is not None:
                pairs.append((candidate, watch))
        if not pairs:
            return []

        pairs.sort(key=lambda pair: self._failed_breakout_candidate_sort_key(pair[0]))

        # stock_setup_state remains one latest event per setup/side, matching
        # ACCEPTED_BREAKOUT. Persist only the preferred candidate for each side:
        # current dynamic range first when equally eligible, ORB/fixed levels as
        # fallback when the dynamic candidate is blocked.
        persisted_sides: set[str] = set()
        for candidate, watch in pairs:
            side_u = upper(candidate.side)
            if side_u in persisted_sides:
                continue
            persisted_sides.add(side_u)
            self._write_failed_breakout_candidate_state(
                watch=watch,
                candidate=candidate,
            )

        return [candidate for candidate, _ in pairs]

    # ------------------------------------------------------------------
    # New setup family 2: RANGE_REABSORPTION
    # Audit/WATCH-only for the current freeze.
    #
    # We intentionally defer CREATE for this setup for now. Range reabsorption is
    # very close to failed breakout unless the entry is still near the reabsorbed
    # edge with tight risk and enough room back into value. For the current design
    # goal, we prefer to log these cases and skip them rather than chase inside a
    # range after the lucrative entry point has passed.
    # ------------------------------------------------------------------
    def _discover_range_reabsorption(self) -> List[SetupCandidate]:
        d = self._snapshot
        cfg = self.cfg.range_reabsorption
        rule = self.cfg.setup_discovery.setup_rules[self.cfg.pattern.setup_range_reabsorption]
        failed_observations = [
            item
            for item in self._derived_breakout_observations(d)
            if upper(item.get("status")) == "FAILED_BREAKOUT"
        ]
        if not failed_observations:
            return []
        breakout = sorted(failed_observations, key=lambda x: int(x.get("rank") or 999))[0]
        status = "RANGE_REABSORBED"
        breakout_side = upper(breakout.get("side"))
        if breakout_side not in SIDES:
            return []

        candidate_side = opposite_side(breakout_side)
        bars_reclaimed = int(self._as_int(breakout.get("bars_reclaimed")) or 0)
        reference_level = {
            "reference_id": breakout.get("reference_id"),
            "level_type": breakout.get("level_type"),
            "price": breakout.get("price"),
            "side": breakout_side,
            "source": breakout.get("source"),
            "role": "RANGE_REABSORPTION_REFERENCE",
            "range_context": breakout.get("range_context", {}),
        }

        accepted = self._accepted_range_context(d)
        close = require_numeric(d, "bar.close")
        inside_range = self._is_inside_accepted_range(close=close, accepted=accepted)
        setup_inputs = {
            "setup_family": self.cfg.pattern.setup_range_reabsorption,
            "breakout_status": status,
            "breakout_side": breakout_side,
            "candidate_side": candidate_side,
            "breakout_reason": "exact_level_acceptance_reabsorbed",
            "attempt_time": breakout.get("attempt_time"),
            "accepted_time": breakout.get("accepted_time"),
            "failed_time": breakout.get("failed_time"),
            "bars_outside": int(self._as_int(breakout.get("bars_outside")) or 0),
            "bars_reclaimed": bars_reclaimed,
            "min_bars_reclaimed": cfg.min_bars_reclaimed,
            "close": close,
            "inside_accepted_range": inside_range,
            "accepted_range": accepted,
            "reabsorbed_level": reference_level,
            "level_type": reference_level.get("level_type"),
            "level_price": reference_level.get("price"),
            "level_source": reference_level.get("source"),
        }

        candidate = self._structural_reversal_candidate_for_side(
            setup_label=self.cfg.pattern.setup_range_reabsorption,
            side=candidate_side,
            original_breakout_side=breakout_side,
            rule_priority=rule.priority,
            rule_strategy=rule.strategy,
            atr_buffer=cfg.invalidation_atr_buffer,
            require_inside_accepted_range=cfg.require_inside_accepted_range,
            min_bars_reclaimed=cfg.min_bars_reclaimed,
            bars_reclaimed=bars_reclaimed,
            inside_range=inside_range,
            accepted_range=accepted,
            level_price=float(reference_level["price"]),
            base_data=setup_inputs,
            block_entry_if_opposite_exhaustion=cfg.block_entry_if_opposite_exhaustion,
            allow_create=rule.allow_create,
            not_confirmed_text=(
                f"{candidate_side} range reabsorption discovered after {breakout_side} breakout failure, "
                "but price action has not confirmed."
            ),
            blocked_text=f"{candidate_side} range reabsorption is deferred by sustain/location filter.",
            ready_text=f"{candidate_side} range reabsorption is sustained, price-action confirmed, and entry location is acceptable.",
        )
        return [candidate]

    def _structural_reversal_candidate_for_side(
        self,
        *,
        setup_label: str,
        side: str,
        original_breakout_side: str,
        rule_priority: int,
        rule_strategy: str,
        atr_buffer: float,
        require_inside_accepted_range: bool,
        min_bars_reclaimed: int,
        bars_reclaimed: int,
        inside_range: bool,
        accepted_range: Dict[str, Any],
        level_price: float,
        base_data: Dict[str, Any],
        frozen_accepted_range_width_points: Optional[float] = None,
        frozen_accepted_range_width_atr: Optional[float] = None,
        block_entry_if_opposite_exhaustion: bool,
        allow_create: bool,
        not_confirmed_text: str,
        blocked_text: str,
        ready_text: str,
    ) -> SetupCandidate:
        pa = self._price_action_confirmation(side)
        location = self._structural_entry_filter(
            side=side,
            setup_label=setup_label,
            require_inside_accepted_range=require_inside_accepted_range,
            inside_range=inside_range,
            min_bars_reclaimed=min_bars_reclaimed,
            bars_reclaimed=bars_reclaimed,
            accepted_range=accepted_range,
            frozen_accepted_range_width_points=frozen_accepted_range_width_points,
            frozen_accepted_range_width_atr=frozen_accepted_range_width_atr,
            block_entry_if_opposite_exhaustion=block_entry_if_opposite_exhaustion,
            level_price=level_price,
            atr_buffer=atr_buffer,
        )
        if not allow_create:
            # Audit/WATCH-only setup. Keep the candidate in discovery/audit payloads,
            # but never allow it to become ENTRY_READY until replay proves it adds
            # a distinct, attractive entry beyond failed breakout.
            watch_code = f"{setup_label}_WATCH_ONLY_NO_CREATE"
            location["blocked"] = True
            if not location.get("blocked_by"):
                location["blocked_by"] = watch_code
            location.setdefault("risk_flags", []).append(watch_code)
            location["allow_create"] = False
            location["watch_only_reason"] = (
                "Range reabsorption is currently logged as context only; CREATE is deferred "
                "unless/until replay proves a separate high-quality edge-entry pattern."
            )
        else:
            location["allow_create"] = True

        pa_quality_block = self._single_candle_reclaim_quality_blocker(
            setup_label=setup_label,
            side=side,
            pa=pa,
            min_move_atr=getattr(self.cfg.failed_breakout, "single_candle_reclaim_min_move_atr", 0.50),
        )
        if pa_quality_block is not None:
            location = self._with_location_block(location, pa_quality_block["code"], price_action_quality_filter=pa_quality_block)

        setup_levels = self._structural_setup_levels(
            side=side,
            setup_label=setup_label,
            original_breakout_side=original_breakout_side,
            level_price=level_price,
            atr_buffer=atr_buffer,
        )
        confirmed = bool(pa["confirmed"])
        blocked = bool(location["blocked"])
        risk_flags = list(location["risk_flags"])
        if not confirmed:
            state = "SETUP_DISCOVERED"
            code = self.cfg.reason.setup_not_confirmed_code
            text = not_confirmed_text
        elif blocked:
            state = "ENTRY_DEFERRED"
            code = self.cfg.reason.blocked_by_location_code
            text = blocked_text
        else:
            state = "ENTRY_READY"
            code = self.cfg.reason.create_code
            text = ready_text
        return SetupCandidate(
            setup_label=setup_label,
            strategy=rule_strategy,
            side=side,
            priority=rule_priority,
            discovered=True,
            price_action_confirmed=confirmed,
            price_action_strength=float(pa["strength"]),
            entry_blocked=blocked,
            blocked_by=location["blocked_by"],
            reason_code=code,
            reason_text=text,
            evidence_state=state,
            risk_flags=risk_flags,
            data={
                "setup_inputs": base_data,
                "price_action": pa,
                "entry_location_filter": location,
                "setup_levels": setup_levels,
            },
        )

    # ------------------------------------------------------------------
    # Common gates / setup-level helpers
    # ------------------------------------------------------------------
    def _price_action_confirmation(self, side: str) -> Dict[str, Any]:
        d = self._snapshot
        cfg = self.cfg.price_action
        open_price = require_numeric(d, "bar.open")
        high = require_numeric(d, "bar.high")
        low = require_numeric(d, "bar.low")
        close = require_numeric(d, "bar.close")
        current_move_atr = require_numeric(d, "market_windows.current.move_atr")
        current_pos = self._close_position(low=low, high=high, close=close)
        move_15m = require_numeric(d, "market_windows.15m.move_atr")
        pos_15m = require_numeric(d, "market_windows.15m.close_position_in_range")

        if side == BUY:
            single = (
                close > open_price
                and current_pos >= cfg.buy_single_candle_close_position_min
                and current_move_atr >= cfg.min_single_candle_move_atr
            )
            multi = (
                cfg.multi_candle_enabled
                and move_15m >= cfg.multi_candle_buy_15m_min_move_atr
                and pos_15m >= cfg.multi_candle_buy_15m_close_position_min
            )
            direction = "green/recovering"
        elif side == SELL:
            single = (
                close < open_price
                and current_pos <= cfg.sell_single_candle_close_position_max
                and current_move_atr <= -cfg.min_single_candle_move_atr
            )
            multi = (
                cfg.multi_candle_enabled
                and move_15m <= cfg.multi_candle_sell_15m_max_move_atr
                and pos_15m <= cfg.multi_candle_sell_15m_close_position_max
            )
            direction = "red/rejecting"
        else:
            raise ValueError(f"Unsupported side for price action confirmation: {side}")

        raw_confirmed = bool(single or multi)
        strength = self._price_action_strength(single=single, multi=multi, current_pos=current_pos, side=side)
        confirmed = bool(raw_confirmed and strength >= cfg.strength_confirm_min)
        return {
            "confirmed": confirmed,
            "raw_confirmed": raw_confirmed,
            "strength": strength,
            "strength_confirm_min": cfg.strength_confirm_min,
            "single_candle_confirmed": bool(single),
            "multi_candle_confirmed": bool(multi),
            "direction": direction,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "current_close_position": current_pos,
            "current_move_atr": current_move_atr,
            "move_15m_atr": move_15m,
            "position_15m": pos_15m,
        }

    def _directional_vwap_room_filter(self, side: str, vwap_gap_pct: Optional[float]) -> Dict[str, Any]:
        """Price-action location guard for fresh exhaustion reversal.

        VWAP is treated as the first value/reference magnet, not as a lagging
        trend filter.  BUY exhaustion must still have room upward to VWAP; SELL
        exhaustion must still have room downward to VWAP.  If price has already
        crossed/consumed VWAP, the later continuation/reclaim can be handled by
        another setup, not EXHAUSTION_REVERSAL.
        """
        cfg = self.cfg.exhaustion_reversal
        side_u = upper(side)
        min_room = float(getattr(cfg, "min_directional_vwap_room_pct", cfg.min_abs_vwap_gap_pct) or 0.0)

        if not bool(cfg.vwap_filter_enabled):
            return {"blocked": False, "reason": "VWAP_FILTER_DISABLED"}
        if vwap_gap_pct is None:
            return {
                "blocked": False,
                "reason": "VWAP_UNAVAILABLE_SKIP_DIRECTIONAL_ROOM",
                "vwap_available": False,
                "min_directional_vwap_room_pct": min_room,
            }

        try:
            gap = float(vwap_gap_pct)
        except Exception:
            return {
                "blocked": False,
                "reason": "VWAP_GAP_NOT_NUMERIC_SKIP_DIRECTIONAL_ROOM",
                "vwap_available": False,
                "vwap_gap_pct": vwap_gap_pct,
                "min_directional_vwap_room_pct": min_room,
            }

        if side_u == BUY:
            # Negative gap means price is below VWAP and has room to mean-revert up.
            required_relation = f"vwap_gap_pct <= -{min_room}"
            blocked = gap > -min_room
        elif side_u == SELL:
            # Positive gap means price is above VWAP and has room to mean-revert down.
            required_relation = f"vwap_gap_pct >= {min_room}"
            blocked = gap < min_room
        else:
            return {"blocked": False, "reason": "UNSUPPORTED_SIDE", "side": side_u}

        return {
            "blocked": bool(blocked),
            "code": "EXHAUSTION_REVERSAL_VWAP_TARGET_ALREADY_CONSUMED" if blocked else "EXHAUSTION_REVERSAL_DIRECTIONAL_VWAP_ROOM_OK",
            "side": side_u,
            "vwap_available": True,
            "vwap_gap_pct": gap,
            "min_directional_vwap_room_pct": min_room,
            "required_relation": required_relation,
            "rule": (
                "Fresh exhaustion reversal CREATE requires directional room back to VWAP. "
                "If VWAP is already consumed, classify later movement under a reclaim/continuation setup, not exhaustion."
            ),
        }

    def _entry_location_filter(
        self,
        side: str,
        *,
        vwap_blocked: bool,
        vwap_gap_pct: Optional[float],
        price_action: Dict[str, Any],
    ) -> Dict[str, Any]:
        d = self._snapshot
        cfg = self.cfg.exhaustion_reversal
        rsi = require_numeric(d, "indicators.rsi.value")
        bb_pos = require_numeric(d, "indicators.bollinger.position")
        close = require_numeric(d, "bar.close")
        risk_flags: List[str] = []
        blocked = False
        blocked_by: Optional[str] = None

        def block(code: str) -> None:
            nonlocal blocked, blocked_by
            blocked = True
            if blocked_by is None:
                blocked_by = code
            risk_flags.append(code)

        if side == BUY and rsi >= cfg.block_buy_if_rsi_min and bb_pos >= cfg.block_buy_bollinger_position_min:
            block("BUY_ENTRY_AT_UPPER_RSI_BB_EXHAUSTION")
        if side == SELL and rsi <= cfg.block_sell_if_rsi_max and bb_pos <= cfg.block_sell_bollinger_position_max:
            block("SELL_ENTRY_AT_LOWER_RSI_BB_EXHAUSTION")
        if vwap_blocked:
            block("VWAP_EXTENSION_TOO_SMALL")

        directional_vwap_room = self._directional_vwap_room_filter(side, vwap_gap_pct)
        if bool(directional_vwap_room.get("blocked")):
            block(str(directional_vwap_room.get("code") or "EXHAUSTION_REVERSAL_DIRECTIONAL_VWAP_ROOM_BLOCKED"))

        # Signal creation must stay setup/price-action driven.  Do not compute
        # or evaluate reward:R or trade-manager targets here.  This setup-level
        # guard only checks whether the immediate exhaustion-to-value reference
        # has already been consumed before CREATE.

        return {
            "blocked": blocked,
            "blocked_by": blocked_by,
            "risk_flags": risk_flags,
            "rsi": rsi,
            "bollinger_position": bb_pos,
            "vwap_gap_pct": vwap_gap_pct,
            "min_abs_vwap_gap_pct": cfg.min_abs_vwap_gap_pct,
            "vwap_filter_enabled": cfg.vwap_filter_enabled,
            "directional_vwap_room_filter": directional_vwap_room,
            "close": close,
            "target_filter_mode": "NO_REWARD_R_IN_SIGNAL_LAYER",
            "target_filter_note": "Signal creation ignores reward:R and trade-manager targets; exhaustion only checks whether the immediate VWAP/value reference was already consumed.",
            "price_action_quality_filter": None,
        }

    @staticmethod
    def _with_location_block(location: Dict[str, Any], code: str, **extra: Any) -> Dict[str, Any]:
        out = dict(location)
        risk_flags = list(out.get("risk_flags") or [])
        if code not in risk_flags:
            risk_flags.append(code)
        out["risk_flags"] = risk_flags
        out["blocked"] = True
        if not out.get("blocked_by"):
            out["blocked_by"] = code
        for key, value in extra.items():
            out[key] = value
        return out

    def _single_candle_reclaim_quality_blocker(
        self,
        *,
        setup_label: str,
        side: str,
        pa: Dict[str, Any],
        min_move_atr: float,
        buy_15m_position_min: Optional[float] = None,
        sell_15m_position_max: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Common single-candle quality gate for CREATE-capable setups.

        Generic price action can confirm on one candle if the current candle closes
        strongly inside its own range.  For CREATE, that is not enough when there
        is no multi-candle confirmation.  A one-candle exhaustion entry must be a
        true price-action reversal candle: large displacement, close at the right
        end of the candle, and enough short-window structure break/reclaim.

        This deliberately avoids HMA/ADX/other lagging trend indicators.
        """
        if not bool(pa.get("confirmed")):
            return None
        if not bool(pa.get("single_candle_confirmed")):
            return None
        if bool(pa.get("multi_candle_confirmed")):
            return None

        current_move_atr = self._as_float(pa.get("current_move_atr"))
        current_close_position = self._as_float(pa.get("current_close_position"))
        position_15m = self._as_float(pa.get("position_15m"))

        if side == BUY:
            large_displacement = current_move_atr is not None and current_move_atr >= float(min_move_atr)
            closes_at_reversal_end = current_close_position is None or current_close_position >= 0.80
            short_window_reclaimed = (
                buy_15m_position_min is None
                or position_15m is None
                or position_15m >= float(buy_15m_position_min)
            )
            strong_reclaim = large_displacement and closes_at_reversal_end and short_window_reclaimed
        elif side == SELL:
            large_displacement = current_move_atr is not None and current_move_atr <= -float(min_move_atr)
            closes_at_reversal_end = current_close_position is None or current_close_position <= 0.20
            short_window_reclaimed = (
                sell_15m_position_max is None
                or position_15m is None
                or position_15m <= float(sell_15m_position_max)
            )
            strong_reclaim = large_displacement and closes_at_reversal_end and short_window_reclaimed
        else:
            return None

        if strong_reclaim:
            return None

        missing: List[str] = []
        if not large_displacement:
            missing.append("LARGE_REVERSAL_DISPLACEMENT")
        if not closes_at_reversal_end:
            missing.append("CLOSE_AT_REVERSAL_END")
        if not short_window_reclaimed:
            missing.append("SHORT_WINDOW_STRUCTURE_BREAK")

        return {
            "code": f"{setup_label}_WEAK_SINGLE_CANDLE_REVERSAL",
            "setup_label": setup_label,
            "side": side,
            "single_candle_confirmed": bool(pa.get("single_candle_confirmed")),
            "multi_candle_confirmed": bool(pa.get("multi_candle_confirmed")),
            "current_move_atr": current_move_atr,
            "current_close_position": current_close_position,
            "position_15m": position_15m,
            "min_single_candle_reclaim_move_atr": float(min_move_atr),
            "buy_15m_position_min": buy_15m_position_min,
            "sell_15m_position_max": sell_15m_position_max,
            "missing_requirements": missing,
            "rule": (
                "Single-candle exhaustion is WATCH/DEFER unless it is a strong "
                "price-action reversal candle: large displacement, close near the "
                "reversal end, and a short-window structure break/reclaim."
            ),
        }

    def _structural_entry_filter(
        self,
        *,
        side: str,
        setup_label: str,
        require_inside_accepted_range: bool,
        inside_range: bool,
        min_bars_reclaimed: int,
        bars_reclaimed: int,
        accepted_range: Dict[str, Any],
        frozen_accepted_range_width_points: Optional[float],
        frozen_accepted_range_width_atr: Optional[float],
        block_entry_if_opposite_exhaustion: bool,
        level_price: float,
        atr_buffer: float,
    ) -> Dict[str, Any]:
        d = self._snapshot
        exhaustion_cfg = self.cfg.exhaustion_reversal
        if setup_label == self.cfg.pattern.setup_failed_breakout:
            setup_cfg = self.cfg.failed_breakout
        elif setup_label == self.cfg.pattern.setup_range_reabsorption:
            setup_cfg = self.cfg.range_reabsorption
        else:
            setup_cfg = self.cfg.failed_breakout

        rsi = require_numeric(d, "indicators.rsi.value")
        bb_pos = require_numeric(d, "indicators.bollinger.position")
        pos_15m = require_numeric(d, "market_windows.15m.close_position_in_range")
        high = require_numeric(d, "bar.high")
        low = require_numeric(d, "bar.low")
        close = require_numeric(d, "bar.close")
        atr = require_numeric(d, "indicators.atr.value")
        level = float(level_price)
        buffer_points = float(atr_buffer) * atr

        if side == BUY:
            invalidation_base = min(low, level)
            calculated_invalidation = invalidation_base - buffer_points
        elif side == SELL:
            invalidation_base = max(high, level)
            calculated_invalidation = invalidation_base + buffer_points
        else:
            raise ValueError(f"Unsupported side for structural entry filter: {side}")

        entry_distance_points = abs(close - level)
        entry_distance_atr = entry_distance_points / atr if atr else None

        risk_flags: List[str] = []
        blocked = False
        blocked_by: Optional[str] = None

        def block(code: str) -> None:
            nonlocal blocked, blocked_by
            blocked = True
            if blocked_by is None:
                blocked_by = code
            risk_flags.append(code)

        if require_inside_accepted_range and not inside_range:
            block(f"{setup_label}_NOT_INSIDE_ACCEPTED_RANGE")
        if bars_reclaimed < min_bars_reclaimed:
            block(f"{setup_label}_REABSORPTION_NOT_SUSTAINED")

        # Conservative failed-breakout / range-reabsorption location gate:
        # do not chase a reclaim after it has already moved away from the
        # broken/reabsorbed level or stretched into the opposite edge.
        if side == BUY:
            if bb_pos >= setup_cfg.block_buy_bollinger_position_min:
                block(f"{setup_label}_BUY_UPPER_BOLLINGER_STRETCH")
            if rsi >= setup_cfg.block_buy_rsi_min and bb_pos >= setup_cfg.block_buy_rsi_bollinger_position_min:
                block(f"{setup_label}_BUY_HIGH_RSI_UPPER_BOLLINGER")
            if pos_15m >= setup_cfg.block_buy_15m_position_min:
                block(f"{setup_label}_BUY_UPPER_15M_LOCATION")
        elif side == SELL:
            if bb_pos <= setup_cfg.block_sell_bollinger_position_max:
                block(f"{setup_label}_SELL_LOWER_BOLLINGER_STRETCH")
            if rsi <= setup_cfg.block_sell_rsi_max and bb_pos <= setup_cfg.block_sell_rsi_bollinger_position_max:
                block(f"{setup_label}_SELL_LOW_RSI_LOWER_BOLLINGER")
            if pos_15m <= setup_cfg.block_sell_15m_position_max:
                block(f"{setup_label}_SELL_LOWER_15M_LOCATION")

        if entry_distance_atr is not None and entry_distance_atr > setup_cfg.max_entry_distance_from_level_atr:
            block(f"{setup_label}_ENTRY_DISTANCE_FROM_LEVEL_GT_{setup_cfg.max_entry_distance_from_level_atr:.2f}_ATR")

        accepted = dict(accepted_range or {})
        accepted_high = self._as_float(accepted.get("high"))
        accepted_low = self._as_float(accepted.get("low"))
        range_mid = None
        accepted_range_width_points = None
        accepted_range_width_atr = None
        accepted_range_width_source = "CURRENT_EVALUATION"

        if accepted_high is None or accepted_low is None or accepted_high <= accepted_low:
            block(f"{setup_label}_ACCEPTED_RANGE_NOT_USABLE")
        else:
            range_mid = (accepted_high + accepted_low) / 2.0
            if (
                frozen_accepted_range_width_points is not None
                and frozen_accepted_range_width_atr is not None
            ):
                accepted_range_width_points = float(frozen_accepted_range_width_points)
                accepted_range_width_atr = float(frozen_accepted_range_width_atr)
                accepted_range_width_source = "EVENT_FROZEN"
            else:
                accepted_range_width_points = accepted_high - accepted_low
                accepted_range_width_atr = accepted_range_width_points / atr if atr else None
            if (
                accepted_range_width_atr is not None
                and accepted_range_width_atr < float(setup_cfg.min_accepted_range_width_atr)
            ):
                block(
                    f"{setup_label}_ACCEPTED_RANGE_WIDTH_LT_"
                    f"{float(setup_cfg.min_accepted_range_width_atr):.2f}_ATR"
                )

        # Keep the older anti-opposite-exhaustion safety net. These thresholds
        # are broader and remain useful as a common blanket guard.
        if block_entry_if_opposite_exhaustion:
            if side == BUY and rsi >= exhaustion_cfg.block_buy_if_rsi_min and bb_pos >= exhaustion_cfg.block_buy_bollinger_position_min:
                block("BUY_ENTRY_AT_UPPER_RSI_BB_EXHAUSTION")
            if side == SELL and rsi <= exhaustion_cfg.block_sell_if_rsi_max and bb_pos <= exhaustion_cfg.block_sell_bollinger_position_max:
                block("SELL_ENTRY_AT_LOWER_RSI_BB_EXHAUSTION")

        return {
            "blocked": blocked,
            "blocked_by": blocked_by,
            "risk_flags": risk_flags,
            "rsi": rsi,
            "bollinger_position": bb_pos,
            "position_15m": pos_15m,
            "inside_accepted_range": inside_range,
            "require_inside_accepted_range": require_inside_accepted_range,
            "bars_reclaimed": bars_reclaimed,
            "min_bars_reclaimed": min_bars_reclaimed,
            "close": close,
            "level_price": level,
            "atr": atr,
            "atr_buffer": atr_buffer,
            "buffer_points": buffer_points,
            "invalidation_base": invalidation_base,
            "entry_distance_from_level_points": entry_distance_points,
            "entry_distance_from_level_atr": entry_distance_atr,
            "max_entry_distance_from_level_atr": setup_cfg.max_entry_distance_from_level_atr,
            "accepted_range_high": accepted_high,
            "accepted_range_low": accepted_low,
            "accepted_range_id": accepted.get("range_id"),
            "accepted_range_version": self._as_int(accepted.get("version")),
            "accepted_range_source": accepted.get("source"),
            "accepted_range_mid": range_mid,
            "accepted_range_width_points": accepted_range_width_points,
            "accepted_range_width_atr": accepted_range_width_atr,
            "accepted_range_width_source": accepted_range_width_source,
            "min_accepted_range_width_atr": setup_cfg.min_accepted_range_width_atr,
            "block_buy_bollinger_position_min": setup_cfg.block_buy_bollinger_position_min,
            "block_sell_bollinger_position_max": setup_cfg.block_sell_bollinger_position_max,
            "block_buy_15m_position_min": setup_cfg.block_buy_15m_position_min,
            "block_sell_15m_position_max": setup_cfg.block_sell_15m_position_max,
        }

    def _exhaustion_setup_levels(self, side: str) -> Dict[str, Any]:
        d = self._snapshot
        high = require_numeric(d, "bar.high")
        low = require_numeric(d, "bar.low")
        close = require_numeric(d, "bar.close")
        snapshot_time = self._optional_path(d, "snapshot_time")
        side_u = upper(side)
        if side_u == SELL:
            reference_price = high
            invalidation_side = "ABOVE"
            reference_source = "reversal_candle_high"
        elif side_u == BUY:
            reference_price = low
            invalidation_side = "BELOW"
            reference_source = "reversal_candle_low"
        else:
            raise ValueError(f"Unsupported side for setup levels: {side}")
        return {
            "setup_label": self.cfg.pattern.setup_exhaustion_reversal,
            "side": side_u,
            "reference_type": "REVERSAL_EXTREME",
            "reference_price": reference_price,
            "reference_source": reference_source,
            "signal_invalidation_reference_price": reference_price,
            "signal_invalidation_reference_source": reference_source,
            "signal_invalidation_reference_policy": "CONFIGURED_BUFFERED_EXTREME_INVALIDATION",
            "initial_stop_reference_price": reference_price,
            "initial_stop_reference_source": reference_source,
            "initial_stop_side": invalidation_side,
            "invalidation_side": invalidation_side,
            "confirmation_price": close,
            "confirmation_time": str(snapshot_time) if snapshot_time is not None else None,
            "note": "Signal layer passes setup reference levels only; trade manager owns SL buffer, targets, trailing and risk/reward.",
        }

    def _structural_setup_levels(
        self,
        *,
        side: str,
        setup_label: str,
        original_breakout_side: str,
        level_price: float,
        atr_buffer: float,
    ) -> Dict[str, Any]:
        d = self._snapshot
        close = require_numeric(d, "bar.close")
        level = float(level_price)
        side_u = upper(side)
        if side_u == SELL:
            invalidation_side = "ABOVE"
        elif side_u == BUY:
            invalidation_side = "BELOW"
        else:
            raise ValueError(f"Unsupported side for setup levels: {side}")
        return {
            "setup_label": setup_label,
            "side": side_u,
            "reference_type": "STRUCTURAL_LEVEL",
            "reference_price": level,
            "reference_source": "breakout_or_range_level",
            "level_price": level,
            "original_breakout_side": original_breakout_side,
            "signal_invalidation_reference_price": level,
            "signal_invalidation_reference_source": "breakout_or_range_level",
            "signal_invalidation_reference_policy": "CONFIGURED_BUFFERED_REACCEPTANCE",
            "initial_stop_reference_price": level,
            "initial_stop_reference_source": "breakout_or_range_level",
            "initial_stop_side": invalidation_side,
            "invalidation_side": invalidation_side,
            "confirmation_price": close,
            "note": "Signal layer passes structure level only; trade manager owns SL buffer, targets, trailing and risk/reward.",
        }

    # ------------------------------------------------------------------
    # New setup family 3: ACCEPTED_BREAKOUT
    # ------------------------------------------------------------------
    def _discover_accepted_breakout(self) -> List[SetupCandidate]:
        """Derive ACCEPTED_BREAKOUT from neutral exact-level observations.

        Snapshot provides only fixed/qualified levels and recent closes. Evidence
        applies configured ATR buffers independently to each unique level, so
        acceptance against one reference can never be attached to another.
        """
        d = self._snapshot
        cfg = self.cfg.accepted_breakout
        rule = self.cfg.setup_discovery.setup_rules[self.cfg.pattern.setup_accepted_breakout]
        self._refresh_accepted_breakout_pending_states()
        observations = [
            item
            for item in self._derived_breakout_observations(d)
            if upper(item.get("status")) in {
                "ACCEPTED_BREAKOUT",
                "BREAKOUT_TESTING",
                "BREAKOUT_ATTEMPT",
            }
        ]
        if not observations:
            return []

        accepted = self._accepted_range_context(d)
        close = require_numeric(d, "bar.close")

        normalized_observations: List[Dict[str, Any]] = []
        for item in observations:
            normalized = dict(item)
            priority_context = self._accepted_breakout_level_priority_context(d, normalized)
            normalized["level_priority_context"] = priority_context
            if bool(priority_context.get("major_level_reacceptance")):
                normalized["original_acceptance_path"] = normalized.get("acceptance_path")
                normalized["acceptance_path"] = "MAJOR_LEVEL_REACCEPTANCE"
            normalized_observations.append(normalized)
        observations = normalized_observations

        compact_levels = [
            {
                "reference_id": item.get("reference_id"),
                "level_type": item.get("level_type"),
                "price": item.get("price"),
                "side": item.get("side"),
                "source": item.get("source"),
                "aliases": item.get("aliases", []),
                "status": item.get("status"),
                "bars_outside": item.get("bars_outside"),
                "current_offset_atr": item.get("current_offset_atr"),
                "acceptance_path": item.get("acceptance_path"),
            }
            for item in observations
        ]

        candidates: List[SetupCandidate] = []
        for observation in observations:
            side = upper(observation.get("side"))
            if side not in SIDES:
                continue
            status = upper(observation.get("status"))
            path_type = str(observation.get("acceptance_path") or "EARLY_MAJOR_LEVEL_BREAK")
            reference_level = {
                "reference_id": observation.get("reference_id"),
                "level_type": observation.get("level_type"),
                "price": observation.get("price"),
                "side": side,
                "source": observation.get("source"),
                "sources": observation.get("sources", []),
                "role": "ACCEPTED_BREAKOUT_REFERENCE",
                "rank": observation.get("rank"),
                "tags": observation.get("aliases", []),
                "range_context": observation.get("range_context", {}),
            }
            event_time = observation.get("attempt_time") or observation.get("accepted_time")
            event_key = "|".join([
                str(reference_level.get("reference_id") or ""),
                side,
                str(event_time or ""),
            ])
            observation = dict(observation)
            observation["event_key"] = event_key
            observation["event_time"] = event_time

            setup_inputs = {
                "setup_family": self.cfg.pattern.setup_accepted_breakout,
                "breakout_status": status,
                "breakout_side": side,
                "breakout_reason": "evidence_derived_exact_level_acceptance",
                "attempt_time": observation.get("attempt_time"),
                "accepted_time": observation.get("accepted_time"),
                "event_key": event_key,
                "event_source": "EVIDENCE_NEUTRAL_LEVEL_OBSERVATION",
                "event_time": event_time,
                "acceptance_path": path_type,
                "bars_outside": int(self._as_int(observation.get("bars_outside")) or 0),
                "min_bars_outside": cfg.min_bars_outside,
                "early_min_bars_outside": cfg.early_min_bars_outside,
                "early_min_break_distance_atr": cfg.early_min_break_distance_atr,
                "close": close,
                "accepted_range": accepted,
                "breakout_level": reference_level,
                "level_observation": observation,
                "level_priority_context": observation.get("level_priority_context", {}),
                "original_acceptance_path": observation.get("original_acceptance_path"),
                "level_type": reference_level.get("level_type"),
                "level_price": reference_level.get("price"),
                "level_source": reference_level.get("source"),
                "level_rank": reference_level.get("rank"),
                "level_tags": reference_level.get("tags", []),
                "hma_context": self._hma_context_for_breakout(d, side),
                "participation_context": self._participation_context(d),
                "next_external_level": self._next_external_level_context(d, side, close),
                "level_candidate_count": len(observations),
                "all_level_candidates": compact_levels,
            }

            candidate = self._accepted_breakout_candidate_for_side(
                side=side,
                rule_priority=rule.priority,
                rule_strategy=rule.strategy,
                atr_buffer=cfg.invalidation_atr_buffer,
                bars_outside=int(self._as_int(observation.get("bars_outside")) or 0),
                level_price=float(reference_level["price"]),
                base_data=setup_inputs,
                allow_create=rule.allow_create,
                path_type=path_type,
                level_observation=observation,
            )
            candidates.append(candidate)

        ordered = sorted(candidates, key=self._accepted_breakout_candidate_sort_key)
        # stock_setup_state is one current event per setup/side. Persist only the
        # highest-ranked accepted-breakout candidate for each side so lower-ranked
        # ORB/PDH/PDL observations cannot overwrite the dynamic event that the
        # selector will actually use.
        persisted_sides: set[str] = set()
        for candidate in ordered:
            side_u = upper(candidate.side)
            if side_u in persisted_sides:
                continue
            persisted_sides.add(side_u)
            inputs = candidate.data.get("setup_inputs", {}) if isinstance(candidate.data, dict) else {}
            observation = inputs.get("level_observation") if isinstance(inputs, dict) else None
            if isinstance(observation, dict):
                self._write_accepted_breakout_candidate_state(
                    observation=observation,
                    candidate=candidate,
                )

        return ordered

    def _accepted_breakout_candidate_for_side(
        self,
        *,
        side: str,
        rule_priority: int,
        rule_strategy: str,
        atr_buffer: float,
        bars_outside: int,
        level_price: float,
        base_data: Dict[str, Any],
        allow_create: bool,
        path_type: str = "MAJOR_LEVEL_ACCEPTANCE",
        level_observation: Optional[Dict[str, Any]] = None,
    ) -> SetupCandidate:
        pa = self._price_action_confirmation(side)
        location = self._accepted_breakout_entry_filter(
            side=side,
            bars_outside=bars_outside,
            level_price=level_price,
            atr_buffer=atr_buffer,
            allow_create=allow_create,
            path_type=path_type,
            level_observation=level_observation or {},
        )

        # Accepted breakout needs more than a marginal one-candle bounce/reversal
        # when the recent short-term sequence is still against the breakout side.
        # The generic price-action helper may mark the latest candle as confirmed
        # because it closes well within its own range, but for breakout entries we
        # also require a meaningful reclaim/continuation when multi-candle
        # confirmation is absent.
        pa_quality_block = self._accepted_breakout_price_action_quality_blocker(side=side, pa=pa)
        if pa_quality_block is not None:
            location = dict(location)
            risk_flags = list(location.get("risk_flags") or [])
            risk_flags.append(pa_quality_block["code"])
            location["risk_flags"] = risk_flags
            location["blocked"] = True
            if not location.get("blocked_by"):
                location["blocked_by"] = pa_quality_block["code"]
            location["price_action_quality_filter"] = pa_quality_block

        terminal_event = location.get("terminal_displacement") if isinstance(location.get("terminal_displacement"), dict) else {}
        resolved_event_key = terminal_event.get("event_key") or base_data.get("event_key")
        resolved_event_time = terminal_event.get("event_time") or base_data.get("event_time")
        resolved_event_atr = self._as_float(terminal_event.get("event_atr"))
        setup_levels = self._accepted_breakout_setup_levels(
            side=side,
            setup_label=self.cfg.pattern.setup_accepted_breakout,
            level_price=level_price,
            atr_buffer=atr_buffer,
            event_atr=resolved_event_atr,
            event_key=resolved_event_key,
            event_time=resolved_event_time,
            event_source=base_data.get("event_source"),
            level_type=base_data.get("level_type"),
            level_source=base_data.get("level_source"),
            reference_id=(base_data.get("breakout_level") or {}).get("reference_id")
            if isinstance(base_data.get("breakout_level"), dict)
            else None,
            acceptance_path=str(location.get("effective_acceptance_path") or path_type),
        )
        effective_path_type = str(location.get("effective_acceptance_path") or path_type)
        base_data = dict(base_data)
        base_data["event_key"] = resolved_event_key
        base_data["event_time"] = resolved_event_time
        base_data["effective_acceptance_path"] = effective_path_type
        base_data["acceptance_policy"] = location.get("acceptance_policy")
        base_data["strict_displacement"] = location.get("strict_displacement")
        base_data["dynamic_range_age"] = location.get("dynamic_range_age")
        base_data["structural_room"] = location.get("structural_room")
        confirmed = bool(pa["confirmed"])
        blocked = bool(location["blocked"])
        risk_flags = list(location["risk_flags"])
        setup_label = self.cfg.pattern.setup_accepted_breakout
        if not confirmed:
            state = "SETUP_DISCOVERED"
            code = self.cfg.reason.setup_not_confirmed_code
            text = f"{side} accepted breakout ({effective_path_type}) discovered, but price action has not confirmed."
        elif blocked:
            state = "ENTRY_DEFERRED"
            code = self.cfg.reason.blocked_by_location_code
            text = f"{side} accepted breakout ({effective_path_type}) is deferred by acceptance/location/setup/location filter."
        else:
            state = "ENTRY_READY"
            code = self.cfg.reason.create_code
            text = f"{side} accepted breakout ({effective_path_type}) has confirmed with acceptable entry setup/location."

        return SetupCandidate(
            setup_label=setup_label,
            strategy=rule_strategy,
            side=side,
            priority=rule_priority,
            discovered=True,
            price_action_confirmed=confirmed,
            price_action_strength=float(pa["strength"]),
            entry_blocked=blocked,
            blocked_by=location["blocked_by"],
            reason_code=code,
            reason_text=text,
            evidence_state=state,
            risk_flags=risk_flags,
            data={
                "setup_inputs": base_data,
                "price_action": pa,
                "entry_location_filter": location,
                "setup_levels": setup_levels,
            },
        )

    def _accepted_breakout_price_action_quality_blocker(self, *, side: str, pa: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extra price-action quality gate for ACCEPTED_BREAKOUT.

        The shared price-action helper can confirm a single candle when the current
        candle is green/red and closes strongly inside its own range. For accepted
        breakout this is too permissive after a pullback: a small green candle after
        recent red pressure should not confirm a BUY breakout unless it meaningfully
        reclaims the short-term range. Mirror the rule for SELL.

        This helper is intentionally local to accepted breakout so the frozen
        exhaustion/failed-breakout behavior is not changed.
        """
        if not bool(pa.get("confirmed")):
            return None
        if not bool(pa.get("single_candle_confirmed")):
            return None
        if bool(pa.get("multi_candle_confirmed")):
            return None

        move_15m = self._as_float(pa.get("move_15m_atr"))
        pos_15m = self._as_float(pa.get("position_15m"))
        current_move_atr = self._as_float(pa.get("current_move_atr"))
        current_close_position = self._as_float(pa.get("current_close_position"))
        slope_3_atr = self._optional_numeric(self._snapshot, "price_action.slope.bars_3_atr")
        slope_5_atr = self._optional_numeric(self._snapshot, "price_action.slope.bars_5_atr")
        slope_3_atr_per_bar = self._optional_numeric(self._snapshot, "price_action.slope.bars_3_atr_per_bar")

        # Reclaim thresholds. A single-candle BUY must close back in the upper
        # part of the recent 15m range; a single-candle SELL must close back in
        # the lower part. This approximates previous-candle/sequence reclaim using
        # fields available in the snapshot contract.
        buy_reclaim_position_min = 0.70
        sell_reclaim_position_max = 0.30
        min_single_candle_reclaim_move_atr = 0.50

        if side == BUY:
            recent_sequence_against_side = any(
                v is not None and v < 0
                for v in (move_15m, slope_3_atr, slope_5_atr, slope_3_atr_per_bar)
            )
            strong_reclaim = (
                (pos_15m is not None and pos_15m >= buy_reclaim_position_min)
                and (current_close_position is None or current_close_position >= 0.80)
                and (current_move_atr is None or current_move_atr >= min_single_candle_reclaim_move_atr)
            )
        elif side == SELL:
            recent_sequence_against_side = any(
                v is not None and v > 0
                for v in (move_15m, slope_3_atr, slope_5_atr, slope_3_atr_per_bar)
            )
            strong_reclaim = (
                (pos_15m is not None and pos_15m <= sell_reclaim_position_max)
                and (current_close_position is None or current_close_position <= 0.20)
                and (current_move_atr is None or current_move_atr <= -min_single_candle_reclaim_move_atr)
            )
        else:
            return None

        if recent_sequence_against_side and not strong_reclaim:
            return {
                "code": "ACCEPTED_BREAKOUT_SINGLE_CANDLE_PA_WITHOUT_RECLAIM",
                "side": side,
                "single_candle_confirmed": bool(pa.get("single_candle_confirmed")),
                "multi_candle_confirmed": bool(pa.get("multi_candle_confirmed")),
                "move_15m_atr": move_15m,
                "position_15m": pos_15m,
                "current_move_atr": current_move_atr,
                "current_close_position": current_close_position,
                "slope_3_atr": slope_3_atr,
                "slope_5_atr": slope_5_atr,
                "slope_3_atr_per_bar": slope_3_atr_per_bar,
                "recent_sequence_against_side": recent_sequence_against_side,
                "strong_reclaim": strong_reclaim,
                "buy_reclaim_position_min": buy_reclaim_position_min,
                "sell_reclaim_position_max": sell_reclaim_position_max,
                "min_single_candle_reclaim_move_atr": min_single_candle_reclaim_move_atr,
                "rule": (
                    "Accepted breakout cannot be confirmed by a marginal single candle "
                    "after recent opposite-side pressure unless it reclaims the short-term range."
                ),
            }

        return None

    def _accepted_breakout_terminal_extension_context(
        self,
        d: Dict[str, Any],
        side: str,
    ) -> Dict[str, Any]:
        """Return a narrow late-continuation guard for fresh breakout entries.

        This is deliberately setup/entry logic, not snapshot structure. For an
        ordinary breakout, multiple extension components are enough to show that
        the first continuation move is probably consumed. A strict displacement
        candidate may bypass the final block in the caller, but the component
        diagnostics remain visible.
        """
        cfg = self.cfg.accepted_breakout
        side_u = upper(side)
        move_15m_atr = self._optional_numeric(d, "market_windows.15m.move_atr")
        vwap_distance_atr = self._optional_numeric(d, "indicators.vwap.distance_atr")
        sod_position = self._optional_numeric(d, "market_windows.sod.close_position_in_range")

        enabled = bool(getattr(cfg, "terminal_extension_guard_enabled", True))
        move_threshold = float(getattr(cfg, "terminal_extension_min_move_15m_atr", 2.0))
        vwap_threshold = float(getattr(cfg, "terminal_extension_min_vwap_distance_atr", 1.5))
        buy_edge = float(getattr(cfg, "terminal_extension_buy_min_sod_position", 0.85))
        sell_edge = float(getattr(cfg, "terminal_extension_sell_max_sod_position", 0.15))
        min_components = max(
            1,
            int(getattr(cfg, "terminal_extension_min_components_to_block", 2)),
        )

        move_extreme = False
        vwap_extreme = False
        day_edge = False
        if side_u == BUY:
            move_extreme = move_15m_atr is not None and move_15m_atr >= move_threshold
            vwap_extreme = vwap_distance_atr is not None and vwap_distance_atr >= vwap_threshold
            day_edge = sod_position is not None and sod_position >= buy_edge
        elif side_u == SELL:
            move_extreme = move_15m_atr is not None and move_15m_atr <= -move_threshold
            vwap_extreme = vwap_distance_atr is not None and vwap_distance_atr <= -vwap_threshold
            day_edge = sod_position is not None and sod_position <= sell_edge

        component_count = int(move_extreme) + int(vwap_extreme) + int(day_edge)
        blocked = bool(enabled and component_count >= min_components)
        return {
            "enabled": enabled,
            "blocked": blocked,
            "side": side_u,
            "move_15m_atr": move_15m_atr,
            "vwap_distance_atr": vwap_distance_atr,
            "sod_position": sod_position,
            "move_threshold_atr": move_threshold,
            "vwap_threshold_atr": vwap_threshold,
            "buy_edge_min": buy_edge,
            "sell_edge_max": sell_edge,
            "move_extreme": move_extreme,
            "vwap_extreme": vwap_extreme,
            "day_edge": day_edge,
            "component_count": component_count,
            "min_components_to_block": min_components,
            "reason": (
                f"ACCEPTED_BREAKOUT_{side_u}_TERMINAL_EXTENSION_FIRST_MOVE_CONSUMED"
                if blocked
                else None
            ),
        }

    def _accepted_breakout_pending_row(self, *, side: str) -> Optional[Any]:
        return self._safe_setup_state_fetch(
            side=side,
            setup_label=self.cfg.pattern.setup_accepted_breakout,
        )

    def _accepted_breakout_recent_event_closes(
        self,
        *,
        event_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in self._recent_close_series(self._snapshot):
            item_time = self._to_ist_naive_dt(item.get("time"))
            close = self._as_float(item.get("close"))
            if item_time is None or close is None:
                continue
            if event_time is not None and item_time < event_time:
                continue
            rows.append({"time": item_time, "close": close})
        return rows

    def _accepted_breakout_pending_evaluation(
        self,
        *,
        side: str,
        level_price: float,
        event_atr: float,
        event_time: Optional[datetime],
        bars_outside: int,
        required_bars_outside: int,
    ) -> Dict[str, Any]:
        """Evaluate one frozen terminal-displacement event.

        The event ATR and structural level are immutable.  Pending invalidation
        therefore cannot widen when volatility expands after a failed impulse.
        Confirmation uses either sustained outside closes or a shallow retest
        followed by reclaim; both are normalized to the same event ATR.
        """
        cfg = self.cfg.accepted_breakout
        policy = self.cfg.signal_invalidation.accepted_breakout
        side_u = upper(side)
        closes = self._accepted_breakout_recent_event_closes(event_time=event_time)
        offsets: List[float] = []
        for item in closes:
            close = float(item["close"])
            offset = (close - level_price) / event_atr if side_u == BUY else (level_price - close) / event_atr
            offsets.append(float(offset))

        buffer_atr = max(0.0, float(getattr(policy, "buffer_atr", 0.20) or 0.20))
        strong_atr = max(
            buffer_atr,
            float(getattr(policy, "strong_single_close_atr", 0.50) or 0.50),
        )
        required_inside = max(1, int(getattr(policy, "required_consecutive_closes", 2) or 2))
        current_offset = offsets[-1] if offsets else None
        consecutive_inside = 0
        for value in reversed(offsets):
            if value <= -buffer_atr:
                consecutive_inside += 1
            else:
                break
        strong_inside = current_offset is not None and current_offset <= -strong_atr
        invalidated = bool(strong_inside or consecutive_inside >= required_inside)

        min_post = max(1, int(getattr(cfg, "terminal_displacement_min_post_impulse_closes", 2) or 2))
        sustained_required = max(int(required_bars_outside), 1 + min_post)
        acceptance_atr = max(0.0, float(getattr(cfg, "acceptance_buffer_atr", 0.25) or 0.25))
        sustained = bool(
            not invalidated
            and int(bars_outside or 0) >= sustained_required
            and current_offset is not None
            and current_offset >= acceptance_atr
        )

        # Retest/reclaim reuses existing acceptance + invalidation buffers.  A
        # retest is shallow acceptance around the old boundary, not a strong
        # close back inside the old range.
        post_impulse_offsets = offsets[1:] if len(offsets) > 1 else []
        prior_post_offsets = post_impulse_offsets[:-1] if post_impulse_offsets else []
        retest_seen = any(-buffer_atr < value < acceptance_atr for value in prior_post_offsets)
        reclaimed = bool(current_offset is not None and current_offset >= acceptance_atr)
        retest_reclaim = bool(not invalidated and retest_seen and reclaimed)

        return {
            "event_atr": event_atr,
            "event_time": event_time.isoformat() if isinstance(event_time, datetime) else None,
            "level_price": level_price,
            "recent_closes": [
                {"time": item["time"].isoformat(), "close": item["close"]}
                for item in closes
            ],
            "offsets_atr": offsets,
            "current_offset_atr": current_offset,
            "invalidation_buffer_atr": buffer_atr,
            "strong_invalidation_atr": strong_atr,
            "required_inside_closes": required_inside,
            "consecutive_inside_closes": consecutive_inside,
            "strong_inside_close": strong_inside,
            "invalidated": invalidated,
            "required_sustained_bars_outside": sustained_required,
            "bars_outside": int(bars_outside or 0),
            "sustained_acceptance": sustained,
            "retest_seen": retest_seen,
            "reclaimed": reclaimed,
            "retest_reclaim": retest_reclaim,
            "confirmed": bool(sustained or retest_reclaim),
        }

    def _accepted_breakout_pending_context(
        self,
        *,
        side: str,
        observation: Dict[str, Any],
        level_price: float,
        event_atr: float,
        bars_outside: int,
        required_bars_outside: int,
    ) -> Dict[str, Any]:
        cfg = self.cfg.accepted_breakout
        result: Dict[str, Any] = {
            "enabled": bool(getattr(cfg, "terminal_displacement_pending_enabled", True)),
            "active": False,
            "same_event": False,
            "confirmed": False,
            "invalidated": False,
            "expired": False,
            "confirmation_path": None,
        }
        if not result["enabled"] or not self._setup_state_read_watch_enabled():
            return result

        row = self._accepted_breakout_pending_row(side=side)
        if row is None:
            return result

        state_json = getattr(row, "state_json", None) or {}
        watch = state_json.get("watch") if isinstance(state_json, dict) else None
        if not isinstance(watch, dict):
            return result
        incoming_key = str(observation.get("event_key") or "")
        stored_key = str(watch.get("event_key") or state_json.get("event_key") or "")
        row_state = upper(getattr(row, "state", ""))
        pending_state = upper(getattr(self.cfg.setup_state, "confirmed_pending_state", "CONFIRMED_PENDING"))
        confirmed_state = upper(getattr(self.cfg.setup_state, "confirmed_state", "CONFIRMED"))
        invalidated_state = upper(getattr(self.cfg.setup_state, "invalidated_state", "INVALIDATED"))
        expired_state = upper(getattr(self.cfg.setup_state, "expired_state", "EXPIRED"))

        frozen_level = self._as_float(watch.get("level_price"))
        frozen_atr = self._as_float(watch.get("event_atr"))
        frozen_time = self._to_ist_naive_dt(watch.get("event_time"))
        expires_at = self._to_ist_naive_dt(getattr(row, "expires_at", None))
        now = self._snapshot_dt()
        incoming_time = self._to_ist_naive_dt(observation.get("event_time") or observation.get("attempt_time"))
        exact_same_event = bool(incoming_key and stored_key and incoming_key == stored_key)
        stored_reference_id = str(watch.get("reference_id") or "")
        incoming_reference_id = str(observation.get("reference_id") or "")
        within_live_window = bool(
            frozen_time is not None
            and incoming_time is not None
            and incoming_time >= frozen_time
            and (expires_at is None or incoming_time <= expires_at)
        )
        same_reference_live_event = bool(
            row_state in {pending_state, confirmed_state}
            and stored_reference_id
            and incoming_reference_id
            and stored_reference_id == incoming_reference_id
            and within_live_window
        )
        same_event = bool(exact_same_event or same_reference_live_event)
        result.update({
            "active": bool(same_event and row_state == pending_state),
            "same_event": same_event,
            "exact_same_event": exact_same_event,
            "same_reference_live_event": same_reference_live_event,
            "stored_event_key": stored_key or None,
            "incoming_event_key": incoming_key or None,
            "stored_event_time": frozen_time.isoformat() if isinstance(frozen_time, datetime) else None,
            "stored_event_atr": frozen_atr,
            "stored_level_price": frozen_level,
            "stored_reference_id": stored_reference_id or None,
            "incoming_reference_id": incoming_reference_id or None,
            "stored_state": row_state or None,
            "confirmed": bool(same_event and row_state == confirmed_state),
            "invalidated": bool(same_event and row_state == invalidated_state),
            "expired": bool(same_event and row_state == expired_state),
        })
        if same_event and row_state == confirmed_state:
            current_eval = state_json.get("current_evaluation") if isinstance(state_json, dict) else None
            if isinstance(current_eval, dict):
                result["confirmation_path"] = current_eval.get("confirmation_path")
            return result
        if not same_event or row_state != pending_state:
            return result

        if frozen_level is None or frozen_atr is None or frozen_atr <= 0:
            return result

        expired = bool(expires_at is not None and now is not None and now > expires_at)
        evaluation = self._accepted_breakout_pending_evaluation(
            side=side,
            level_price=frozen_level,
            event_atr=frozen_atr,
            event_time=frozen_time,
            bars_outside=bars_outside,
            required_bars_outside=required_bars_outside,
        )
        result.update(evaluation)
        result["expired"] = expired
        result["confirmed"] = bool(evaluation.get("confirmed") and not expired)
        if evaluation.get("retest_reclaim"):
            result["confirmation_path"] = "TERMINAL_DISPLACEMENT_RETEST_RECLAIM"
        elif evaluation.get("sustained_acceptance"):
            result["confirmation_path"] = "TERMINAL_DISPLACEMENT_SUSTAINED_ACCEPTANCE"
        return result

    def _transition_accepted_breakout_pending(
        self,
        *,
        row: Any,
        state: str,
        reason: str,
        details: Dict[str, Any],
    ) -> None:
        if not self._setup_state_write_enabled():
            return
        ts = self._snapshot_dt()
        if ts is None:
            raise ValueError("ACCEPTED_BREAKOUT transition requires snapshot.snapshot_time")
        state_json = getattr(row, "state_json", None) or {}
        watch = state_json.get("watch") if isinstance(state_json, dict) else {}
        try:
            StockSetupStateSchema.transition_state(
                trading_day=ts.date(),
                equity_ref=self._snapshot_equity_ref(),
                symbol=self._snapshot_symbol() or self._snapshot_equity_ref(),
                setup=self.cfg.pattern.setup_accepted_breakout,
                side=upper(getattr(row, "side", "")),
                state=state,
                state_reason=reason,
                ts=ts,
                signal_id=getattr(row, "signal_id", None),
                expires_at=getattr(row, "expires_at", None),
                event_key=watch.get("event_key") if isinstance(watch, dict) else None,
                event_source=watch.get("event_source") if isinstance(watch, dict) else None,
                event_time=watch.get("event_time") if isinstance(watch, dict) else None,
                reference_price=watch.get("level_price") if isinstance(watch, dict) else getattr(row, "reference_price", None),
                reference_source=watch.get("level_source") if isinstance(watch, dict) else getattr(row, "reference_source", None),
                state_json_update={
                    "pending_transition": sanitize_json(details),
                },
            )
        except Exception:
            if bool(getattr(self.cfg.setup_state, "fail_silently", True)):
                return
            raise

    def _refresh_accepted_breakout_pending_states(self) -> None:
        """Invalidate/expire pending impulses even when no breakout candidate exists."""
        if not self._setup_state_read_watch_enabled():
            return
        cfg = self.cfg.accepted_breakout
        for side in SIDES:
            row = self._accepted_breakout_pending_row(side=side)
            if row is None:
                continue
            pending_state = upper(getattr(self.cfg.setup_state, "confirmed_pending_state", "CONFIRMED_PENDING"))
            if upper(getattr(row, "state", "")) != pending_state:
                continue
            state_json = getattr(row, "state_json", None) or {}
            watch = state_json.get("watch") if isinstance(state_json, dict) else None
            if not isinstance(watch, dict):
                continue
            level = self._as_float(watch.get("level_price"))
            event_atr = self._as_float(watch.get("event_atr"))
            event_time = self._to_ist_naive_dt(watch.get("event_time"))
            if level is None or event_atr is None or event_atr <= 0:
                continue
            evaluation = self._accepted_breakout_pending_evaluation(
                side=side,
                level_price=level,
                event_atr=event_atr,
                event_time=event_time,
                bars_outside=0,
                required_bars_outside=1,
            )
            now = self._snapshot_dt()
            expires_at = self._to_ist_naive_dt(getattr(row, "expires_at", None))
            if bool(evaluation.get("invalidated")):
                self._transition_accepted_breakout_pending(
                    row=row,
                    state=getattr(self.cfg.setup_state, "invalidated_state", "INVALIDATED"),
                    reason=str(getattr(cfg, "terminal_displacement_invalidated_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_INVALIDATED")),
                    details={
                        "event": "TERMINAL_DISPLACEMENT_INVALIDATED",
                        "snapshot_time": now.isoformat() if isinstance(now, datetime) else None,
                        "evaluation": evaluation,
                    },
                )
            elif expires_at is not None and now is not None and now > expires_at:
                self._transition_accepted_breakout_pending(
                    row=row,
                    state=getattr(self.cfg.setup_state, "expired_state", "EXPIRED"),
                    reason=str(getattr(cfg, "terminal_displacement_expired_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_EXPIRED")),
                    details={
                        "event": "TERMINAL_DISPLACEMENT_EXPIRED",
                        "snapshot_time": now.isoformat(),
                        "expires_at": expires_at.isoformat(),
                    },
                )

    def _write_accepted_breakout_candidate_state(
        self,
        *,
        observation: Dict[str, Any],
        candidate: SetupCandidate,
    ) -> None:
        """Persist only terminal-displacement pending/confirmed event memory."""
        if not self._setup_state_write_enabled():
            return
        location = dict(candidate.data.get("entry_location_filter") or {})
        terminal = dict(location.get("terminal_displacement") or {})
        if not bool(terminal.get("tracked")) or bool(terminal.get("same_event_rejected")):
            return
        ts = self._snapshot_dt()
        event_time = self._to_ist_naive_dt(observation.get("event_time") or observation.get("attempt_time"))
        if ts is None:
            raise ValueError("ACCEPTED_BREAKOUT persistence requires snapshot.snapshot_time")
        if event_time is None:
            raise ValueError("ACCEPTED_BREAKOUT persistence requires immutable event_time")
        setup_levels = dict(candidate.data.get("setup_levels") or {})
        event_atr = self._as_float(setup_levels.get("event_atr"))
        level_price = self._as_float(setup_levels.get("reference_price"))
        if event_atr is None or event_atr <= 0 or level_price is None:
            return

        confirmed = bool(terminal.get("confirmed"))
        state = (
            getattr(self.cfg.setup_state, "confirmed_state", "CONFIRMED")
            if confirmed
            else getattr(self.cfg.setup_state, "confirmed_pending_state", "CONFIRMED_PENDING")
        )
        reason = (
            str(getattr(self.cfg.accepted_breakout, "terminal_displacement_confirmed_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_CONFIRMED"))
            if confirmed
            else str(getattr(self.cfg.accepted_breakout, "terminal_displacement_pending_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_PENDING"))
        )
        event_key = str(observation.get("event_key") or "")
        if not event_key:
            return
        existing_row = self._accepted_breakout_pending_row(side=upper(candidate.side))
        existing_json = (getattr(existing_row, "state_json", None) or {}) if existing_row is not None else {}
        existing_watch = existing_json.get("watch") if isinstance(existing_json, dict) else None
        pending_context = terminal.get("pending_context") if isinstance(terminal.get("pending_context"), dict) else {}
        same_existing_event = bool(
            isinstance(existing_watch, dict)
            and (
                event_key
                and str(existing_watch.get("event_key") or "") == event_key
                or pending_context.get("same_event")
            )
        )
        if same_existing_event:
            watch = dict(existing_watch)
        else:
            watch = {
                "event_key": event_key,
                "event_source": "EVIDENCE_TERMINAL_DISPLACEMENT",
                "event_time": event_time.isoformat(),
                "side": upper(candidate.side),
                "reference_id": observation.get("reference_id"),
                "level_type": observation.get("level_type"),
                "level_source": observation.get("source"),
                "level_price": level_price,
                "event_atr": event_atr,
                "impulse_open": self._optional_numeric(self._snapshot, "bar.open"),
                "impulse_high": self._optional_numeric(self._snapshot, "bar.high"),
                "impulse_low": self._optional_numeric(self._snapshot, "bar.low"),
                "impulse_close": self._optional_numeric(self._snapshot, "bar.close"),
                "acceptance_path": observation.get("acceptance_path"),
            }
        # Keep immutable event measurements even when later evaluations use a
        # larger current ATR or a different candle shape.
        event_key = str(watch.get("event_key") or event_key)
        event_atr = self._as_float(watch.get("event_atr")) or event_atr
        level_price = self._as_float(watch.get("level_price")) or level_price
        event_time = self._to_ist_naive_dt(watch.get("event_time")) or event_time
        expires_at = event_time + timedelta(
            minutes=float(getattr(self.cfg.accepted_breakout, "terminal_displacement_pending_valid_minutes", 15.5) or 15.5)
        )
        self._safe_setup_state_upsert({
            "trading_day": ts.date(),
            "equity_ref": self._snapshot_equity_ref(),
            "symbol": self._snapshot_symbol() or self._snapshot_equity_ref(),
            "lifecycle": getattr(self.cfg, "lifecycle_name", "DEFAULT"),
            "setup": self.cfg.pattern.setup_accepted_breakout,
            "side": upper(candidate.side),
            "state": state,
            "state_reason": reason,
            "first_seen_time": event_time,
            "last_seen_time": ts,
            "expires_at": expires_at,
            "age_bars": max(0, int(location.get("effective_bars_outside") or 0) - 1),
            "discovery_price": watch.get("impulse_close"),
            "discovery_extreme_price": watch.get("impulse_high") if upper(candidate.side) == BUY else watch.get("impulse_low"),
            "confirmation_price": self._optional_numeric(self._snapshot, "bar.close") if confirmed else None,
            "confirmation_time": ts if confirmed else None,
            "reference_price": level_price,
            "reference_source": observation.get("source"),
            "signal_id": None,
            "state_json": {
                "event_key": event_key,
                "event_source": watch["event_source"],
                "event_time": watch["event_time"],
                "watch": watch,
                "current_evaluation": {
                    "snapshot_time": ts.isoformat(),
                    "confirmed": confirmed,
                    "confirmation_path": terminal.get("confirmation_path"),
                    "pending_context": terminal.get("pending_context"),
                    "entry_blocked": candidate.entry_blocked,
                    "blocked_by": candidate.blocked_by,
                    "setup_levels": setup_levels,
                },
                "source": "SETUP_DISCOVERY_HELPER",
            },
        })

    def _accepted_breakout_level_class(self, observation: Dict[str, Any]) -> str:
        cfg = self.cfg.accepted_breakout
        level_type = upper(observation.get("level_type") or "")
        range_context = observation.get("range_context") if isinstance(observation.get("range_context"), dict) else {}
        range_source = upper(range_context.get("source") or "")
        fixed_types = {upper(value) for value in cfg.fixed_level_types}
        dynamic_types = {upper(value) for value in cfg.dynamic_range_level_types}
        if level_type in fixed_types:
            return "FIXED_MAJOR_LEVEL"
        if level_type in dynamic_types or range_source == "INTRADAY_BALANCE":
            return "DYNAMIC_RANGE"
        return "OTHER_STRUCTURAL_LEVEL"

    def _accepted_breakout_required_bars_context(
        self,
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        cfg = self.cfg.accepted_breakout
        level_class = self._accepted_breakout_level_class(observation)
        if level_class == "FIXED_MAJOR_LEVEL":
            required = int(cfg.fixed_level_min_bars_outside)
        elif level_class == "DYNAMIC_RANGE":
            required = int(cfg.dynamic_range_min_bars_outside)
        else:
            required = int(cfg.other_level_min_bars_outside)
        return {
            "level_class": level_class,
            "required_bars_outside": max(1, required),
            "fixed_level_min_bars_outside": int(cfg.fixed_level_min_bars_outside),
            "dynamic_range_min_bars_outside": int(cfg.dynamic_range_min_bars_outside),
            "other_level_min_bars_outside": int(cfg.other_level_min_bars_outside),
        }

    def _accepted_breakout_strict_displacement_context(
        self,
        *,
        side: str,
        observation: Dict[str, Any],
        atr: float,
        bar_rvol: Optional[float],
    ) -> Dict[str, Any]:
        cfg = self.cfg.accepted_breakout
        side_u = upper(side)
        open_price = require_numeric(self._snapshot, "bar.open")
        high = require_numeric(self._snapshot, "bar.high")
        low = require_numeric(self._snapshot, "bar.low")
        close = require_numeric(self._snapshot, "bar.close")
        current_move_atr = require_numeric(self._snapshot, "market_windows.current.move_atr")
        candle_range = max(0.0, high - low)
        body_fraction = abs(close - open_price) / candle_range if candle_range > 0 else 0.0
        close_position = self._close_position(low=low, high=high, close=close)
        break_distance_atr = self._as_float(observation.get("current_offset_atr"))
        rvol_value = self._as_float(bar_rvol)

        move_ok = (
            current_move_atr >= float(cfg.strict_displacement_min_candle_move_atr)
            if side_u == BUY
            else current_move_atr <= -float(cfg.strict_displacement_min_candle_move_atr)
        )
        close_position_ok = (
            close_position >= float(cfg.strict_displacement_buy_close_position_min)
            if side_u == BUY
            else close_position <= float(cfg.strict_displacement_sell_close_position_max)
        )
        rvol_ok = rvol_value is not None and rvol_value >= float(cfg.strict_displacement_min_bar_rvol)
        body_ok = body_fraction >= float(cfg.strict_displacement_min_body_fraction)
        break_ok = (
            break_distance_atr is not None
            and break_distance_atr >= float(cfg.strict_displacement_min_break_distance_atr)
        )
        enabled = bool(cfg.strict_displacement_enabled)
        qualified = bool(enabled and move_ok and close_position_ok and rvol_ok and body_ok and break_ok)
        return {
            "enabled": enabled,
            "qualified": qualified,
            "side": side_u,
            "current_move_atr": current_move_atr,
            "min_candle_move_atr": float(cfg.strict_displacement_min_candle_move_atr),
            "bar_rvol": rvol_value,
            "min_bar_rvol": float(cfg.strict_displacement_min_bar_rvol),
            "body_fraction": body_fraction,
            "min_body_fraction": float(cfg.strict_displacement_min_body_fraction),
            "close_position": close_position,
            "buy_close_position_min": float(cfg.strict_displacement_buy_close_position_min),
            "sell_close_position_max": float(cfg.strict_displacement_sell_close_position_max),
            "break_distance_atr": break_distance_atr,
            "min_break_distance_atr": float(cfg.strict_displacement_min_break_distance_atr),
            "move_ok": move_ok,
            "rvol_ok": rvol_ok,
            "body_ok": body_ok,
            "close_position_ok": close_position_ok,
            "break_ok": break_ok,
        }

    def _accepted_breakout_dynamic_range_age_context(
        self,
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        cfg = self.cfg.accepted_breakout
        level_class = self._accepted_breakout_level_class(observation)
        enabled = bool(cfg.dynamic_range_age_guard_enabled and level_class == "DYNAMIC_RANGE")
        range_context = observation.get("range_context") if isinstance(observation.get("range_context"), dict) else {}
        established_at = range_context.get("established_at")
        if established_at is None:
            established_at = self._accepted_range_context(self._snapshot).get("established_at")
        established_dt = self._to_ist_naive_dt(established_at)
        snapshot_dt = self._dt_from_snapshot_dict(self._snapshot)
        age_minutes = None
        if established_dt is not None and snapshot_dt is not None:
            age_minutes = max(0.0, (snapshot_dt - established_dt).total_seconds() / 60.0)
        min_age = float(cfg.dynamic_range_min_age_minutes)
        passes = bool(not enabled or (age_minutes is not None and age_minutes >= min_age))
        return {
            "enabled": enabled,
            "level_class": level_class,
            "established_at": established_at,
            "snapshot_time": self._optional_path(self._snapshot, "snapshot_time"),
            "age_minutes": age_minutes,
            "min_age_minutes": min_age,
            "passes": passes,
        }

    def _accepted_breakout_structural_room_context(
        self,
        *,
        side: str,
        close: float,
        atr: float,
        strict_displacement_qualified: bool,
    ) -> Dict[str, Any]:
        cfg = self.cfg.accepted_breakout
        enabled = bool(cfg.structural_room_guard_enabled)
        next_level = self._next_external_level_context(self._snapshot, side, close)
        available = bool(next_level.get("available"))
        distance_points = self._as_float(next_level.get("distance_points"))
        distance_atr = distance_points / atr if distance_points is not None and atr > 0 else None
        distance_pct = (distance_points / close) * 100.0 if distance_points is not None and close > 0 else None
        required_points = max(
            float(cfg.structural_room_min_atr) * atr,
            (float(cfg.structural_room_min_pct) / 100.0) * close,
        )
        raw_passes = bool(not available or (distance_points is not None and distance_points >= required_points))
        bypassed = bool(
            enabled
            and not raw_passes
            and strict_displacement_qualified
            and cfg.strict_displacement_room_bypass_enabled
        )
        passes = bool(not enabled or raw_passes or bypassed)
        return {
            "enabled": enabled,
            "available": available,
            "passes": passes,
            "raw_passes": raw_passes,
            "bypassed_by_strict_displacement": bypassed,
            "level_type": next_level.get("level_type"),
            "price": next_level.get("price"),
            "distance_points": distance_points,
            "distance_atr": distance_atr,
            "distance_pct": distance_pct,
            "required_points": required_points,
            "min_room_atr": float(cfg.structural_room_min_atr),
            "min_room_pct": float(cfg.structural_room_min_pct),
        }

    def _accepted_breakout_entry_filter(
        self,
        *,
        side: str,
        bars_outside: int,
        level_price: float,
        atr_buffer: float,
        allow_create: bool,
        path_type: str = "MAJOR_LEVEL_ACCEPTANCE",
        level_observation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        d = self._snapshot
        cfg = self.cfg.accepted_breakout
        exhaustion_cfg = self.cfg.exhaustion_reversal
        close = require_numeric(d, "bar.close")
        high = require_numeric(d, "bar.high")
        low = require_numeric(d, "bar.low")
        atr = require_numeric(d, "indicators.atr.value")
        rsi = require_numeric(d, "indicators.rsi.value")
        bb_pos = require_numeric(d, "indicators.bollinger.position")
        pos_15m = require_numeric(d, "market_windows.15m.close_position_in_range")
        bar_rvol = self._optional_numeric(d, "volume.bar_rvol")
        bar_rvol_band = upper((self._optional_path(d, "volume.bar_rvol_band") or "NA"))
        level = float(level_price)
        buffer_points = float(atr_buffer) * atr
        observation = level_observation or {}
        path_u = upper(path_type)
        is_early_path = path_u in {"EARLY_MAJOR_LEVEL_BREAK", "LEVEL_TEST"}
        observed_strong_displacement_path = path_u == "STRONG_DISPLACEMENT_ACCEPTANCE"
        is_major_level_reacceptance = path_u == "MAJOR_LEVEL_REACCEPTANCE"
        level_priority_context = dict(observation.get("level_priority_context") or {})

        if side == BUY:
            invalidation = level - buffer_points
            break_distance_points = close - level
            break_direction_valid = break_distance_points > 0
            entry_distance_points = break_distance_points
        elif side == SELL:
            invalidation = level + buffer_points
            break_distance_points = level - close
            break_direction_valid = break_distance_points > 0
            entry_distance_points = break_distance_points
        else:
            raise ValueError(f"Unsupported side for accepted breakout entry filter: {side}")

        break_distance_atr = break_distance_points / atr if atr else None
        entry_distance_atr = entry_distance_points / atr if atr else None
        effective_bars_outside = int(bars_outside or 0)
        acceptance_policy = self._accepted_breakout_required_bars_context(observation)
        strict_displacement = self._accepted_breakout_strict_displacement_context(
            side=side,
            observation=observation,
            atr=atr,
            bar_rvol=bar_rvol,
        )
        strict_displacement_qualified = bool(strict_displacement.get("qualified"))
        strict_displacement_immediate_create_enabled = bool(
            getattr(cfg, "strict_displacement_immediate_create_enabled", False)
        )
        if strict_displacement_qualified and strict_displacement_immediate_create_enabled:
            # Compatibility switch only. The production policy keeps this off:
            # a single impulse candle is evidence, not accepted auction.
            effective_acceptance_path = "STRICT_STRONG_DISPLACEMENT_ACCEPTANCE"
            min_bars_outside_used = 1
            min_break_distance_atr = float(cfg.strict_displacement_min_break_distance_atr)
            max_entry_distance_atr = float(cfg.strong_displacement_max_entry_distance_atr)
        else:
            effective_acceptance_path = (
                "DISPLACEMENT_MULTI_CLOSE_ACCEPTANCE"
                if strict_displacement_qualified
                else path_type
            )
            min_bars_outside_used = int(acceptance_policy["required_bars_outside"])
            min_break_distance_atr = (
                float(cfg.strict_displacement_min_break_distance_atr)
                if strict_displacement_qualified
                else (float(cfg.early_min_break_distance_atr) if is_early_path else 0.0)
            )
            max_entry_distance_atr = (
                float(cfg.strong_displacement_max_entry_distance_atr)
                if strict_displacement_qualified
                else (cfg.early_max_entry_distance_from_level_atr if is_early_path else cfg.max_entry_distance_from_level_atr)
            )

        accepted = self._accepted_range_context(d)
        accepted_high = self._as_float(accepted.get("high"))
        accepted_low = self._as_float(accepted.get("low"))
        accepted_range_width_points = None
        accepted_range_width_atr = None
        if accepted_high is not None and accepted_low is not None and accepted_high > accepted_low:
            accepted_range_width_points = accepted_high - accepted_low
            accepted_range_width_atr = accepted_range_width_points / atr if atr else None

        hma = self._hma_context_for_breakout(d, side)
        participation = self._participation_context(d)
        terminal_extension = self._accepted_breakout_terminal_extension_context(d, side)
        dynamic_range_age = self._accepted_breakout_dynamic_range_age_context(observation)
        structural_room = self._accepted_breakout_structural_room_context(
            side=side,
            close=close,
            atr=atr,
            strict_displacement_qualified=strict_displacement_qualified,
        )

        terminal_pending_enabled = bool(getattr(cfg, "terminal_displacement_pending_enabled", True))
        terminal_required_bars = max(
            int(acceptance_policy["required_bars_outside"]),
            1 + max(1, int(getattr(cfg, "terminal_displacement_min_post_impulse_closes", 2) or 2)),
        )
        pending_context = self._accepted_breakout_pending_context(
            side=side,
            observation=observation,
            level_price=level,
            event_atr=atr,
            bars_outside=effective_bars_outside,
            required_bars_outside=int(acceptance_policy["required_bars_outside"]),
        )
        terminal_sustained_now = bool(
            terminal_pending_enabled
            and terminal_extension.get("blocked")
            and strict_displacement_qualified
            and effective_bars_outside >= terminal_required_bars
        )
        terminal_confirmation_path = pending_context.get("confirmation_path")
        if terminal_sustained_now and not terminal_confirmation_path:
            terminal_confirmation_path = "TERMINAL_DISPLACEMENT_SUSTAINED_ACCEPTANCE"
        terminal_confirmed = bool(pending_context.get("confirmed") or terminal_sustained_now)
        terminal_same_event_rejected = bool(
            pending_context.get("same_event")
            and (pending_context.get("invalidated") or pending_context.get("expired"))
        )
        terminal_tracked = bool(
            terminal_pending_enabled
            and (
                terminal_extension.get("blocked") and strict_displacement_qualified
                or pending_context.get("active")
                or terminal_confirmed
                or terminal_same_event_rejected
            )
        )
        terminal_pending = bool(terminal_tracked and not terminal_confirmed and not terminal_same_event_rejected)
        if terminal_confirmed:
            effective_acceptance_path = str(terminal_confirmation_path or "TERMINAL_DISPLACEMENT_CONFIRMED")
            if effective_acceptance_path == "TERMINAL_DISPLACEMENT_RETEST_RECLAIM":
                min_bars_outside_used = 1
            else:
                min_bars_outside_used = terminal_required_bars

        resolved_terminal_event_key = (
            pending_context.get("stored_event_key")
            if pending_context.get("same_event")
            else observation.get("event_key")
        )
        resolved_terminal_event_time = (
            pending_context.get("stored_event_time")
            if pending_context.get("same_event")
            else observation.get("event_time") or observation.get("attempt_time")
        )
        resolved_terminal_event_atr = (
            pending_context.get("stored_event_atr")
            if pending_context.get("same_event")
            else atr
        )
        terminal_displacement = {
            "enabled": terminal_pending_enabled,
            "tracked": terminal_tracked,
            "pending": terminal_pending,
            "confirmed": terminal_confirmed,
            "same_event_rejected": terminal_same_event_rejected,
            "confirmation_path": terminal_confirmation_path,
            "required_bars_outside": terminal_required_bars,
            "event_key": resolved_terminal_event_key,
            "event_time": resolved_terminal_event_time,
            "event_atr": resolved_terminal_event_atr,
            "pending_context": pending_context,
        }

        risk_flags: List[str] = []
        blocked = False
        blocked_by: Optional[str] = None

        def block(code: str) -> None:
            nonlocal blocked, blocked_by
            blocked = True
            if blocked_by is None:
                blocked_by = code
            risk_flags.append(code)

        if not allow_create:
            block("ACCEPTED_BREAKOUT_WATCH_ONLY_NO_CREATE")
        if (
            is_early_path
            and not strict_displacement_qualified
            and not terminal_confirmed
            and not bool(cfg.generic_early_create_enabled)
        ):
            block("ACCEPTED_BREAKOUT_GENERIC_EARLY_CREATE_DISABLED")
        if observed_strong_displacement_path and not strict_displacement_qualified:
            block("ACCEPTED_BREAKOUT_STRONG_DISPLACEMENT_QUALITY_NOT_MET")
        if is_major_level_reacceptance and not bool(cfg.major_level_reacceptance_enabled):
            block("ACCEPTED_BREAKOUT_MAJOR_LEVEL_REACCEPTANCE_DISABLED_INSIDE_ACTIVE_DYNAMIC_RANGE")
        if (
            is_major_level_reacceptance
            and bool(cfg.major_level_reacceptance_enabled)
            and effective_bars_outside < int(cfg.major_level_reacceptance_min_bars_outside)
        ):
            block(
                "ACCEPTED_BREAKOUT_MAJOR_LEVEL_REACCEPTANCE_BARS_OUTSIDE_LT_"
                f"{int(cfg.major_level_reacceptance_min_bars_outside)}"
            )
        if not break_direction_valid:
            block(f"ACCEPTED_BREAKOUT_{side}_CLOSE_NOT_OUTSIDE_LEVEL")
        if break_distance_atr is not None and break_distance_atr < min_break_distance_atr:
            block(f"ACCEPTED_BREAKOUT_{path_type}_BREAK_DISTANCE_LT_{min_break_distance_atr:.2f}_ATR")
        if effective_bars_outside < min_bars_outside_used and not terminal_pending:
            block(
                "ACCEPTED_BREAKOUT_"
                f"{acceptance_policy['level_class']}_BARS_OUTSIDE_LT_{min_bars_outside_used}"
            )
        if entry_distance_atr is not None and entry_distance_atr > max_entry_distance_atr:
            block(
                f"ACCEPTED_BREAKOUT_{effective_acceptance_path}_"
                f"ENTRY_DISTANCE_FROM_LEVEL_GT_{max_entry_distance_atr:.2f}_ATR"
            )
        if not bool(dynamic_range_age.get("passes")):
            block(
                "ACCEPTED_BREAKOUT_DYNAMIC_RANGE_AGE_LT_"
                f"{float(dynamic_range_age.get('min_age_minutes') or 0.0):.0f}_MIN"
            )
        if terminal_same_event_rejected:
            code = (
                str(getattr(cfg, "terminal_displacement_invalidated_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_INVALIDATED"))
                if pending_context.get("invalidated")
                else str(getattr(cfg, "terminal_displacement_expired_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_EXPIRED"))
            )
            block(code)
        elif terminal_pending:
            block(str(getattr(cfg, "terminal_displacement_pending_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_PENDING")))
        elif terminal_confirmed:
            risk_flags.append(
                str(getattr(cfg, "terminal_displacement_confirmed_reason_code", "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_CONFIRMED"))
            )
        elif bool(terminal_extension.get("blocked")):
            if (
                strict_displacement_qualified
                and not terminal_pending_enabled
                and bool(getattr(cfg, "terminal_extension_strict_displacement_bypass_enabled", False))
            ):
                risk_flags.append(
                    "ACCEPTED_BREAKOUT_TERMINAL_EXTENSION_BYPASSED_BY_STRICT_DISPLACEMENT"
                )
            else:
                block(str(terminal_extension.get("reason")))
        if not bool(structural_room.get("passes")):
            block("ACCEPTED_BREAKOUT_INSUFFICIENT_STRUCTURAL_ROOM")
        elif bool(structural_room.get("bypassed_by_strict_displacement")):
            risk_flags.append(
                "ACCEPTED_BREAKOUT_STRUCTURAL_ROOM_BYPASSED_BY_STRICT_DISPLACEMENT"
            )
        # Snapshot has already qualified the active dynamic range. Evidence does
        # not reject a structural level merely because the current active range
        # is narrow; MICRO_COMPRESSION uses wider observation buffers instead.
        # Participation is a hard gate only when the snapshot says it is weak/stale.
        if bar_rvol is not None and bar_rvol < cfg.min_bar_rvol:
            block(f"ACCEPTED_BREAKOUT_BAR_RVOL_LT_{cfg.min_bar_rvol:.2f}")
        if bar_rvol_band in {upper(x) for x in cfg.weak_participation_bands}:
            block(f"ACCEPTED_BREAKOUT_WEAK_PARTICIPATION_{bar_rvol_band}")

        # Accepted breakout can naturally be near Bollinger extremes, so only block
        # severe exhaustion. This keeps it from becoming a late momentum chase.
        if side == BUY:
            if rsi >= cfg.block_buy_rsi_min and bb_pos >= cfg.block_buy_bollinger_position_min:
                block("ACCEPTED_BREAKOUT_BUY_SEVERE_UPPER_EXHAUSTION")
            if rsi >= exhaustion_cfg.block_buy_if_rsi_min and bb_pos >= cfg.block_buy_bollinger_position_min:
                block("ACCEPTED_BREAKOUT_BUY_LATE_UPPER_BAND_CHASE")
        elif side == SELL:
            if rsi <= cfg.block_sell_rsi_max and bb_pos <= cfg.block_sell_bollinger_position_max:
                block("ACCEPTED_BREAKOUT_SELL_SEVERE_LOWER_EXHAUSTION")
            if rsi <= exhaustion_cfg.block_sell_if_rsi_max and bb_pos <= cfg.block_sell_bollinger_position_max:
                block("ACCEPTED_BREAKOUT_SELL_LATE_LOWER_BAND_CHASE")

        if cfg.require_hma_side_alignment and not bool(hma.get("aligned")):
            block("ACCEPTED_BREAKOUT_HMA_NOT_ALIGNED")

        return {
            "blocked": blocked,
            "blocked_by": blocked_by,
            "risk_flags": risk_flags,
            "side": side,
            "close": close,
            "level_price": level,
            "acceptance_path": path_type,
            "effective_acceptance_path": effective_acceptance_path,
            "is_early_path": is_early_path,
            "is_strong_displacement_path": strict_displacement_qualified,
            "observed_strong_displacement_path": observed_strong_displacement_path,
            "is_major_level_reacceptance": is_major_level_reacceptance,
            "level_priority_context": level_priority_context,
            "acceptance_policy": acceptance_policy,
            "strict_displacement": strict_displacement,
            "dynamic_range_age": dynamic_range_age,
            "structural_room": structural_room,
            "break_direction_valid": break_direction_valid,
            "break_distance_points": break_distance_points,
            "break_distance_atr": break_distance_atr,
            "min_break_distance_atr": min_break_distance_atr,
            "bars_outside": bars_outside,
            "effective_bars_outside": effective_bars_outside,
            "min_bars_outside": min_bars_outside_used,
            "mature_min_bars_outside": cfg.min_bars_outside,
            "early_min_bars_outside": cfg.early_min_bars_outside,
            "atr": atr,
            "atr_buffer": atr_buffer,
            "buffer_points": buffer_points,
            "entry_distance_from_level_points": entry_distance_points,
            "entry_distance_from_level_atr": entry_distance_atr,
            "max_entry_distance_from_level_atr": max_entry_distance_atr,
            "mature_max_entry_distance_from_level_atr": cfg.max_entry_distance_from_level_atr,
            "early_max_entry_distance_from_level_atr": cfg.early_max_entry_distance_from_level_atr,
            "strong_displacement_max_entry_distance_atr": cfg.strong_displacement_max_entry_distance_atr,
            "accepted_range_high": accepted_high,
            "accepted_range_low": accepted_low,
            "accepted_range_width_points": accepted_range_width_points,
            "accepted_range_width_atr": accepted_range_width_atr,
            "min_accepted_range_width_atr": cfg.min_accepted_range_width_atr,
            "level_observation": observation,
            "rsi": rsi,
            "bollinger_position": bb_pos,
            "position_15m": pos_15m,
            "bar_rvol": bar_rvol,
            "bar_rvol_band": bar_rvol_band,
            "min_bar_rvol": cfg.min_bar_rvol,
            "hma": hma,
            "participation": participation,
            "terminal_extension": terminal_extension,
            "terminal_displacement": terminal_displacement,
        }

    def _accepted_breakout_setup_levels(
        self,
        *,
        side: str,
        setup_label: str,
        level_price: float,
        atr_buffer: float,
        event_atr: Optional[float] = None,
        event_key: Any = None,
        event_time: Any = None,
        event_source: Any = None,
        level_type: Any = None,
        level_source: Any = None,
        reference_id: Any = None,
        acceptance_path: Any = None,
    ) -> Dict[str, Any]:
        d = self._snapshot
        close = require_numeric(d, "bar.close")
        frozen_event_atr = self._as_float(event_atr)
        if frozen_event_atr is None or frozen_event_atr <= 0:
            frozen_event_atr = require_numeric(d, "indicators.atr.value")
        level = float(level_price)
        side_u = upper(side)
        if side_u == BUY:
            invalidation_side = "BELOW"
        elif side_u == SELL:
            invalidation_side = "ABOVE"
        else:
            raise ValueError(f"Unsupported side for accepted breakout setup levels: {side}")
        return {
            "setup_label": setup_label,
            "side": side_u,
            "reference_type": "ACCEPTED_BREAKOUT_LEVEL",
            "reference_price": level,
            "reference_source": "accepted_breakout_level",
            "level_price": level,
            "signal_invalidation_reference_price": level,
            "signal_invalidation_reference_source": "accepted_breakout_level",
            "signal_invalidation_reference_policy": "CONFIGURED_BUFFERED_REACCEPTANCE",
            "signal_invalidation_atr_policy": "FROZEN_EVENT_ATR",
            "event_atr": frozen_event_atr,
            "event_key": str(event_key) if event_key else None,
            "event_time": str(event_time) if event_time is not None else None,
            "event_source": str(event_source) if event_source else None,
            "reference_id": str(reference_id) if reference_id else None,
            "level_type": str(level_type) if level_type else None,
            "level_source": str(level_source) if level_source else None,
            "acceptance_path": str(acceptance_path) if acceptance_path else None,
            "initial_stop_reference_price": level,
            "initial_stop_reference_source": "accepted_breakout_level",
            "initial_stop_side": invalidation_side,
            "invalidation_side": invalidation_side,
            "confirmation_price": close,
            "note": (
                "Signal lifecycle applies setup-specific buffered/re-acceptance rules "
                "to the raw structural level. TradeManager owns the live SL, targets, "
                "trailing and trade exits."
            ),
        }

    @staticmethod
    def _candidate_is_setup(candidate: SetupCandidate, setup_label: str) -> bool:
        return upper(candidate.setup_label) == upper(setup_label)

    @staticmethod
    def _candidates_are_opposite(a: SetupCandidate, b: SetupCandidate) -> bool:
        return upper(a.side) in SIDES and upper(b.side) in SIDES and upper(a.side) != upper(b.side)

    @staticmethod
    def _candidate_has_watch_context(candidate: SetupCandidate) -> bool:
        data = candidate.data if isinstance(candidate.data, dict) else {}
        setup_inputs = data.get("setup_inputs") if isinstance(data.get("setup_inputs"), dict) else {}
        promotion = data.get("watch_promotion") if isinstance(data.get("watch_promotion"), dict) else {}
        blocked_by = upper(candidate.blocked_by or "")
        return bool(
            isinstance(setup_inputs.get("watched_extreme"), dict)
            or isinstance(promotion.get("watched_extreme"), dict)
            or candidate.reason_code == EVIDENCE_CONFIG.reason.setup_not_confirmed_code
            or blocked_by == "EXHAUSTION_REVERSAL_PRICE_ACTION_NOT_CONFIRMED"
        )

    def _setup_conflict_defer_result(
        self,
        *,
        candidates: List[SetupCandidate],
        confirmed: List[SetupCandidate],
        exhaustion: SetupCandidate,
        breakout: SetupCandidate,
        reason_code: str,
        reason_text: str,
        confirmed_conflict: bool,
    ) -> SetupDiscoveryResult:
        risk_flags: List[str] = []
        for candidate in candidates:
            risk_flags.extend(candidate.risk_flags)
        risk_flags.append(reason_code)
        return SetupDiscoveryResult(
            discovered_setups=candidates,
            confirmed_setups=confirmed,
            primary_setup=None,
            supporting_setups=[],
            decision="DEFER",
            evaluator_state="ENTRY_DEFERRED",
            preferred_side=upper(exhaustion.side),
            reason_code=reason_code,
            reason_text=reason_text,
            blocked_by="OPPOSING_EXHAUSTION_CONTEXT",
            price_action_confirmed=bool(confirmed_conflict),
            price_action_strength=max(
                float(exhaustion.price_action_strength or 0.0),
                float(breakout.price_action_strength or 0.0),
            ),
            risk_flags=list(dict.fromkeys(risk_flags)),
        )

    def _select(self, candidates: List[SetupCandidate]) -> SetupDiscoveryResult:
        if not candidates:
            return SetupDiscoveryResult(
                discovered_setups=[],
                confirmed_setups=[],
                primary_setup=None,
                supporting_setups=[],
                decision="DEFER",
                evaluator_state="NO_SETUP",
                preferred_side="NONE",
                reason_code=self.cfg.reason.no_setup_code,
                reason_text="No enabled setup discovered.",
                blocked_by=None,
                price_action_confirmed=False,
                price_action_strength=0.0,
                risk_flags=[],
            )

        all_confirmed = [c for c in candidates if c.price_action_confirmed]
        confirmed = [c for c in all_confirmed if not c.entry_blocked]

        exhaustion_label = self.cfg.pattern.setup_exhaustion_reversal
        breakout_label = self.cfg.pattern.setup_accepted_breakout
        ready_breakouts = [
            c for c in confirmed
            if self._candidate_is_setup(c, breakout_label)
        ]
        confirmed_exhaustion = [
            c for c in all_confirmed
            if self._candidate_is_setup(c, exhaustion_label)
        ]
        confirmed_exhaustion_sides = {upper(c.side) for c in confirmed_exhaustion}
        exhaustion_watches = [
            c for c in candidates
            if self._candidate_is_setup(c, exhaustion_label)
            and not c.price_action_confirmed
            and upper(c.side) not in confirmed_exhaustion_sides
            and self._candidate_has_watch_context(c)
        ]

        # A confirmed exhaustion may be too late/consumed for a new reversal
        # entry, but it still disproves an opposing continuation thesis. Do not
        # allow the accepted breakout merely because the exhaustion candidate is
        # blocked by its own fresh-entry location filter.
        if bool(getattr(self.cfg.price_action, "confirmed_exhaustion_overrides_accepted_breakout", True)):
            for exhaustion in sorted(
                confirmed_exhaustion,
                key=lambda c: (c.entry_blocked, c.priority, -c.price_action_strength),
            ):
                opposing = next(
                    (b for b in ready_breakouts if self._candidates_are_opposite(exhaustion, b)),
                    None,
                )
                if opposing is None:
                    continue
                if exhaustion.entry_blocked:
                    return self._setup_conflict_defer_result(
                        candidates=candidates,
                        confirmed=confirmed,
                        exhaustion=exhaustion,
                        breakout=opposing,
                        reason_code=self.cfg.reason.breakout_blocked_by_confirmed_exhaustion_code,
                        reason_text=(
                            f"{opposing.side} accepted breakout is deferred because opposing "
                            f"{exhaustion.side} exhaustion is price-action confirmed. The "
                            "reversal entry is currently blocked/late, but continuation is no "
                            "longer clean."
                        ),
                        confirmed_conflict=True,
                    )

        # An unresolved extreme WATCH is exactly the situation where another
        # completed candle adds information. Do not force continuation from the
        # first expansion candle while the opposing exhaustion event is alive.
        if bool(getattr(self.cfg.price_action, "exhaustion_watch_defers_opposing_accepted_breakout", True)):
            for breakout in ready_breakouts:
                opposing_watch = next(
                    (e for e in exhaustion_watches if self._candidates_are_opposite(e, breakout)),
                    None,
                )
                if opposing_watch is not None:
                    return self._setup_conflict_defer_result(
                        candidates=candidates,
                        confirmed=confirmed,
                        exhaustion=opposing_watch,
                        breakout=breakout,
                        reason_code=self.cfg.reason.breakout_deferred_by_exhaustion_watch_code,
                        reason_text=(
                            f"{breakout.side} accepted breakout is deferred for one completed "
                            f"candle because opposing {opposing_watch.side} exhaustion WATCH is "
                            "still unresolved. Hold outside/retest can restore continuation; "
                            "rejection/reclaim confirms reversal."
                        ),
                        confirmed_conflict=False,
                    )

        if not confirmed:
            best = sorted(candidates, key=lambda c: (c.price_action_confirmed, -c.priority, c.price_action_strength), reverse=True)[0]
            risk_flags: List[str] = []
            for c in candidates:
                risk_flags.extend(c.risk_flags)
            state = "ENTRY_DEFERRED" if any(c.entry_blocked for c in candidates) else "SETUP_DISCOVERED"
            return SetupDiscoveryResult(
                discovered_setups=candidates,
                confirmed_setups=[],
                primary_setup=None,
                supporting_setups=[],
                decision="DEFER",
                evaluator_state=state,
                preferred_side=best.side,
                reason_code=best.reason_code,
                reason_text=best.reason_text,
                blocked_by=best.blocked_by,
                price_action_confirmed=best.price_action_confirmed,
                price_action_strength=best.price_action_strength,
                risk_flags=risk_flags,
            )

        by_side: Dict[str, List[SetupCandidate]] = {BUY: [], SELL: []}
        for c in confirmed:
            by_side[c.side].append(c)
        side_best: Dict[str, SetupCandidate] = {}
        for side in SIDES:
            if by_side[side]:
                side_best[side] = sorted(by_side[side], key=lambda c: (c.priority, -c.price_action_strength))[0]

        if BUY in side_best and SELL in side_best:
            buy_candidate = side_best[BUY]
            sell_candidate = side_best[SELL]
            pair = {upper(buy_candidate.setup_label), upper(sell_candidate.setup_label)}
            exhaustion_breakout_pair = {
                upper(exhaustion_label),
                upper(breakout_label),
            }
            if (
                bool(getattr(self.cfg.price_action, "confirmed_exhaustion_overrides_accepted_breakout", True))
                and pair == exhaustion_breakout_pair
            ):
                primary = (
                    buy_candidate
                    if self._candidate_is_setup(buy_candidate, exhaustion_label)
                    else sell_candidate
                )
            else:
                buy_strength = buy_candidate.price_action_strength
                sell_strength = sell_candidate.price_action_strength
                gap = abs(buy_strength - sell_strength)
                if gap < self.cfg.price_action.side_conflict_min_strength_gap:
                    return SetupDiscoveryResult(
                        discovered_setups=candidates,
                        confirmed_setups=confirmed,
                        primary_setup=None,
                        supporting_setups=[],
                        decision="DEFER",
                        evaluator_state="ENTRY_DEFERRED",
                        preferred_side="NONE",
                        reason_code=self.cfg.reason.side_conflict_code,
                        reason_text=f"BUY and SELL price-action strengths are too close: BUY={buy_strength:.2f}, SELL={sell_strength:.2f}.",
                        blocked_by="SIDE_CONFLICT",
                        price_action_confirmed=True,
                        price_action_strength=max(buy_strength, sell_strength),
                        risk_flags=["SIDE_CONFLICT"],
                    )
                primary = buy_candidate if buy_strength > sell_strength else sell_candidate
        else:
            primary = next(iter(side_best.values()))

        reason_code = primary.reason_code
        reason_text = primary.reason_text
        if (
            self._candidate_is_setup(primary, exhaustion_label)
            and any(
                self._candidate_is_setup(c, breakout_label)
                and self._candidates_are_opposite(primary, c)
                for c in confirmed
            )
        ):
            reason_code = self.cfg.reason.exhaustion_overrides_breakout_code
            reason_text = (
                f"Confirmed {primary.side} exhaustion reversal overrides the opposing "
                "accepted-breakout continuation candidate."
            )

        supporting = [c for c in confirmed if c is not primary]
        return SetupDiscoveryResult(
            discovered_setups=candidates,
            confirmed_setups=confirmed,
            primary_setup=primary,
            supporting_setups=supporting,
            decision="CREATE",
            evaluator_state="ENTRY_READY",
            preferred_side=primary.side,
            reason_code=reason_code,
            reason_text=reason_text,
            blocked_by=None,
            price_action_confirmed=True,
            price_action_strength=primary.price_action_strength,
            risk_flags=primary.risk_flags,
        )

    def _breakout_reference_context(self, d: Dict[str, Any], breakout_side: str) -> Optional[Dict[str, Any]]:
        """Compatibility helper backed by Evidence-derived exact-level state."""
        side = upper(breakout_side)
        observations = [
            item
            for item in self._derived_breakout_observations(d)
            if upper(item.get("side")) == side and upper(item.get("status")) != "NONE"
        ]
        if not observations:
            return None
        observation = sorted(observations, key=lambda x: int(x.get("rank") or 999))[0]
        return {
            "reference_id": observation.get("reference_id"),
            "level_type": observation.get("level_type"),
            "price": observation.get("price"),
            "side": side,
            "source": observation.get("source"),
            "role": "FAILED_BREAKOUT_REFERENCE",
            "aliases": observation.get("aliases", []),
            "range_context": observation.get("range_context", {}),
        }

    def _accepted_breakout_candidate_sort_key(self, candidate: SetupCandidate) -> tuple:
        filt = candidate.data.get("entry_location_filter", {}) if isinstance(candidate.data, dict) else {}
        inputs = candidate.data.get("setup_inputs", {}) if isinstance(candidate.data, dict) else {}
        entry_dist = self._as_float(filt.get("entry_distance_from_level_atr"))
        rank = self._as_int(inputs.get("level_rank")) or 999
        return (
            1 if candidate.entry_blocked else 0,
            rank,
            entry_dist if entry_dist is not None else 999.0,
        )

    def _accepted_breakout_level_candidates(self, d: Dict[str, Any], breakout_side: str) -> List[Dict[str, Any]]:
        """Compatibility view of exact neutral levels currently broken."""
        side = upper(breakout_side)
        return [
            {
                "reference_id": item.get("reference_id"),
                "level_type": item.get("level_type"),
                "price": item.get("price"),
                "side": side,
                "source": item.get("source"),
                "role": "ACCEPTED_BREAKOUT_REFERENCE",
                "rank": item.get("rank"),
                "tags": item.get("aliases", []),
                "status": item.get("status"),
                "bars_outside": item.get("bars_outside"),
                "acceptance_path": item.get("acceptance_path"),
            }
            for item in self._derived_breakout_observations(d)
            if upper(item.get("side")) == side
            and upper(item.get("status")) in {"BREAKOUT_ATTEMPT", "BREAKOUT_TESTING", "ACCEPTED_BREAKOUT"}
        ]

    def _accepted_breakout_reference_context(self, d: Dict[str, Any], breakout_side: str) -> Optional[Dict[str, Any]]:
        candidates = self._accepted_breakout_level_candidates(d, breakout_side)
        return candidates[0] if candidates else None

    def _next_external_level_context(self, d: Dict[str, Any], side: str, close: float) -> Dict[str, Any]:
        levels = self._optional_path(d, "levels") or {}
        anchors = self._optional_path(d, "structure.anchors") or {}
        prev_day = levels.get("prev_day") if isinstance(levels, dict) else {}
        opening = levels.get("opening_range") if isinstance(levels, dict) else {}
        candidates: List[Dict[str, Any]] = []

        def add(name: str, value: Any) -> None:
            price = self._as_float(value)
            if price is None:
                return
            if side == BUY and price > close:
                candidates.append({"level_type": name, "price": price, "distance_points": price - close})
            elif side == SELL and price < close:
                candidates.append({"level_type": name, "price": price, "distance_points": close - price})

        if side == BUY:
            add("PREVIOUS_DAY_HIGH", (prev_day or {}).get("high"))
            add("ORB_HIGH", (opening or {}).get("high"))
            add("ANCHOR_RECENT15_HIGH", anchors.get("recent15_high") if isinstance(anchors, dict) else None)
        elif side == SELL:
            add("PREVIOUS_DAY_LOW", (prev_day or {}).get("low"))
            add("ORB_LOW", (opening or {}).get("low"))
            add("ANCHOR_RECENT15_LOW", anchors.get("recent15_low") if isinstance(anchors, dict) else None)

        if not candidates:
            return {"available": False, "level_type": None, "price": None, "distance_points": None}
        best = sorted(candidates, key=lambda x: float(x.get("distance_points") or 0.0))[0]
        best["available"] = True
        return best

    def _hma_context_for_breakout(self, d: Dict[str, Any], side: str) -> Dict[str, Any]:
        hma = self._optional_path(d, "indicators.hma") or {}
        windows = self._optional_path(d, "indicator_windows.hma") or {}
        state = upper(hma.get("state") if isinstance(hma, dict) else None)
        strength = upper(hma.get("strength") if isinstance(hma, dict) else None)
        aligned_states = self.cfg.opportunity.buy_hma_states if side == BUY else self.cfg.opportunity.sell_hma_states
        aligned = state in {upper(x) for x in aligned_states}
        return {
            "state": state,
            "strength": strength,
            "aligned": aligned,
            "windows": windows if isinstance(windows, dict) else {},
            "note": "HMA compression/expansion supports accepted breakout but is not a standalone setup.",
        }

    def _participation_context(self, d: Dict[str, Any]) -> Dict[str, Any]:
        volume = self._optional_path(d, "volume") or {}
        if not isinstance(volume, dict):
            volume = {}
        return {
            "bar_rvol": self._as_float(volume.get("bar_rvol")),
            "bar_rvol_pct": self._as_float(volume.get("bar_rvol_pct")),
            "bar_rvol_band": upper(volume.get("bar_rvol_band") or "NA"),
            "bar_volume_slope": self._as_float(volume.get("bar_volume_slope")),
            "today_vs_prev_ratio": self._as_float(volume.get("today_vs_prev_ratio")),
        }

    def _accepted_range_context(self, d: Dict[str, Any]) -> Dict[str, Any]:
        accepted_range = self._optional_path(d, "structure.accepted.range") or {}
        accepted = self._optional_path(d, "structure.accepted") or {}
        return {
            "range_id": accepted_range.get("range_id"),
            "version": self._as_int(accepted_range.get("version")),
            "high": self._as_float(accepted_range.get("high")),
            "low": self._as_float(accepted_range.get("low")),
            "source": accepted_range.get("source"),
            "range_type": accepted_range.get("range_type"),
            "width_pct": self._as_float(accepted_range.get("width_pct")),
            "width_atr": self._as_float(accepted_range.get("width_atr")),
            "start_time": accepted_range.get("start_time"),
            "end_time": accepted_range.get("end_time"),
            "established_at": accepted_range.get("established_at"),
            "evidence_cutoff": accepted_range.get("evidence_cutoff"),
            "bars": self._as_int(accepted_range.get("bars")),
            "provisional": bool(accepted_range.get("provisional", False)),
            "breakout_eligible": bool(accepted_range.get("breakout_eligible", False)),
            "quality": self._as_float(accepted.get("quality")) if isinstance(accepted, dict) else None,
        }

    @staticmethod
    def _is_inside_accepted_range(*, close: float, accepted: Dict[str, Any]) -> bool:
        high = SetupDiscoverer._as_float(accepted.get("high"))
        low = SetupDiscoverer._as_float(accepted.get("low"))
        if high is None or low is None or high <= low:
            return False
        return low <= float(close) <= high

    def _accepted_breakout_level_priority_context(
        self,
        d: Dict[str, Any],
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Classify fixed-level reacceptance inside the current balance.

        The active qualified intraday balance is the primary auction structure.
        ORB/PDH/PDL remain permanent external references, but when price is still
        inside that current balance they represent a reacceptance of an older
        level, not a breakout from the active range. Keeping this distinction in
        Evidence preserves neutral snapshot structure while making the setup path
        explicit and auditable.
        """
        cfg = self.cfg.accepted_breakout
        accepted = self._accepted_range_context(d)
        close = self._optional_numeric(d, "bar.close")
        accepted_source = upper(accepted.get("source") or "")
        level_type = upper(observation.get("level_type") or "")
        level_source = upper(observation.get("source") or "")
        fixed_types = {upper(item) for item in cfg.major_level_reacceptance_level_types}

        active_dynamic_range = bool(
            accepted.get("breakout_eligible")
            and accepted_source == "INTRADAY_BALANCE"
            and self._as_float(accepted.get("high")) is not None
            and self._as_float(accepted.get("low")) is not None
        )
        close_inside_active_dynamic_range = bool(
            active_dynamic_range
            and close is not None
            and self._is_inside_accepted_range(close=float(close), accepted=accepted)
        )
        fixed_external_level = bool(
            level_type in fixed_types
            and level_source in {"ORB", "PREVIOUS_DAY"}
        )
        major_level_reacceptance = bool(
            close_inside_active_dynamic_range and fixed_external_level
        )

        return {
            "active_dynamic_range": active_dynamic_range,
            "active_range_id": accepted.get("range_id"),
            "active_range_version": accepted.get("version"),
            "active_range_source": accepted.get("source"),
            "active_range_low": accepted.get("low"),
            "active_range_high": accepted.get("high"),
            "close": close,
            "close_inside_active_dynamic_range": close_inside_active_dynamic_range,
            "fixed_external_level": fixed_external_level,
            "level_type": observation.get("level_type"),
            "level_source": observation.get("source"),
            "level_price": observation.get("price"),
            "major_level_reacceptance": major_level_reacceptance,
            "major_level_reacceptance_enabled": bool(cfg.major_level_reacceptance_enabled),
        }

    @staticmethod
    def _close_position(*, low: float, high: float, close: float) -> float:
        if high <= low:
            return 0.5
        return max(0.0, min(1.0, (close - low) / (high - low)))

    @staticmethod
    def _price_action_strength(*, single: bool, multi: bool, current_pos: float, side: str) -> float:
        cfg = EVIDENCE_CONFIG.price_action
        base = 0.0
        if single:
            base += cfg.single_candle_strength_points
        if multi:
            base += cfg.multi_candle_strength_points
        if side == BUY:
            base += max(0.0, min(cfg.close_position_strength_points, current_pos * cfg.close_position_strength_points))
        elif side == SELL:
            base += max(0.0, min(cfg.close_position_strength_points, (1.0 - current_pos) * cfg.close_position_strength_points))
        return round(max(0.0, min(100.0, base)), 2)

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _as_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None
