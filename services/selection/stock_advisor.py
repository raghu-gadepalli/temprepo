from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from schemas.snapshot import SnapshotSchema
from services.selection.stock_advisor_context import StockAdvisorContextBuilder, StockAdvisorDayContext
from services.selection.stock_advisor_features import extract_stock_advisor_features
from services.selection.stock_advisor_result import (
    ALLOW,
    BLOCK,
    BUY,
    SELL,
    WATCH,
    SETUP_TO_FAMILY,
    StockAdvisorFeatures,
    StockAdvisorResult,
    StockAdvisorSetupAlignment,
)

CFG = STOCK_ADVISOR_CONFIG

MEAN_REVERSION = CFG.mean_reversion_family
BREAKOUT = CFG.breakout_family
FAILED_BREAKOUT = CFG.failed_breakout_family


def _score(value: Optional[float], normalizer: float) -> float:
    if value is None or normalizer <= 0:
        return 0.0
    return max(0.0, min(abs(float(value)) / float(normalizer), 1.0))


def _edge_score(range_position: Optional[float]) -> float:
    if range_position is None:
        return 0.0
    pos = max(0.0, min(float(range_position), 1.0))
    return max(abs(pos - 0.5) * 2.0, 0.0)


def _extension_score(features: StockAdvisorFeatures) -> float:
    parts: List[float] = []
    if features.vwap_gap_pct is not None:
        parts.append(_score(features.vwap_gap_pct, CFG.extension_norm_pct))
    if features.bb_position is not None:
        parts.append(_edge_score(features.bb_position))
    if features.rsi is not None:
        if features.rsi >= 50:
            parts.append(_score(features.rsi - 50.0, 25.0))
        else:
            parts.append(_score(50.0 - features.rsi, 25.0))
    return max(parts) if parts else 0.0


def _contains_any(value: str, terms: List[str]) -> bool:
    hay = str(value or "").upper()
    return any(str(term or "").upper() in hay for term in (terms or []))


def _family_key(family: str, side: str) -> str:
    return f"{str(family or '').strip().upper()}_{str(side or '').strip().upper()}"


def _setup_key(setup: str, side: str) -> str:
    return f"{str(setup or '').strip().upper()}_{str(side or '').strip().upper()}"


def _opposite(side: str) -> str:
    return SELL if str(side or "").upper() == BUY else BUY


def _matches_side(value: str, side: str) -> bool:
    v = str(value or "").upper()
    side = str(side or "").upper()
    if side == BUY:
        return any(term in v for term in ("BUY", "BULL", "UP", "ABOVE", "LONG", "HIGH", "BREAKOUT"))
    if side == SELL:
        return any(term in v for term in ("SELL", "BEAR", "DOWN", "BELOW", "SHORT", "LOW", "BREAKDOWN"))
    return False


def _near_level_for_breakout_side(level_type: str, side: str) -> bool:
    level = str(level_type or "").upper()
    side = str(side or "").upper()
    if side == BUY:
        return any(x in level for x in ("HIGH", "PDH", "ORB_HIGH"))
    if side == SELL:
        return any(x in level for x in ("LOW", "PDL", "ORB_LOW"))
    return False


def _near_level_for_failed_breakout_side(level_type: str, side: str) -> bool:
    level = str(level_type or "").upper()
    side = str(side or "").upper()
    if side == SELL:
        return any(x in level for x in ("HIGH", "PDH", "ORB_HIGH"))
    if side == BUY:
        return any(x in level for x in ("LOW", "PDL", "ORB_LOW"))
    return False


def _status_alignment_rank(alignment: str) -> int:
    return {BLOCK: 0, WATCH: 1, ALLOW: 2}.get(str(alignment or "").upper(), 0)


def _downgrade(alignment: str) -> str:
    alignment = str(alignment or "").upper()
    if alignment == ALLOW:
        return WATCH
    if alignment == WATCH:
        return BLOCK
    return BLOCK


