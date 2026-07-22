from __future__ import annotations

from typing import Any, Dict, List

from configs.evidence_config import EVIDENCE_CONFIG
from services.evidence.evidence_result import EvidenceContribution, EvidenceReason, EvidenceResult, SideEvidence
from services.evidence.evidence_score_helper import (
    BUY,
    SELL,
    SIDES,
    as_dict,
    contribution,
    opposite_side,
    require_path,
    upper,
    validate_snapshot_contract,
    weighted_score,
)
from services.evidence.setup_discovery_helper import SetupCandidate, SetupDiscoverer, SetupDiscoveryResult


class EvidenceEvaluator:
    """Evidence V2 evaluator for AutoTrades Snapshot V1.

    V2 is setup-discovery and price-action-gated. Structure/indicators may
    discover setups or affect diagnostics, but they cannot create a signal
    without explicit price-action confirmation.
    """

    def __init__(self) -> None:
        self.cfg = EVIDENCE_CONFIG
        self.discoverer = SetupDiscoverer()

    def evaluate(self, snapshot: Any, existing_context: Dict[str, Any]) -> EvidenceResult:
        d = as_dict(snapshot)
        validate_snapshot_contract(d)

        symbol = str(require_path(d, "symbol")).strip().upper()
        snapshot_time = require_path(d, "snapshot_time")
        discovery = self.discoverer.discover_from_snapshot(d)

        opportunity = self._opportunity_contributions(discovery)
        risk = self._risk_contributions(discovery)

        buy_opportunity = weighted_score(opportunity[BUY])
        sell_opportunity = weighted_score(opportunity[SELL])
        buy_risk = weighted_score(risk[BUY])
        sell_risk = weighted_score(risk[SELL])

        continuation = self._continuation_contributions(
            buy_opportunity=buy_opportunity,
            sell_opportunity=sell_opportunity,
            buy_risk=buy_risk,
            sell_risk=sell_risk,
        )
        buy_continuation = weighted_score(continuation[BUY])
        sell_continuation = weighted_score(continuation[SELL])

        preferred_side = discovery.preferred_side
        if preferred_side == BUY:
            preferred_opportunity = buy_opportunity
            preferred_risk = buy_risk
            opposite_pressure = sell_opportunity
        elif preferred_side == SELL:
            preferred_opportunity = sell_opportunity
            preferred_risk = sell_risk
            opposite_pressure = buy_opportunity
        else:
            preferred_opportunity = max(buy_opportunity, sell_opportunity)
            preferred_risk = min(buy_risk, sell_risk)
            opposite_pressure = max(buy_opportunity, sell_opportunity)

        entry_permission = self._entry_permission(discovery)
        strategy, setup_label, primary_pattern = self._strategy_setup_pattern(discovery)
        reason = self._reason(
            discovery=discovery,
            preferred_side=preferred_side,
            opportunity=preferred_opportunity,
            risk=preferred_risk,
            entry_permission=entry_permission,
            strategy=strategy,
            setup_label=setup_label,
            primary_pattern=primary_pattern,
        )

        return EvidenceResult(
            symbol=symbol,
            snapshot_time=snapshot_time,
            engine_name=self.cfg.engine_name,
            engine_version=self.cfg.engine_version,
            buy=SideEvidence(
                side=BUY,
                opportunity_score=buy_opportunity,
                entry_risk=buy_risk,
                continuation_quality=buy_continuation,
                opportunity_contributions=opportunity[BUY],
                risk_contributions=risk[BUY],
                continuation_contributions=continuation[BUY],
            ),
            sell=SideEvidence(
                side=SELL,
                opportunity_score=sell_opportunity,
                entry_risk=sell_risk,
                continuation_quality=sell_continuation,
                opportunity_contributions=opportunity[SELL],
                risk_contributions=risk[SELL],
                continuation_contributions=continuation[SELL],
            ),
            preferred_side=preferred_side,
            preferred_opportunity_score=round(preferred_opportunity, 2),
            preferred_entry_risk=round(preferred_risk, 2),
            opposite_pressure=round(opposite_pressure, 2),
            market_condition=self._market_condition(d, discovery),
            strategy=strategy,
            setup_label=setup_label,
            primary_pattern=primary_pattern,
            entry_permission=entry_permission,
            evaluator_state=discovery.evaluator_state,
            decision=discovery.decision,
            price_action_confirmed=discovery.price_action_confirmed,
            price_action_strength=round(discovery.price_action_strength, 2),
            discovered_setups=[c.model_dump(mode="python") for c in discovery.discovered_setups],
            confirmed_setups=[c.model_dump(mode="python") for c in discovery.confirmed_setups],
            supporting_setups=[c.model_dump(mode="python") for c in discovery.supporting_setups],
            blocked_by=discovery.blocked_by,
            risk_flags=list(discovery.risk_flags),
            reason=reason,
            details=self._details(d, existing_context, discovery),
        )

    def _opportunity_contributions(self, discovery: SetupDiscoveryResult) -> Dict[str, List[EvidenceContribution]]:
        out: Dict[str, List[EvidenceContribution]] = {BUY: [], SELL: []}
        for side in SIDES:
            candidate = self._best_candidate_for_side(discovery.discovered_setups, side)
            if discovery.primary_setup and discovery.primary_setup.side == side and discovery.decision == "CREATE":
                score = self.cfg.setup_scoring.opportunity_score_ready
                message = f"{side} setup is entry-ready: {discovery.primary_setup.setup_label}."
                data = discovery.primary_setup.model_dump(mode="python")
            elif candidate and candidate.price_action_confirmed and not candidate.entry_blocked:
                score = self.cfg.setup_scoring.opportunity_score_confirmed
                message = f"{side} setup is price-action confirmed: {candidate.setup_label}."
                data = candidate.model_dump(mode="python")
            elif candidate:
                score = self.cfg.setup_scoring.opportunity_score_discovered
                message = f"{side} setup is discovered but not entry-ready: {candidate.setup_label}."
                data = candidate.model_dump(mode="python")
            else:
                score = self.cfg.action.watch_min_opportunity - 5.0
                message = f"No enabled {side} setup discovered."
                data = {}
            out[side].append(contribution(
                key="setup_opportunity",
                label="Setup opportunity",
                side=side,
                score=score,
                weight=1.0,
                message=message,
                data=data,
            ))
        return out

    def _risk_contributions(self, discovery: SetupDiscoveryResult) -> Dict[str, List[EvidenceContribution]]:
        out: Dict[str, List[EvidenceContribution]] = {BUY: [], SELL: []}
        for side in SIDES:
            candidate = self._best_candidate_for_side(discovery.discovered_setups, side)
            if candidate and candidate.entry_blocked:
                score = self.cfg.setup_scoring.blocked_entry_quality_risk
                message = f"{side} entry is blocked by {candidate.blocked_by}."
                data = candidate.model_dump(mode="python")
            else:
                score = self.cfg.setup_scoring.neutral_entry_quality_risk
                message = f"{side} setup entry quality is acceptable or neutral."
                data = candidate.model_dump(mode="python") if candidate else {}
            out[side].append(contribution(
                key="setup_entry_risk",
                label="Setup entry quality",
                side=side,
                score=score,
                weight=1.0,
                message=message,
                data=data,
            ))
        return out

    def _continuation_contributions(
        self,
        *,
        buy_opportunity: float,
        sell_opportunity: float,
        buy_risk: float,
        sell_risk: float,
    ) -> Dict[str, List[EvidenceContribution]]:
        out: Dict[str, List[EvidenceContribution]] = {BUY: [], SELL: []}
        risk_by_side = {BUY: buy_risk, SELL: sell_risk}
        opp_by_side = {BUY: buy_opportunity, SELL: sell_opportunity}
        for side in SIDES:
            opportunity_component = opp_by_side[side]
            risk_component = 100.0 - risk_by_side[side]
            score = max(0.0, min(100.0, (opportunity_component * 0.65) + (risk_component * 0.35)))
            out[side].append(contribution(
                key="continuation_quality",
                label="Continuation quality",
                side=side,
                score=score,
                weight=1.0,
                message=f"Continuation quality blends setup opportunity and entry quality for {side}.",
                data={"opportunity": opportunity_component, "entry_risk": risk_by_side[side]},
            ))
        return out

    def _entry_permission(self, discovery: SetupDiscoveryResult) -> str:
        if discovery.decision == "CREATE" and discovery.primary_setup is not None:
            return "ALLOW"
        if discovery.decision == "DEFER":
            return "DEFER"
        return "BLOCK"

    def _strategy_setup_pattern(self, discovery: SetupDiscoveryResult) -> tuple[str, str, str]:
        if discovery.primary_setup is not None:
            return discovery.primary_setup.strategy, discovery.primary_setup.setup_label, discovery.primary_setup.setup_label
        if discovery.discovered_setups:
            best = sorted(discovery.discovered_setups, key=lambda c: (c.priority, -c.price_action_strength))[0]
            return best.strategy, best.setup_label, best.setup_label
        return self.cfg.pattern.strategy_none, self.cfg.pattern.setup_no_action, self.cfg.pattern.setup_no_action

    def _reason(
        self,
        *,
        discovery: SetupDiscoveryResult,
        preferred_side: str,
        opportunity: float,
        risk: float,
        entry_permission: str,
        strategy: str,
        setup_label: str,
        primary_pattern: str,
    ) -> EvidenceReason:
        if entry_permission == "ALLOW" and preferred_side in SIDES:
            code = self.cfg.reason.create_code
            text = discovery.reason_text
        elif entry_permission == "DEFER":
            code = discovery.reason_code
            text = discovery.reason_text
        else:
            code = self.cfg.reason.no_entry_code
            text = discovery.reason_text
        entry_ready_count = len([c for c in discovery.confirmed_setups if c.price_action_confirmed and not c.entry_blocked])
        return EvidenceReason(code=code, text=text, data={
            "preferred_side": preferred_side,
            "opportunity": round(opportunity, 2),
            "risk": round(risk, 2),
            "entry_permission": entry_permission,
            "strategy": strategy,
            "setup_label": setup_label,
            "primary_pattern": primary_pattern,
            "decision": discovery.decision,
            "evaluator_state": discovery.evaluator_state,
            "price_action_confirmed": discovery.price_action_confirmed,
            "price_action_strength": discovery.price_action_strength,
            "blocked_by": discovery.blocked_by,
            "risk_flags": list(discovery.risk_flags),
            "setup_candidate_count": len(discovery.discovered_setups),
            "entry_ready_candidate_count": entry_ready_count,
            "confirmed_candidate_count": len(discovery.confirmed_setups),
        })

    def _details(self, d: Dict[str, Any], existing_context: Dict[str, Any], discovery: SetupDiscoveryResult) -> Dict[str, Any]:
        accepted = require_path(d, "structure.accepted")
        candidate = require_path(d, "structure.candidate")
        anchors = require_path(d, "structure.anchors")
        recent_closes = require_path(d, "structure.recent_closes")
        slope = require_path(d, "price_action.slope")
        indicators = require_path(d, "indicators")
        windows = require_path(d, "market_windows")

        def pick(obj: Any, keys: List[str]) -> Dict[str, Any]:
            src = obj if isinstance(obj, dict) else {}
            return {k: src.get(k) for k in keys if k in src}

        active_side = upper(existing_context.get("open_signal_side"))
        active_price_action: Dict[str, Any] | None = None
        if active_side in SIDES:
            active_price_action = self.discoverer.price_action_confirmation_for_side(d, active_side)

        primary_setup_levels = None
        primary_level_observation = None
        if discovery.primary_setup is not None:
            data = discovery.primary_setup.data if isinstance(discovery.primary_setup.data, dict) else {}
            levels = data.get("setup_levels")
            if isinstance(levels, dict):
                primary_setup_levels = levels
            setup_inputs = data.get("setup_inputs") if isinstance(data.get("setup_inputs"), dict) else {}
            observation = setup_inputs.get("level_observation")
            if isinstance(observation, dict):
                primary_level_observation = {
                    key: observation.get(key)
                    for key in (
                        "reference_id",
                        "level_type",
                        "price",
                        "side",
                        "status",
                        "acceptance_path",
                        "bars_outside",
                        "bars_reclaimed",
                        "current_offset_atr",
                        "attempt_time",
                        "accepted_time",
                        "failed_time",
                    )
                    if key in observation
                }

        setup_decision = self._setup_decision_payload(
            discovery=discovery,
            existing_context=existing_context,
            active_side=active_side,
        )

        ctx_keys = [
            "open_signal_side",
            "open_signal_stage",
            "open_signal_status",
            "open_signal_id",
            "open_trade_side",
            "open_trade_sides",
            "open_trade_count",
        ]
        window_keys = ["current", "15m", "30m", "60m", "sod"]
        return {
            "existing_context": pick(existing_context, ctx_keys),
            "snapshot": {
                "close": require_path(d, "close"),
                "snapshot_time": require_path(d, "snapshot_time"),
            },
            "active_price_action": active_price_action,
            "setup_levels": primary_setup_levels,
            "setup_decision": setup_decision,
            "normalized_setup_candidates": setup_decision.get("candidates", []),
            "discovery": discovery.model_dump(mode="python"),
            "structure": {
                "accepted": {
                    "state": accepted.get("state") if isinstance(accepted, dict) else None,
                    "quality": accepted.get("quality") if isinstance(accepted, dict) else None,
                    "range": pick((accepted or {}).get("range", {}), [
                        "range_id",
                        "version",
                        "source",
                        "range_type",
                        "high",
                        "low",
                        "width_atr",
                        "established_at",
                        "breakout_eligible",
                    ]),
                },
                "candidate": pick(candidate, ["status", "quality", "bars_confirmed", "reason"]),
                "anchors": pick(anchors, ["pdh", "pdl", "orb_high", "orb_low", "orb_ready", "active_anchor"]),
                "recent_close_count": len(recent_closes) if isinstance(recent_closes, list) else 0,
                "primary_level_observation": primary_level_observation,
            },
            "price_action": {
                "slope": pick(slope, ["state", "value", "angle", "direction"]),
            },
            "indicators": {
                "hma": pick(indicators.get("hma", {}), ["state", "strength"]),
                "rsi": pick(indicators.get("rsi", {}), ["value", "zone"]),
                "vwap": pick(indicators.get("vwap", {}), ["side", "distance_pct", "distance_atr"]),
                "bollinger": pick(indicators.get("bollinger", {}), ["zone", "position", "width_pct"]),
                "adx": pick(indicators.get("adx", {}), ["value", "band"]),
                "atr": pick(indicators.get("atr", {}), ["value", "band", "pct"]),
            },
            "market_windows": {
                name: pick(windows.get(name, {}), ["move_pct", "move_atr", "range_pct", "close_position_in_range", "bars"])
                for name in window_keys
                if isinstance(windows, dict) and name in windows
            },
        }

    def _setup_decision_payload(
        self,
        *,
        discovery: SetupDiscoveryResult,
        existing_context: Dict[str, Any],
        active_side: str,
    ) -> Dict[str, Any]:
        """Compact normalized setup view for audit/replay and Patch-A tracking.

        Full setup payloads remain available in discovery.discovered_setups. This
        normalized view gives the next orchestration layer a stable, small shape
        across ACCEPTED_BREAKOUT, FAILED_BREAKOUT and EXHAUSTION_REVERSAL without
        reintroducing a resolver.
        """
        candidates = [self._normalize_candidate(c) for c in discovery.discovered_setups]
        entry_ready = [c for c in candidates if bool(c.get("entry_ready"))]
        confirmed = [c for c in candidates if bool(c.get("price_action_confirmed"))]

        reference_side = active_side if active_side in SIDES else discovery.preferred_side
        if reference_side not in SIDES and discovery.primary_setup is not None:
            reference_side = discovery.primary_setup.side

        same_side = [c for c in candidates if reference_side in SIDES and c.get("side") == reference_side]
        opposite = [c for c in candidates if reference_side in SIDES and c.get("side") in SIDES and c.get("side") != reference_side]

        setup_counts: Dict[str, int] = {}
        blocker_counts: Dict[str, int] = {}
        side_counts: Dict[str, int] = {BUY: 0, SELL: 0}
        for c in candidates:
            label = str(c.get("setup_label") or "UNKNOWN")
            setup_counts[label] = setup_counts.get(label, 0) + 1
            side = str(c.get("side") or "NONE")
            if side in side_counts:
                side_counts[side] += 1
            blocked_by = c.get("blocked_by")
            if blocked_by:
                key = str(blocked_by)
                blocker_counts[key] = blocker_counts.get(key, 0) + 1

        primary = self._normalize_candidate(discovery.primary_setup) if discovery.primary_setup is not None else None
        same_side_entry_ready = [c for c in same_side if bool(c.get("entry_ready"))]
        opposite_entry_ready = [c for c in opposite if bool(c.get("entry_ready"))]
        same_side_confirmed = [c for c in same_side if bool(c.get("price_action_confirmed"))]
        opposite_confirmed = [c for c in opposite if bool(c.get("price_action_confirmed"))]
        active_setup_label = upper(existing_context.get("open_signal_setup_label"))
        active_signal_evidence = self._active_signal_evidence_payload(
            active_side=active_side,
            active_setup_label=active_setup_label,
            reference_side=reference_side,
            primary=primary,
            same_side=same_side,
            opposite=opposite,
            same_side_entry_ready=same_side_entry_ready,
            opposite_entry_ready=opposite_entry_ready,
            same_side_confirmed=same_side_confirmed,
            opposite_confirmed=opposite_confirmed,
        )
        return {
            "phase": "PATCH_D_ACTIVE_SIGNAL_EVIDENCE_ACTION_MAPPING",
            "has_active_signal": active_side in SIDES,
            "active_side": active_side if active_side in SIDES else "NONE",
            "active_setup_label": active_setup_label or "NONE",
            "reference_side": reference_side if reference_side in SIDES else "NONE",
            "decision": discovery.decision,
            "evaluator_state": discovery.evaluator_state,
            "preferred_side": discovery.preferred_side,
            "primary_candidate": primary,
            "candidate_count": len(candidates),
            "confirmed_candidate_count": len(confirmed),
            "entry_ready_candidate_count": len(entry_ready),
            "same_side_candidate_count": len(same_side),
            "same_side_confirmed_count": len(same_side_confirmed),
            "same_side_entry_ready_count": len(same_side_entry_ready),
            "opposite_candidate_count": len(opposite),
            "opposite_confirmed_count": len(opposite_confirmed),
            "opposite_entry_ready_count": len(opposite_entry_ready),
            "setup_counts": setup_counts,
            "side_counts": side_counts,
            "blocker_counts": blocker_counts,
            "active_signal_evidence": active_signal_evidence,
            "candidates": candidates,
            "entry_ready_candidates": entry_ready,
            "same_side_candidates": same_side,
            "same_side_entry_ready_candidates": same_side_entry_ready,
            "opposite_candidates": opposite,
            "opposite_entry_ready_candidates": opposite_entry_ready,
            "policy": {
                "enabled_setups": [
                    name
                    for name, rule in self.cfg.setup_discovery.setup_rules.items()
                    if bool(rule.enabled)
                ],
                "auto_reverse_enabled": bool(self.cfg.action.enable_opposite_auto_replace),
                "note": (
                    "Patch D maps active-signal setup evidence into SUPPORT / STRENGTHEN / CAUTION / EXIT instructions. "
                    "This patch is still non-executing for trade management: it does not auto-reverse and does not by itself close trades. "
                    "Same-pass reversal remains disabled; opposite CREATE can occur only on a later pass if no active signal exists at pass start."
                ),
            },
        }

    def _active_signal_evidence_payload(
        self,
        *,
        active_side: str,
        active_setup_label: str,
        reference_side: str,
        primary: Dict[str, Any] | None,
        same_side: List[Dict[str, Any]],
        opposite: List[Dict[str, Any]],
        same_side_entry_ready: List[Dict[str, Any]],
        opposite_entry_ready: List[Dict[str, Any]],
        same_side_confirmed: List[Dict[str, Any]],
        opposite_confirmed: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Summarize setup evidence around an already-active signal.

        This is tracking-only.  It does not create/reverse signals.  The
        lifecycle adapter and signal metadata use this compact shape to show
        whether new setup discoveries are supporting the active side or pushing
        against it.
        """
        has_active = active_side in SIDES
        top_same = self._best_normalized_candidate(same_side_entry_ready or same_side_confirmed or same_side)
        top_opp = self._best_normalized_candidate(opposite_entry_ready or opposite_confirmed or opposite)
        opposite_confirmed_exhaustion = self._best_normalized_candidate([
            candidate
            for candidate in opposite_confirmed
            if upper(candidate.get("setup_label")) == upper(self.cfg.pattern.setup_exhaustion_reversal)
        ])
        active_breakout_invalidated_by_exhaustion = bool(
            has_active
            and bool(getattr(self.cfg.price_action, "active_accepted_breakout_exit_on_confirmed_exhaustion", True))
            and upper(active_setup_label) == upper(self.cfg.pattern.setup_accepted_breakout)
            and isinstance(opposite_confirmed_exhaustion, dict)
        )

        # Pair-specific active-signal priority. A confirmed exhaustion reversal
        # already won the continuation/reversal conflict when it was created.
        # Do not let a later opposing ACCEPTED_BREAKOUT candidate immediately
        # invalidate that active reversal through the generic
        # OPPOSITE_SETUP_ENTRY_READY branch. The exhaustion signal remains active
        # until its own lifecycle/invalidation reference is breached.
        top_opposite_is_accepted_breakout = bool(
            isinstance(top_opp, dict)
            and upper(top_opp.get("setup_label"))
            == upper(self.cfg.pattern.setup_accepted_breakout)
        )
        active_exhaustion_overrides_breakout = bool(
            has_active
            and bool(
                getattr(
                    self.cfg.price_action,
                    "active_exhaustion_overrides_opposing_accepted_breakout",
                    True,
                )
            )
            and upper(active_setup_label)
            == upper(self.cfg.pattern.setup_exhaustion_reversal)
            and top_opposite_is_accepted_breakout
        )

        if not has_active:
            evidence_action = "NO_ACTIVE_SIGNAL"
            exit_pressure = "NONE"
            trail_mode = "NONE"
            target_expansion_allowed = None
            should_exit_signal = False
            reason_code = "NO_ACTIVE_SIGNAL"
            reason_text = "No active signal exists; setup evidence is used for fresh-signal selection only."
        elif active_breakout_invalidated_by_exhaustion:
            top_opp = opposite_confirmed_exhaustion
            evidence_action = "EXIT"
            exit_pressure = "HIGH"
            trail_mode = "EXIT_READY"
            target_expansion_allowed = False
            should_exit_signal = True
            reason_code = self.cfg.reason.active_breakout_exit_confirmed_exhaustion_code
            reason_text = (
                "Opposite exhaustion reversal is price-action confirmed; the active "
                "accepted-breakout continuation thesis is invalidated. Exit now, retain "
                "the reversal as CONFIRMED_PENDING, and do not reverse in the same pass."
            )
        elif active_exhaustion_overrides_breakout:
            evidence_action = "HOLD"
            exit_pressure = "LOW"
            trail_mode = "NORMAL"
            target_expansion_allowed = False
            should_exit_signal = False
            reason_code = self.cfg.reason.active_exhaustion_overrides_breakout_code
            reason_text = (
                "Active exhaustion reversal retains priority over the opposing "
                "accepted-breakout continuation candidate. Keep the reversal signal "
                "active; only its own invalidation/lifecycle rule may close it."
            )
        elif opposite_entry_ready:
            evidence_action = "EXIT"
            exit_pressure = "HIGH"
            trail_mode = "EXIT_READY"
            target_expansion_allowed = False
            should_exit_signal = True
            reason_code = "OPPOSITE_SETUP_ENTRY_READY"
            reason_text = "Opposite-side setup is entry-ready; active signal should exit, with no same-pass reversal."
        elif opposite_confirmed:
            evidence_action = "CAUTION"
            exit_pressure = "MEDIUM"
            trail_mode = "DEFENSIVE"
            target_expansion_allowed = False
            should_exit_signal = False
            reason_code = "OPPOSITE_SETUP_CONFIRMED_NOT_ENTRY_READY"
            reason_text = "Opposite-side setup is price-action confirmed but not entry-ready; tighten/protect active signal, do not exit yet."
        elif opposite:
            evidence_action = "CAUTION"
            exit_pressure = "MEDIUM"
            trail_mode = "DEFENSIVE"
            target_expansion_allowed = False
            should_exit_signal = False
            reason_code = "OPPOSITE_SETUP_DISCOVERED"
            reason_text = "Opposite-side setup is discovered but not confirmed; tighten/protect active signal, do not exit yet."
        elif same_side_entry_ready:
            evidence_action = "STRENGTHEN"
            exit_pressure = "LOW"
            trail_mode = "NORMAL"
            target_expansion_allowed = True
            should_exit_signal = False
            reason_code = "SAME_SIDE_SETUP_ENTRY_READY_SUPPORT"
            reason_text = "Same-side setup is entry-ready; treat as strong support for the active signal."
        elif same_side_confirmed:
            evidence_action = "STRENGTHEN"
            exit_pressure = "LOW"
            trail_mode = "NORMAL"
            target_expansion_allowed = True
            should_exit_signal = False
            reason_code = "SAME_SIDE_SETUP_CONFIRMED_SUPPORT"
            reason_text = "Same-side setup is price-action confirmed; treat as support for the active signal."
        elif same_side:
            evidence_action = "SUPPORT"
            exit_pressure = "LOW"
            trail_mode = "NORMAL"
            target_expansion_allowed = True
            should_exit_signal = False
            reason_code = "SAME_SIDE_SETUP_DISCOVERED_SUPPORT"
            reason_text = "Same-side setup is discovered; keep normal management and continue watching for confirmation."
        else:
            evidence_action = "HOLD"
            exit_pressure = "LOW"
            trail_mode = "NORMAL"
            target_expansion_allowed = None
            should_exit_signal = False
            reason_code = "NO_SETUP_EVIDENCE_AROUND_ACTIVE_SIGNAL"
            reason_text = "No same-side or opposite-side setup evidence around the active signal."

        support_score = self._candidate_support_score(top_same)
        opposition_score = self._candidate_support_score(top_opp)

        return {
            "mode": "ACTION_MAPPING_NO_AUTO_REVERSAL",
            "has_active_signal": has_active,
            "active_side": active_side if has_active else "NONE",
            "active_setup_label": upper(active_setup_label) or "NONE",
            "reference_side": reference_side if reference_side in SIDES else "NONE",
            "evidence_action": evidence_action,
            "active_evidence_action": evidence_action,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "exit_pressure": exit_pressure,
            "trail_mode": trail_mode,
            "target_expansion_allowed": target_expansion_allowed,
            "should_exit_signal": should_exit_signal,
            "same_pass_reversal_allowed": False,
            "next_pass_create_policy": "ALLOW_ONLY_IF_NO_ACTIVE_SIGNAL_AT_PASS_START",
            "support_score": support_score,
            "opposition_score": opposition_score,
            "same_side_candidate_count": len(same_side),
            "same_side_confirmed_count": len(same_side_confirmed),
            "same_side_entry_ready_count": len(same_side_entry_ready),
            "opposite_candidate_count": len(opposite),
            "opposite_confirmed_count": len(opposite_confirmed),
            "opposite_entry_ready_count": len(opposite_entry_ready),
            "top_same_side_candidate": self._compact_candidate(top_same),
            "top_opposite_candidate": self._compact_candidate(top_opp),
            "primary_candidate": self._compact_candidate(primary),
        }

    @staticmethod
    def _best_normalized_candidate(candidates: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda c: (
                0 if bool(c.get("entry_ready")) else 1,
                0 if bool(c.get("price_action_confirmed")) else 1,
                int(c.get("priority") or 999),
                -float(c.get("price_action_strength") or 0.0),
            ),
        )[0]

    @staticmethod
    def _candidate_support_score(candidate: Dict[str, Any] | None) -> float:
        if not isinstance(candidate, dict):
            return 0.0
        score = float(candidate.get("price_action_strength") or 0.0)
        if bool(candidate.get("entry_ready")):
            score += 25.0
        elif bool(candidate.get("price_action_confirmed")):
            score += 10.0
        return round(max(0.0, min(100.0, score)), 2)

    @staticmethod
    def _compact_candidate(candidate: Dict[str, Any] | None) -> Dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        keys = [
            "setup_label",
            "strategy",
            "side",
            "entry_ready",
            "price_action_confirmed",
            "price_action_strength",
            "blocked_by",
            "reference_id",
            "level_type",
            "level_source",
            "level_price",
            "entry_price",
            "setup_reference_price",
            "setup_reference_source",
            "invalidation_side",
            "acceptance_path",
            "event_key",
            "event_source",
            "event_time",
            "setup_levels",
        ]
        return {k: candidate.get(k) for k in keys if k in candidate}

    def _normalize_candidate(self, candidate: SetupCandidate | None) -> Dict[str, Any] | None:
        if candidate is None:
            return None
        data = candidate.data if isinstance(candidate.data, dict) else {}
        setup_inputs = data.get("setup_inputs") if isinstance(data.get("setup_inputs"), dict) else {}
        location = data.get("entry_location_filter") if isinstance(data.get("entry_location_filter"), dict) else {}
        setup_levels = data.get("setup_levels") if isinstance(data.get("setup_levels"), dict) else {}
        price_action = data.get("price_action") if isinstance(data.get("price_action"), dict) else {}
        watched_extreme = setup_inputs.get("watched_extreme") if isinstance(setup_inputs.get("watched_extreme"), dict) else {}

        event_key = self._first_not_none(
            setup_inputs.get("watch_event_key"),
            setup_inputs.get("event_key"),
            watched_extreme.get("event_key"),
        )
        event_source = self._first_not_none(
            setup_inputs.get("watch_source"),
            setup_inputs.get("event_source"),
            watched_extreme.get("source"),
        )
        event_time = self._first_not_none(
            setup_inputs.get("watch_event_time"),
            setup_inputs.get("event_time"),
            watched_extreme.get("event_time"),
            watched_extreme.get("snapshot_time"),
            setup_levels.get("watch_snapshot_time"),
            setup_levels.get("confirmation_time"),
        )

        level_price = self._first_not_none(
            setup_inputs.get("level_price"),
            setup_inputs.get("breakout_level", {}).get("price") if isinstance(setup_inputs.get("breakout_level"), dict) else None,
            setup_inputs.get("failed_level", {}).get("price") if isinstance(setup_inputs.get("failed_level"), dict) else None,
            setup_inputs.get("reabsorbed_level", {}).get("price") if isinstance(setup_inputs.get("reabsorbed_level"), dict) else None,
            setup_levels.get("level_price"),
            location.get("level_price"),
        )
        reference_id = self._first_not_none(
            setup_inputs.get("reference_id"),
            setup_inputs.get("breakout_level", {}).get("reference_id") if isinstance(setup_inputs.get("breakout_level"), dict) else None,
            setup_inputs.get("failed_level", {}).get("reference_id") if isinstance(setup_inputs.get("failed_level"), dict) else None,
            setup_inputs.get("reabsorbed_level", {}).get("reference_id") if isinstance(setup_inputs.get("reabsorbed_level"), dict) else None,
        )
        level_type = self._first_not_none(
            setup_inputs.get("level_type"),
            setup_inputs.get("breakout_level", {}).get("level_type") if isinstance(setup_inputs.get("breakout_level"), dict) else None,
            setup_inputs.get("failed_level", {}).get("level_type") if isinstance(setup_inputs.get("failed_level"), dict) else None,
            setup_inputs.get("reabsorbed_level", {}).get("level_type") if isinstance(setup_inputs.get("reabsorbed_level"), dict) else None,
        )
        level_source = self._first_not_none(
            setup_inputs.get("level_source"),
            setup_inputs.get("breakout_level", {}).get("source") if isinstance(setup_inputs.get("breakout_level"), dict) else None,
            setup_inputs.get("failed_level", {}).get("source") if isinstance(setup_inputs.get("failed_level"), dict) else None,
            setup_inputs.get("reabsorbed_level", {}).get("source") if isinstance(setup_inputs.get("reabsorbed_level"), dict) else None,
        )

        # Normalized audit should explain why every discovered setup is not
        # entry-ready. Some frozen setup helpers intentionally leave
        # candidate.blocked_by empty when the only blocker is missing price-action
        # confirmation. Keep the core candidate behavior unchanged, but add a
        # synthetic audit blocker so full-universe replays show a clean funnel.
        normalized_blocked_by = candidate.blocked_by
        normalized_risk_flags = list(candidate.risk_flags or [])
        if not normalized_blocked_by and not bool(candidate.price_action_confirmed):
            normalized_blocked_by = f"{candidate.setup_label}_PRICE_ACTION_NOT_CONFIRMED"
            if normalized_blocked_by not in normalized_risk_flags:
                normalized_risk_flags.append(normalized_blocked_by)

        return {
            "setup_label": candidate.setup_label,
            "strategy": candidate.strategy,
            "side": candidate.side,
            "priority": candidate.priority,
            "discovered": bool(candidate.discovered),
            "price_action_confirmed": bool(candidate.price_action_confirmed),
            "price_action_strength": round(float(candidate.price_action_strength or 0.0), 2),
            "entry_blocked": bool(candidate.entry_blocked),
            "entry_ready": bool(candidate.price_action_confirmed and not candidate.entry_blocked),
            "blocked_by": normalized_blocked_by,
            "reason_code": candidate.reason_code,
            "evidence_state": candidate.evidence_state,
            "risk_flags": normalized_risk_flags,
            "reference_id": reference_id,
            "level_type": level_type,
            "level_source": level_source,
            "level_price": self._as_float(level_price),
            "entry_price": self._as_float(location.get("close") or setup_levels.get("confirmation_price") or price_action.get("close")),
            "setup_reference_price": self._as_float(setup_levels.get("reference_price") or setup_levels.get("initial_stop_reference_price") or location.get("level_price")),
            "setup_reference_source": setup_levels.get("reference_source") or setup_levels.get("initial_stop_reference_source"),
            "invalidation_side": setup_levels.get("invalidation_side") or setup_levels.get("initial_stop_side"),
            "setup_levels": setup_levels,
            "entry_distance_from_level_atr": self._as_float(location.get("entry_distance_from_level_atr")),
            # Target/reward and risk-to-invalidation filters are intentionally
            # not part of signal candidate normalization.  Signal creation is
            # setup/price-action driven; trade manager owns targets, stops,
            # trailing and profit capture.
            "acceptance_path": setup_inputs.get("acceptance_path"),
            "breakout_status": setup_inputs.get("breakout_status"),
            "event_key": event_key,
            "event_source": event_source,
            "event_time": event_time,
            "price_action": {
                "single_candle_confirmed": bool(price_action.get("single_candle_confirmed")),
                "multi_candle_confirmed": bool(price_action.get("multi_candle_confirmed")),
                "strength": self._as_float(price_action.get("strength")),
                "move_15m_atr": self._as_float(price_action.get("move_15m_atr")),
                "position_15m": self._as_float(price_action.get("position_15m")),
            },
        }

    @staticmethod
    def _first_not_none(*values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _market_condition(self, d: Dict[str, Any], discovery: SetupDiscoveryResult) -> str:
        primary_label = upper(discovery.primary_setup.setup_label) if discovery.primary_setup is not None else ""
        if primary_label == upper(self.cfg.pattern.setup_failed_breakout):
            return "FAILED_BREAKOUT"
        if primary_label == upper(self.cfg.pattern.setup_accepted_breakout):
            return "ACCEPTED_BREAKOUT"
        labels = {upper(c.setup_label) for c in discovery.discovered_setups}
        if upper(self.cfg.pattern.setup_failed_breakout) in labels:
            return "FAILED_BREAKOUT"
        if upper(self.cfg.pattern.setup_accepted_breakout) in labels:
            return "ACCEPTED_BREAKOUT"
        raw_state = upper(require_path(d, "structure.raw.state"))
        if raw_state in {"RANGE_ACCEPTED", "COMPRESSION", "BALANCE_QUALIFIED", "TRENDING_UP", "TRENDING_DOWN", "EXPANDING_RANGE"}:
            return raw_state
        return "MIXED"

    @staticmethod
    def _best_candidate_for_side(candidates: List[SetupCandidate], side: str) -> SetupCandidate | None:
        side_candidates = [c for c in candidates if c.side == side]
        if not side_candidates:
            return None
        return sorted(side_candidates, key=lambda c: (c.priority, -c.price_action_strength))[0]
