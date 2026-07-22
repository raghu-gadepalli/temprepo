from __future__ import annotations

from typing import Any, Dict, List

from configs.evidence_config import EVIDENCE_CONFIG
from enums.enums import LifecycleQuality, LifecycleSide, LifecycleStage, SignalAction, SetupState, SetupType
from schemas.lifecycle import LifecycleResult
from services.evidence.evidence_result import EvidenceResult
from services.evidence.evidence_score_helper import BUY, SELL, SIDES, opposite_side, upper
from utils.datetime_utils import to_ist_naive


class EvidenceLifecycleAdapter:
    """Convert EvidenceResult into the existing signal persistence contract."""

    def __init__(self) -> None:
        self.cfg = EVIDENCE_CONFIG

    def adapt(self, evidence: EvidenceResult, existing_context: Dict[str, Any]) -> LifecycleResult:
        active_side = upper(existing_context.get("open_signal_side"))
        active_stage = upper(existing_context.get("open_signal_stage"))
        has_active_signal = active_side in SIDES

        if has_active_signal:
            return self._adapt_existing_signal(evidence, existing_context=existing_context, active_side=active_side, active_stage=active_stage)
        return self._adapt_fresh_signal(evidence)

    def _adapt_fresh_signal(self, evidence: EvidenceResult) -> LifecycleResult:
        action_cfg = self.cfg.action
        side = evidence.preferred_side
        if side in SIDES and evidence.entry_permission == "ALLOW":
            result = self._base_result(evidence, side=side, stage=LifecycleStage.ACTIVE)
            result.set_signal(SignalAction.CREATE, "READY", evidence.reason.code)
            result.add_support(evidence.reason.code, evidence.reason.text, 1.0, evidence.reason.data)
            return result

        if side in SIDES and evidence.entry_permission == "DEFER":
            result = self._base_result(evidence, side=side, stage=LifecycleStage.DISCOVERY)
            result.set_signal(SignalAction.WATCH, "DEFER", evidence.reason.code)
            result.add_warning(evidence.reason.code, evidence.reason.text, 1.0, evidence.reason.data)
            return result

        if side in SIDES and evidence.preferred_opportunity_score >= action_cfg.watch_min_opportunity:
            result = self._base_result(evidence, side=side, stage=LifecycleStage.DISCOVERY)
            result.set_signal(SignalAction.WATCH, "WATCH", evidence.reason.code)
            result.add_warning(evidence.reason.code, evidence.reason.text, 1.0, evidence.reason.data)
            return result

        result = self._base_result(evidence, side="NONE", stage=LifecycleStage.TRANSITION)
        result.set_signal(SignalAction.WATCH, "WATCH", evidence.reason.code)
        result.add_warning(evidence.reason.code, evidence.reason.text, 1.0, evidence.reason.data)
        return result

    def _adapt_existing_signal(self, evidence: EvidenceResult, *, existing_context: Dict[str, Any], active_side: str, active_stage: str) -> LifecycleResult:
        action_cfg = self.cfg.action
        same = evidence.side_evidence(active_side)
        opp_side = opposite_side(active_side)
        opposite = evidence.side_evidence(opp_side)
        score_gap = opposite.opportunity_score - same.opportunity_score

        setup_exit = self._setup_invalidation_reason(evidence=evidence, existing_context=existing_context, active_side=active_side)
        if setup_exit:
            result = self._base_result(evidence, side=active_side, stage=LifecycleStage.FORCE_EXIT)
            result.set_signal(SignalAction.INVALIDATE, "CLOSED", setup_exit["code"])
            result.add_conflict(setup_exit["code"], setup_exit["text"], 1.0, setup_exit)
            result.meta["evidence_reason"] = setup_exit
            return result

        active_evidence_exit = self._active_evidence_exit_reason(evidence=evidence, active_side=active_side)
        if active_evidence_exit:
            result = self._base_result(evidence, side=active_side, stage=LifecycleStage.FORCE_EXIT)
            result.set_signal(SignalAction.INVALIDATE, "CLOSED", active_evidence_exit["code"])
            result.add_conflict(active_evidence_exit["code"], active_evidence_exit["text"], 1.0, active_evidence_exit)
            result.meta["evidence_reason"] = active_evidence_exit
            return result

        price_action_failure = self._active_price_action_failure_reason(evidence=evidence, existing_context=existing_context, active_side=active_side)
        if price_action_failure:
            result = self._base_result(evidence, side=active_side, stage=LifecycleStage.WEAKENING)
            result.set_signal(SignalAction.DOWNGRADE, "MANAGE", price_action_failure["code"])
            result.add_warning(price_action_failure["code"], price_action_failure["text"], 1.0, price_action_failure)
            result.meta["evidence_reason"] = price_action_failure
            return result

        if (
            action_cfg.enable_opposite_auto_replace
            and opposite.opportunity_score >= action_cfg.replace_min_opportunity
            and opposite.entry_risk <= action_cfg.replace_max_entry_risk
            and score_gap >= action_cfg.replace_min_score_gap
        ):
            reason = self._opposite_reason(
                code=self.cfg.reason.replace_code,
                active_side=active_side,
                opposite_side=opp_side,
                opposite_score=opposite.opportunity_score,
                opposite_risk=opposite.entry_risk,
                score_gap=score_gap,
            )
            result = self._base_result(evidence, side=opp_side, stage=LifecycleStage.ACTIVE)
            result.set_signal(SignalAction.INVALIDATE_OPPOSITE, "READY", reason["code"])
            result.add_conflict(reason["code"], reason["text"], 1.0, reason)
            result.meta["evidence_reason"] = reason
            return result

        if same.continuation_quality <= action_cfg.invalidate_below_continuation_quality:
            reason = self._same_side_reason(
                code=self.cfg.reason.invalidate_code,
                side=active_side,
                quality=same.continuation_quality,
                risk=same.entry_risk,
                message_action="downgrades",
            )
            reason["signal_exit_policy"] = "NO_SIGNAL_EXIT_ON_CONTINUATION_QUALITY"
            reason["trade_manager_owns_risk_exit"] = True
            reason["note"] = (
                "Continuation quality weakness no longer closes the signal. "
                "The signal remains open/weakening until setup invalidation or opposite setup evidence; "
                "SL/target/adverse-continuation exits belong to trade management."
            )
            result = self._base_result(evidence, side=active_side, stage=LifecycleStage.WEAKENING)
            result.set_signal(SignalAction.DOWNGRADE, "MANAGE", reason["code"])
            result.add_warning(reason["code"], reason["text"], 1.0, reason)
            result.meta["evidence_reason"] = reason
            return result

        if (
            same.continuation_quality <= action_cfg.downgrade_below_continuation_quality
            or opposite.opportunity_score >= action_cfg.downgrade_opposite_pressure_min
        ):
            reason = self._downgrade_reason(
                active_side=active_side,
                continuation_quality=same.continuation_quality,
                opposite_side=opp_side,
                opposite_score=opposite.opportunity_score,
            )
            result = self._base_result(evidence, side=active_side, stage=LifecycleStage.WEAKENING)
            result.set_signal(SignalAction.DOWNGRADE, "MANAGE", reason["code"])
            result.add_warning(reason["code"], reason["text"], 1.0, reason)
            result.meta["evidence_reason"] = reason
            return result

        reason = self._same_side_reason(
            code=self.cfg.reason.hold_code,
            side=active_side,
            quality=same.continuation_quality,
            risk=same.entry_risk,
            message_action="keeps active",
        )
        stage = LifecycleStage.ACTIVE if active_stage != "WEAKENING" else LifecycleStage.WEAKENING
        result = self._base_result(evidence, side=active_side, stage=stage)
        result.set_signal(SignalAction.HOLD, "ACTIVE", reason["code"])
        result.add_support(reason["code"], reason["text"], 1.0, reason)
        result.meta["evidence_reason"] = reason
        return result

    def _active_evidence_exit_reason(self, *, evidence: EvidenceResult, active_side: str) -> Dict[str, Any] | None:
        """Exit active signal when the normalized setup layer confirms opposite evidence.

        This enforces the no same-pass reversal policy: the current active signal
        is closed first.  A new opposite signal can be created only on a later
        evaluation pass if no active signal exists at pass start and the setup
        remains valid.
        """
        details = evidence.details if isinstance(evidence.details, dict) else {}
        setup_decision = details.get("setup_decision") if isinstance(details.get("setup_decision"), dict) else {}
        active_ev = setup_decision.get("active_signal_evidence") if isinstance(setup_decision.get("active_signal_evidence"), dict) else {}
        action = upper(active_ev.get("active_evidence_action") or active_ev.get("evidence_action"))
        should_exit = bool(active_ev.get("should_exit_signal")) or action == "EXIT"
        if not should_exit:
            return None

        top_opp = active_ev.get("top_opposite_candidate") if isinstance(active_ev.get("top_opposite_candidate"), dict) else {}
        if (
            upper(top_opp.get("setup_label")) == upper(self.cfg.pattern.setup_exhaustion_reversal)
            and not self._exhaustion_candidate_is_fresh(evidence=evidence, candidate=top_opp)
        ):
            return None
        reason_code = str(active_ev.get("reason_code") or "OPPOSITE_SETUP_CONFIRMED").upper().strip()
        setup_label = upper(top_opp.get("setup_label")) or "OPPOSITE_SETUP"
        opp_side = upper(top_opp.get("side"))
        text = (
            f"{active_side} active signal exits because opposite {opp_side or 'side'} "
            f"{setup_label} evidence is confirmed/entry-ready. Same-pass reversal is disabled; "
            f"a new opposite signal may be created only on the next pass if still valid."
        )
        return {
            "code": reason_code,
            "text": text,
            "active_side": active_side,
            "opposite_side": opp_side,
            "opposite_setup_label": setup_label,
            "active_signal_evidence": active_ev,
            "same_pass_reversal_allowed": False,
            "next_pass_create_policy": "ALLOW_ONLY_IF_NO_ACTIVE_SIGNAL_AT_PASS_START",
        }

    def _setup_invalidation_reason(self, *, evidence: EvidenceResult, existing_context: Dict[str, Any], active_side: str) -> Dict[str, Any] | None:
        setup_levels = existing_context.get("open_signal_setup_levels")
        if not isinstance(setup_levels, dict):
            return None

        setup_label = upper(setup_levels.get("setup_label"))
        if not setup_label:
            return None

        level_side = upper(setup_levels.get("side"))
        if level_side in SIDES and level_side != active_side:
            return None

        policy = self._signal_invalidation_policy(setup_label)
        if policy is None:
            return None

        reference = self._as_float(
            setup_levels.get("signal_invalidation_reference_price")
            or setup_levels.get("reference_price")
            or setup_levels.get("initial_stop_reference_price")
        )
        invalidation_side = upper(setup_levels.get("invalidation_side") or setup_levels.get("initial_stop_side"))
        current_atr = self._current_atr(evidence)
        frozen_event_atr = self._as_float(setup_levels.get("event_atr"))
        use_frozen_event_atr = bool(
            setup_label == upper(self.cfg.pattern.setup_accepted_breakout)
            and frozen_event_atr is not None
            and frozen_event_atr > 0
        )
        atr = frozen_event_atr if use_frozen_event_atr else current_atr
        closes = self._recent_closes(evidence)
        if reference is None or atr is None or atr <= 0 or invalidation_side not in {"ABOVE", "BELOW"} or not closes:
            return None

        buffer_atr = max(0.0, float(getattr(policy, "buffer_atr", 0.0) or 0.0))
        strong_atr = max(buffer_atr, float(getattr(policy, "strong_single_close_atr", buffer_atr) or buffer_atr))
        required = max(1, int(getattr(policy, "required_consecutive_closes", 1) or 1))
        buffer_points = buffer_atr * atr
        strong_points = strong_atr * atr
        buffered_level = reference + buffer_points if invalidation_side == "ABOVE" else reference - buffer_points

        def breached(value: float, threshold: float) -> bool:
            return value >= threshold if invalidation_side == "ABOVE" else value <= threshold

        consecutive = 0
        for value in reversed(closes):
            if breached(value, buffered_level):
                consecutive += 1
            else:
                break

        current_close = closes[-1]
        strong_level = reference + strong_points if invalidation_side == "ABOVE" else reference - strong_points
        strong_single = breached(current_close, strong_level)
        invalidated = consecutive >= required or strong_single
        if not invalidated:
            return None

        reference_source = str(
            setup_levels.get("signal_invalidation_reference_source")
            or setup_levels.get("reference_source")
            or setup_levels.get("initial_stop_reference_source")
            or "setup_reference"
        )
        mode = str(getattr(policy, "mode", "BUFFERED_REACCEPTANCE") or "BUFFERED_REACCEPTANCE")
        if setup_label == self.cfg.pattern.setup_exhaustion_reversal:
            code = self.cfg.reason.reversal_invalidation_code
            text = (
                f"{active_side} exhaustion reversal invalidated: close {current_close:.2f} "
                f"breached the buffered adverse extreme {buffered_level:.2f} "
                f"({invalidation_side}, raw={reference:.2f}, source={reference_source})."
            )
        else:
            code = f"{setup_label.lower()}_invalidation_exit"
            text = (
                f"{active_side} {setup_label} invalidated by {mode}: close {current_close:.2f}, "
                f"raw level {reference:.2f}, buffered level {buffered_level:.2f}, "
                f"consecutive confirming closes={consecutive}/{required}."
            )

        return {
            "code": code,
            "text": text,
            "active_side": active_side,
            "setup_label": setup_label,
            "close": current_close,
            "recent_closes": closes,
            "setup_reference_price": reference,
            "signal_invalidation_reference_price": reference,
            "signal_invalidation_reference_source": reference_source,
            "signal_invalidation_reference_policy": mode,
            "signal_invalidation_buffer_atr": buffer_atr,
            "signal_invalidation_atr_value": atr,
            "signal_invalidation_atr_source": "FROZEN_EVENT_ATR" if use_frozen_event_atr else "CURRENT_ATR",
            "signal_invalidation_current_atr": current_atr,
            "signal_invalidation_event_atr": frozen_event_atr,
            "signal_invalidation_buffer_points": buffer_points,
            "signal_invalidation_buffered_level": buffered_level,
            "required_consecutive_closes": required,
            "consecutive_breaching_closes": consecutive,
            "strong_single_close_atr": strong_atr,
            "strong_single_close": strong_single,
            "invalidation_side": invalidation_side,
            "setup_levels": setup_levels,
        }

    def _signal_invalidation_policy(self, setup_label: str) -> Any:
        cfg = getattr(self.cfg, "signal_invalidation", None)
        if cfg is None:
            return None
        setup_u = upper(setup_label)
        if setup_u == upper(self.cfg.pattern.setup_accepted_breakout):
            return getattr(cfg, "accepted_breakout", None)
        if setup_u == upper(self.cfg.pattern.setup_failed_breakout):
            return getattr(cfg, "failed_breakout", None)
        if setup_u == upper(self.cfg.pattern.setup_exhaustion_reversal):
            return getattr(cfg, "exhaustion_reversal", None)
        return None

    def _recent_closes(self, evidence: EvidenceResult) -> List[float]:
        details = evidence.details if isinstance(evidence.details, dict) else {}
        snap = details.get("snapshot") if isinstance(details.get("snapshot"), dict) else {}
        structure = snap.get("structure") if isinstance(snap.get("structure"), dict) else {}
        raw = structure.get("recent_closes") if isinstance(structure.get("recent_closes"), list) else []
        values: List[float] = []
        for item in raw:
            value = self._as_float(item.get("close") if isinstance(item, dict) else item)
            if value is not None:
                values.append(value)
        current = self._as_float(snap.get("close"))
        if current is None:
            current = self._as_float((snap.get("bar") or {}).get("close") if isinstance(snap.get("bar"), dict) else None)
        if current is not None and (not values or abs(values[-1] - current) > 1e-12):
            values.append(current)
        return values

    def _current_atr(self, evidence: EvidenceResult) -> float | None:
        details = evidence.details if isinstance(evidence.details, dict) else {}
        snap = details.get("snapshot") if isinstance(details.get("snapshot"), dict) else {}
        indicators = snap.get("indicators") if isinstance(snap.get("indicators"), dict) else {}
        atr_block = indicators.get("atr") if isinstance(indicators.get("atr"), dict) else {}
        return self._as_float(atr_block.get("value"))

    def _exhaustion_candidate_is_fresh(self, *, evidence: EvidenceResult, candidate: Dict[str, Any]) -> bool:
        details = evidence.details if isinstance(evidence.details, dict) else {}
        snap = details.get("snapshot") if isinstance(details.get("snapshot"), dict) else {}
        now = to_ist_naive(snap.get("snapshot_time") or evidence.snapshot_time)
        event_time = to_ist_naive(
            candidate.get("event_time")
            or candidate.get("watch_snapshot_time")
            or ((candidate.get("setup_levels") or {}).get("watch_snapshot_time") if isinstance(candidate.get("setup_levels"), dict) else None)
        )
        if now is None or event_time is None:
            return False
        age_minutes = (now - event_time).total_seconds() / 60.0
        valid_minutes = float(getattr(self.cfg.exhaustion_reversal, "watch_extreme_valid_minutes", 15.5) or 15.5)
        return 0.0 <= age_minutes <= valid_minutes

    def _active_price_action_failure_reason(self, *, evidence: EvidenceResult, existing_context: Dict[str, Any], active_side: str) -> Dict[str, Any] | None:
        setup_levels = existing_context.get("open_signal_setup_levels")
        if not isinstance(setup_levels, dict):
            return None
        if upper(setup_levels.get("setup_label")) != self.cfg.pattern.setup_exhaustion_reversal:
            return None
        level_side = upper(setup_levels.get("side"))
        if level_side in SIDES and level_side != active_side:
            return None
        details = evidence.details if isinstance(evidence.details, dict) else {}
        active_pa = details.get("active_price_action")
        if not isinstance(active_pa, dict):
            return None
        if bool(active_pa.get("confirmed")):
            return None
        return {
            "code": self.cfg.reason.reversal_price_action_failed_code,
            "text": f"{active_side} exhaustion reversal is weakening: current price action no longer confirms the active side.",
            "active_side": active_side,
            "setup_label": self.cfg.pattern.setup_exhaustion_reversal,
            "price_action": active_pa,
            "setup_levels": setup_levels,
        }

    @staticmethod
    def _current_close(evidence: EvidenceResult) -> float | None:
        details = evidence.details if isinstance(evidence.details, dict) else {}
        snap = details.get("snapshot") if isinstance(details.get("snapshot"), dict) else {}
        return EvidenceLifecycleAdapter._as_float(snap.get("close"))

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _base_result(self, evidence: EvidenceResult, *, side: str, stage: LifecycleStage) -> LifecycleResult:
        side_e = LifecycleSide.BUY if side == BUY else LifecycleSide.SELL if side == SELL else LifecycleSide.NONE
        preferred_evidence = evidence.side_evidence(side) if side in SIDES else None
        setup_type = self._setup_type_from_strategy(evidence.strategy)
        confidence = (
            preferred_evidence.opportunity_score if preferred_evidence is not None else evidence.preferred_opportunity_score
        )
        quality = self._quality(confidence)
        result = LifecycleResult(
            symbol=evidence.symbol,
            snapshot_time=evidence.snapshot_time,
            lifecycle=self.cfg.lifecycle_name,
            lifecycle_version=self.cfg.engine_version,
            stage=stage,
            side=side_e,
            quality=quality,
            confidence=confidence,
            setup_type=setup_type,
            initiated_setup_state=SetupState.NONE,
            current_setup_state=SetupState.NONE,
            setup_state=SetupState.NONE,
            signal_action=SignalAction.WATCH,
            signal_state="WATCH",
            signal_reason=evidence.reason.code,
            transition_context={},
            price_action_context={},
            meta={
                "engine_name": evidence.engine_name,
                "engine_version": evidence.engine_version,
                "evidence_reason": evidence.reason.model_dump(mode="python"),
                "evidence_result": evidence.model_dump(mode="python"),
            },
        )
        result.structure_state = evidence.market_condition
        result.structure_side = side
        result.active_anchor = evidence.setup_label
        return result

    @staticmethod
    def _setup_type_from_strategy(strategy: str) -> SetupType:
        value = str(strategy or "").upper().strip()
        if value == "BREAKOUT":
            return SetupType.BREAKOUT
        if value == "CONTRA":
            return SetupType.REVERSAL
        if value == "REVERSAL":
            return SetupType.REVERSAL
        if value == "CONTINUATION":
            return SetupType.AUCTION_CONTINUATION
        if value == "MIXED":
            return SetupType.BALANCE
        return SetupType.NONE

    def _quality(self, confidence: float) -> LifecycleQuality:
        if confidence >= self.cfg.action.high_quality_min:
            return LifecycleQuality.HIGH
        if confidence >= self.cfg.action.medium_quality_min:
            return LifecycleQuality.MEDIUM
        return LifecycleQuality.LOW

    @staticmethod
    def _same_side_reason(*, code: str, side: str, quality: float, risk: float, message_action: str) -> Dict[str, Any]:
        return {
            "code": code,
            "text": f"{side} evidence {message_action}: continuation quality {quality:.2f}, risk {risk:.2f}.",
            "side": side,
            "continuation_quality": quality,
            "risk": risk,
        }

    @staticmethod
    def _opposite_reason(
        *,
        code: str,
        active_side: str,
        opposite_side: str,
        opposite_score: float,
        opposite_risk: float,
        score_gap: float,
    ) -> Dict[str, Any]:
        return {
            "code": code,
            "text": (
                f"{opposite_side} evidence replaces active {active_side}: opportunity {opposite_score:.2f}, "
                f"risk {opposite_risk:.2f}, score gap {score_gap:.2f}."
            ),
            "active_side": active_side,
            "opposite_side": opposite_side,
            "opposite_opportunity_score": opposite_score,
            "opposite_entry_risk": opposite_risk,
            "score_gap": score_gap,
        }

    def _downgrade_reason(
        self,
        *,
        active_side: str,
        continuation_quality: float,
        opposite_side: str,
        opposite_score: float,
    ) -> Dict[str, Any]:
        return {
            "code": self.cfg.reason.downgrade_code,
            "text": (
                f"{active_side} evidence is weakening: continuation quality {continuation_quality:.2f}; "
                f"opposite {opposite_side} pressure {opposite_score:.2f}."
            ),
            "active_side": active_side,
            "continuation_quality": continuation_quality,
            "opposite_side": opposite_side,
            "opposite_pressure": opposite_score,
        }