class StockAdvisor:
    """Stock/day context advisor.

    The advisor evaluates day-so-far stock behaviour and returns family-level
    ALLOW / WATCH / BLOCK decisions.  It does not confirm individual setups;
    setup confirmation remains in EvidenceEvaluator.
    """

    def __init__(self) -> None:
        self.context_builder = StockAdvisorContextBuilder(snapshot_limit=CFG.day_context_snapshot_limit)

    def analyze(
        self,
        snapshot: SnapshotSchema,
        recent_snapshots: Optional[Iterable[SnapshotSchema]] = None,
        candidate_context: Optional[Dict[str, Any]] = None,
    ) -> StockAdvisorResult:
        features = extract_stock_advisor_features(
            snapshot,
            recent_snapshots=recent_snapshots,
            candidate_context=candidate_context,
        )
        context = self.context_builder.build(snapshot, recent_snapshots=recent_snapshots if recent_snapshots is not None else None)
        score = self._tradeability_score(features)

        stock_decision, stock_context, reason_code, reason_text = self._stock_decision(features, context)
        family_alignment = self._family_alignment(features, context, stock_decision, reason_code, reason_text)
        setup_alignment, side_setup_alignment = self._mapped_setup_alignment(family_alignment)
        decision, regime, best_code, best_text = self._decision_from_alignment(score, stock_decision, stock_context, family_alignment, reason_code, reason_text)

        eligible = [k for k, a in family_alignment.items() if a.alignment == ALLOW]
        watch = [k for k, a in family_alignment.items() if a.alignment == WATCH]
        blocked = [k for k, a in family_alignment.items() if a.alignment == BLOCK]

        features_dict = features.to_dict()
        if isinstance(candidate_context, dict) and candidate_context:
            features_dict["candidate_context"] = {
                key: candidate_context.get(key)
                for key in (
                    "setup_label",
                    "side",
                    "breakout_status",
                    "acceptance_path",
                    "reference_id",
                    "level_type",
                    "level_source",
                    "level_price",
                    "entry_distance_from_level_atr",
                    "price_action_confirmed",
                    "entry_ready",
                )
                if key in candidate_context
            }
        features_dict.update({f"day_context_{k}": v for k, v in context.to_dict().items()})

        return StockAdvisorResult(
            symbol=features.symbol,
            snapshot_time=features.snapshot_time,
            decision=decision,
            regime=regime,
            tradeability_score=round(score, 2),
            family_alignment=family_alignment,
            stock_context=stock_context,
            volatility_context=context.atr_context,
            vwap_context=context.vwap_context,
            trend_context=context.trend_context,
            range_context=context.range_context,
            chop_context=context.chop_context,
            attempt_context=context.attempt_context,
            preferred_direction=context.preferred_direction,
            avoid_direction=context.avoid_direction,
            eligible_setups=eligible,
            watch_setups=watch,
            blocked_setups=blocked,
            setup_alignment=setup_alignment,
            side_setup_alignment=side_setup_alignment,
            reason_code=best_code,
            reason_text=best_text,
            reason_codes=list(dict.fromkeys([reason_code] + (context.reason_codes or []) + [a.reason_code for a in family_alignment.values() if a.reason_code])),
            features=features_dict,
        )

    def _tradeability_score(self, f: StockAdvisorFeatures) -> float:
        day = _score(f.day_range_pct, CFG.day_range_norm_pct) * CFG.w_day_range
        recent = _score(f.recent_range_pct, CFG.recent_range_norm_pct) * CFG.w_recent_range
        edge = _edge_score(f.range_position) * CFG.w_edge_location
        extension = _extension_score(f) * CFG.w_extension
        volume = _score(f.volume_ratio, CFG.volume_ratio_norm) * CFG.w_volume
        return round((day + recent + edge + extension + volume) * 100.0, 2)

    def _stock_decision(self, f: StockAdvisorFeatures, c: StockAdvisorDayContext) -> Tuple[str, str, str, str]:
        if not f.close or not f.snapshot_time:
            return BLOCK, "STALE_OR_BAD_SNAPSHOT", "missing_close_or_time", "Snapshot does not have usable close/time."

        day_range = f.day_range_pct or 0.0
        recent_range = f.recent_range_pct or 0.0
        range_pos = f.range_position
        vwap_gap = abs(f.vwap_gap_pct or 0.0)

        if day_range < CFG.min_day_range_pct and recent_range < CFG.min_recent_range_pct:
            return BLOCK, "LOW_MOVEMENT", "low_day_and_recent_range", "Day range and recent range are too small for useful evaluation."

        if c.chop_context == "HIGH_CONTEXT_FLIP_CHOP":
            return WATCH, "HIGH_CONTEXT_FLIP_CHOP", "high_context_flip_chop_watch", "Day-so-far state/VWAP/structure flips are frequent; keep setup families on watch instead of globally blocking."

        if c.vwap_context == "VWAP_CHOP_REPEATED_CROSSES":
            return WATCH, "VWAP_CHOP", "vwap_chop_repeated_crosses_watch", "Price has repeatedly crossed VWAP; require family/direction context before allowing."

        if c.attempt_context == "MULTIPLE_FAILED_ATTEMPTS":
            return WATCH, "MULTIPLE_FAILED_ATTEMPTS", "multiple_failed_attempts_watch", "The stock has multiple failed/no-follow-through attempts today; downgrade family allows to watch."

        if range_pos is not None and CFG.middle_zone_low <= range_pos <= CFG.middle_zone_high:
            if recent_range < CFG.min_recent_range_pct and day_range < CFG.min_day_range_pct_for_evaluate:
                return BLOCK, "RANGE_MIDDLE_NO_EDGE", "middle_range_no_edge", "Price is in the middle of the day range with insufficient recent movement."

        if f.vwap_gap_pct is not None and vwap_gap <= CFG.vwap_chop_max_gap_pct:
            if recent_range <= CFG.vwap_chop_max_recent_range_pct and range_pos is not None and CFG.middle_zone_low <= range_pos <= CFG.middle_zone_high:
                return BLOCK, "VWAP_CHOP", "vwap_pinned_chop", "Price is near VWAP, mid-range, and recent range is small."

        if c.range_context in {"POST_SPIKE_COMPRESSION", "RANGE_EXPANSION_STALLED"}:
            return WATCH, c.range_context, c.range_context.lower(), "Day-so-far range expansion has stalled or compressed after an earlier move."

        if c.chop_context == "MODERATE_CONTEXT_FLIP_CHOP" or c.attempt_context == "PRIOR_ATTEMPT_WEAK_FOLLOW_THROUGH":
            return WATCH, "DEVELOPING_EDGE", "mixed_context_watch", "Stock is active but day-so-far context is mixed or follow-through is weak."

        return ALLOW, "ACTIVE_WITH_EDGE", "active_with_edge", "Stock has enough range/context to evaluate setup families."

    def _family_alignment(
        self,
        f: StockAdvisorFeatures,
        c: StockAdvisorDayContext,
        stock_decision: str,
        stock_reason_code: str,
        stock_reason_text: str,
    ) -> Dict[str, StockAdvisorSetupAlignment]:
        if stock_decision == BLOCK:
            return {
                _family_key(MEAN_REVERSION, BUY): StockAdvisorSetupAlignment(MEAN_REVERSION, BLOCK, BUY, 0.0, stock_reason_code, stock_reason_text),
                _family_key(MEAN_REVERSION, SELL): StockAdvisorSetupAlignment(MEAN_REVERSION, BLOCK, SELL, 0.0, stock_reason_code, stock_reason_text),
                _family_key(BREAKOUT, BUY): StockAdvisorSetupAlignment(BREAKOUT, BLOCK, BUY, 0.0, stock_reason_code, stock_reason_text),
                _family_key(BREAKOUT, SELL): StockAdvisorSetupAlignment(BREAKOUT, BLOCK, SELL, 0.0, stock_reason_code, stock_reason_text),
                _family_key(FAILED_BREAKOUT, BUY): StockAdvisorSetupAlignment(FAILED_BREAKOUT, BLOCK, BUY, 0.0, stock_reason_code, stock_reason_text),
                _family_key(FAILED_BREAKOUT, SELL): StockAdvisorSetupAlignment(FAILED_BREAKOUT, BLOCK, SELL, 0.0, stock_reason_code, stock_reason_text),
            }

        alignments: Dict[str, StockAdvisorSetupAlignment] = {}
        for side in (BUY, SELL):
            alignments[_family_key(BREAKOUT, side)] = self._breakout_family_alignment(f, c, side)
            alignments[_family_key(MEAN_REVERSION, side)] = self._mean_reversion_family_alignment(f, c, side)
            alignments[_family_key(FAILED_BREAKOUT, side)] = self._failed_breakout_family_alignment(f, c, side)

        if stock_decision == WATCH:
            # Stock-level WATCH means families may remain WATCH but not ALLOW.
            adjusted: Dict[str, StockAdvisorSetupAlignment] = {}
            for key, a in alignments.items():
                if a.alignment == ALLOW:
                    adjusted[key] = StockAdvisorSetupAlignment(a.setup, WATCH, a.side, a.score, f"stock_watch_{a.reason_code}", f"Stock-level context is WATCH: {a.reason_text}")
                else:
                    adjusted[key] = a
            return adjusted
        return alignments

    def _breakout_family_alignment(self, f: StockAdvisorFeatures, c: StockAdvisorDayContext, side: str) -> StockAdvisorSetupAlignment:
        side = str(side or "").upper()
        preferred = c.preferred_direction == side
        avoided = c.avoid_direction == side
        near_level = f.nearest_level_distance_atr is not None and f.nearest_level_distance_atr <= CFG.level_proximity_atr
        side_level = near_level and _near_level_for_breakout_side(f.nearest_level_type, side)
        status_side = _matches_side(f.breakout_side, side)
        accepted_status = _contains_any(f.breakout_status, ["ACCEPT", "PULLBACK_HOLD", "SUSTAIN"])
        watch_status = _contains_any(f.breakout_status, CFG.breakout_watch_status_terms)

        score = 0.0
        if preferred:
            score += 35.0
        if side_level:
            score += 25.0
        elif near_level:
            score += 10.0
        if accepted_status and (status_side or side_level):
            score += 30.0
        elif watch_status and (status_side or side_level):
            score += 15.0
        if c.atr_context in {"ATR_EXPANDING", "ATR_STABLE"} and c.range_context == "RANGE_EXPANSION_CONTINUING":
            score += 10.0
        score = min(100.0, score)

        choppy_vwap = c.vwap_context in {"VWAP_CHOP_REPEATED_CROSSES", "VWAP_MIXED"}
        has_directional_context = accepted_status and (preferred or side_level or status_side)
        has_developing_context = preferred or side_level or watch_status

        if avoided:
            return StockAdvisorSetupAlignment(BREAKOUT, BLOCK, side, score, f"breakout_{side.lower()}_context_block", "Breakout side is against the preferred day direction.")
        if choppy_vwap and not (has_directional_context or has_developing_context):
            return StockAdvisorSetupAlignment(BREAKOUT, BLOCK, side, score, f"breakout_{side.lower()}_vwap_chop_no_context", "VWAP behaviour is mixed/choppy and there is no clean direction/level breakout context.")
        if choppy_vwap and has_developing_context:
            return StockAdvisorSetupAlignment(BREAKOUT, WATCH, side, score, f"breakout_{side.lower()}_vwap_chop_watch", "Breakout context exists, but VWAP behaviour is mixed/choppy; keep as watch.")
        if accepted_status and (preferred or side_level or status_side):
            return StockAdvisorSetupAlignment(BREAKOUT, ALLOW, side, score, f"breakout_{side.lower()}_context_allow", "Day-so-far trend/VWAP/level context supports breakout continuation.")
        if preferred or side_level or watch_status:
            return StockAdvisorSetupAlignment(BREAKOUT, WATCH, side, score, f"breakout_{side.lower()}_context_watch", "Breakout context is developing but not clean enough for stock-level allow.")
        return StockAdvisorSetupAlignment(BREAKOUT, BLOCK, side, score, f"breakout_{side.lower()}_context_not_relevant", "No day-so-far breakout context supports this direction.")

    def _mean_reversion_family_alignment(self, f: StockAdvisorFeatures, c: StockAdvisorDayContext, side: str) -> StockAdvisorSetupAlignment:
        side = str(side or "").upper()
        upper_ext = self._upper_extension_markers(f)
        lower_ext = self._lower_extension_markers(f)
        markers = lower_ext if side == BUY else upper_ext
        counter_to_persistent = (side == SELL and c.trend_context == "PERSISTENT_UPTREND") or (side == BUY and c.trend_context == "PERSISTENT_DOWNTREND")
        wrong_side = (side == BUY and upper_ext >= 2) or (side == SELL and lower_ext >= 2)
        score = min(100.0, markers * 22.0 + _extension_score(f) * 30.0 + _edge_score(f.range_position) * 20.0)

        if wrong_side:
            return StockAdvisorSetupAlignment(MEAN_REVERSION, BLOCK, side, score, f"mean_reversion_{side.lower()}_wrong_extension_side", "Mean-reversion direction is opposite to the current extension side.")
        if c.attempt_context == "MULTIPLE_FAILED_ATTEMPTS" and markers >= 1:
            return StockAdvisorSetupAlignment(MEAN_REVERSION, WATCH, side, score, f"mean_reversion_{side.lower()}_attempt_risk_watch", "Mean-reversion extension is present, but prior failed attempts require watch.")
        if markers >= 3:
            if counter_to_persistent and c.atr_context in {"ATR_EXPANDING", "ATR_STABLE"} and c.range_context == "RANGE_EXPANSION_CONTINUING":
                return StockAdvisorSetupAlignment(MEAN_REVERSION, WATCH, side, score, f"mean_reversion_{side.lower()}_countertrend_watch", "Extension exists, but persistent trend/range expansion has not weakened enough.")
            return StockAdvisorSetupAlignment(MEAN_REVERSION, ALLOW, side, score, f"mean_reversion_{side.lower()}_context_allow", "Day-so-far extension context supports mean reversion for this side.")
        if markers >= 1:
            return StockAdvisorSetupAlignment(MEAN_REVERSION, WATCH, side, score, f"mean_reversion_{side.lower()}_context_watch", "Some extension markers are present, but the broader day context is not clean.")
        return StockAdvisorSetupAlignment(MEAN_REVERSION, BLOCK, side, score, f"mean_reversion_{side.lower()}_context_not_relevant", "No day-so-far extension context supports mean reversion for this side.")

    def _failed_breakout_family_alignment(self, f: StockAdvisorFeatures, c: StockAdvisorDayContext, side: str) -> StockAdvisorSetupAlignment:
        side = str(side or "").upper()
        near_level = f.nearest_level_distance_atr is not None and f.nearest_level_distance_atr <= CFG.level_proximity_atr
        side_level = near_level and _near_level_for_failed_breakout_side(f.nearest_level_type, side)
        status_failed = _contains_any(f.breakout_status, CFG.failed_breakout_allow_status_terms)
        status_watch = _contains_any(f.breakout_status, CFG.breakout_watch_status_terms)
        failed_side_supported = _matches_side(f.breakout_side, _opposite(side)) or side_level
        preferred = c.preferred_direction == side
        against_persistent = (side == SELL and c.trend_context == "PERSISTENT_UPTREND") or (side == BUY and c.trend_context == "PERSISTENT_DOWNTREND")

        score = 0.0
        if side_level:
            score += 35.0
        elif near_level:
            score += 15.0
        if status_failed:
            score += 35.0
        elif status_watch:
            score += 15.0
        if failed_side_supported:
            score += 20.0
        if preferred:
            score += 10.0
        score = min(100.0, score)

        if status_failed and failed_side_supported and near_level and not against_persistent:
            return StockAdvisorSetupAlignment(FAILED_BREAKOUT, ALLOW, side, score, f"failed_breakout_{side.lower()}_context_allow", "Day-so-far failed/reabsorbed level context supports this direction.")
        if (status_failed or status_watch or side_level) and failed_side_supported:
            return StockAdvisorSetupAlignment(FAILED_BREAKOUT, WATCH, side, score, f"failed_breakout_{side.lower()}_context_watch", "Failed-breakout context is developing but not clean enough for stock-level allow.")
        return StockAdvisorSetupAlignment(FAILED_BREAKOUT, BLOCK, side, score, f"failed_breakout_{side.lower()}_context_not_relevant", "No day-so-far failed-breakout context supports this direction.")

    def _upper_extension_markers(self, f: StockAdvisorFeatures) -> int:
        markers = 0
        if f.range_position is not None and f.range_position >= CFG.edge_zone_high:
            markers += 1
        if f.bb_position is not None and f.bb_position >= CFG.upper_bb_position_for_extension:
            markers += 1
        if f.rsi is not None and f.rsi >= CFG.sell_rsi_for_extension:
            markers += 1
        if abs(f.vwap_gap_pct or 0.0) >= CFG.min_vwap_gap_pct_for_extension and markers > 0:
            markers += 1
        return markers

    def _lower_extension_markers(self, f: StockAdvisorFeatures) -> int:
        markers = 0
        if f.range_position is not None and f.range_position <= CFG.edge_zone_low:
            markers += 1
        if f.bb_position is not None and f.bb_position <= CFG.lower_bb_position_for_extension:
            markers += 1
        if f.rsi is not None and f.rsi <= CFG.buy_rsi_for_extension:
            markers += 1
        if abs(f.vwap_gap_pct or 0.0) >= CFG.min_vwap_gap_pct_for_extension and markers > 0:
            markers += 1
        return markers

    def _mapped_setup_alignment(self, family_alignment: Dict[str, StockAdvisorSetupAlignment]) -> Tuple[Dict[str, StockAdvisorSetupAlignment], Dict[str, StockAdvisorSetupAlignment]]:
        setup_alignment: Dict[str, StockAdvisorSetupAlignment] = {}
        side_setup_alignment: Dict[str, StockAdvisorSetupAlignment] = {}
        for setup, family in SETUP_TO_FAMILY.items():
            buy = family_alignment.get(_family_key(family, BUY))
            sell = family_alignment.get(_family_key(family, SELL))
            side_items = [x for x in (buy, sell) if x is not None]
            best = self._best_alignment(side_items, setup)
            setup_alignment[setup] = StockAdvisorSetupAlignment(setup, best.alignment, "ANY", best.score, best.reason_code, best.reason_text)
            if buy is not None:
                side_setup_alignment[_setup_key(setup, BUY)] = StockAdvisorSetupAlignment(setup, buy.alignment, BUY, buy.score, buy.reason_code, buy.reason_text)
            if sell is not None:
                side_setup_alignment[_setup_key(setup, SELL)] = StockAdvisorSetupAlignment(setup, sell.alignment, SELL, sell.score, sell.reason_code, sell.reason_text)
        return setup_alignment, side_setup_alignment

    @staticmethod
    def _best_alignment(items: List[StockAdvisorSetupAlignment], setup: str) -> StockAdvisorSetupAlignment:
        if not items:
            return StockAdvisorSetupAlignment(setup, BLOCK, "ANY", 0.0, "missing_alignment", "No alignment was computed.")
        items = sorted(items, key=lambda x: (_status_alignment_rank(x.alignment), x.score), reverse=True)
        return items[0]

    def _decision_from_alignment(
        self,
        stock_score: float,
        stock_decision: str,
        stock_context: str,
        alignment: Dict[str, StockAdvisorSetupAlignment],
        reason_code: str,
        reason_text: str,
    ) -> Tuple[str, str, str, str]:
        if stock_decision == BLOCK:
            return "SKIP", stock_context, reason_code, reason_text
        allowed = [a for a in alignment.values() if a.alignment == ALLOW]
        watched = [a for a in alignment.values() if a.alignment == WATCH]
        if allowed:
            best = max(allowed, key=lambda x: x.score)
            return "EVALUATE", self._regime_for_family(best.setup, allow=True), best.reason_code, best.reason_text
        if watched or stock_decision == WATCH:
            best = max(watched, key=lambda x: x.score) if watched else None
            return "WATCH", self._regime_for_family(best.setup, allow=False) if best else stock_context, best.reason_code if best else reason_code, best.reason_text if best else reason_text
        if stock_score >= CFG.family_watch_score:
            return "WATCH", "MOVING_BUT_NO_FAMILY_ALIGNMENT", "moving_without_family_alignment", "Stock has movement/context, but no family/direction is clearly aligned."
        return "SKIP", "NO_TRADEABLE_EDGE", "no_tradeable_edge", "No tradeable movement, edge, or family alignment is visible yet."

    @staticmethod
    def _regime_for_family(family: str, *, allow: bool) -> str:
        family = str(family or "").upper()
        if family == MEAN_REVERSION:
            return "MEAN_REVERSION_CONTEXT" if allow else "MEAN_REVERSION_WATCH"
        if family == BREAKOUT:
            return "BREAKOUT_CONTEXT" if allow else "BREAKOUT_WATCH"
        if family == FAILED_BREAKOUT:
            return "FAILED_BREAKOUT_CONTEXT" if allow else "FAILED_BREAKOUT_WATCH"
        return "STOCK_CONTEXT"
