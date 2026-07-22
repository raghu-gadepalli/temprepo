# services/signal_helper.py

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from configs.signal_config import SIGNAL_CONFIG


class SignalHelper:
    """
    Signal/lifecycle resolver for UI display.

    Important:
    - This helper does NOT re-evaluate lifecycle rules.
    - LifecycleHelper / SignalGenerator are the source of truth.
    - This helper only ranks already-generated signals for UI display.
    - The resolver should show the stock's state across signal profiles first, then provide
      a preferred recommendation only when one signal is clearly stronger.
    """

    # UI/resolution rank maps are owned by signal_config.py.
    # Keep class attributes for the existing resolver code path, but bind them
    # directly to configuration so ranking changes never require helper edits.
    STAGE_RANK = SIGNAL_CONFIG.resolution.stage_rank
    QUALITY_RANK = SIGNAL_CONFIG.resolution.quality_rank
    TRADE_ACTION_RANK = SIGNAL_CONFIG.resolution.trade_action_rank
    SIGNAL_ACTION_RANK = SIGNAL_CONFIG.resolution.signal_action_rank
    ENTRY_POSTURE_RANK = SIGNAL_CONFIG.resolution.entry_posture_rank
    SIGNAL_DECISION_RANK = SIGNAL_CONFIG.resolution.signal_decision_rank
    SIGNAL_STATE_RANK = SIGNAL_CONFIG.resolution.signal_state_rank

    @classmethod
    def build_signal_context(
        cls,
        *,
        symbol: str,
        ltp: float,
        snapshot_time: Any,
        snapshot: Optional[Dict[str, Any]] = None,
        signals: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        snap = cls._as_dict(snapshot)
        context = cls._build_context(snap)

        signal_rows = cls._build_signal_rows(
            signals or [],
            context=context,
        )

        signal_resolution = cls._resolve_preferred_signal(
            signal_rows=signal_rows,
            context=context,
        )

        return {
            "symbol": cls._txt(symbol, ""),
            "ltp": cls._fnum(ltp, 0.0),
            "snapshot_time": snapshot_time,
            "context": context,
            "signals": signal_rows,
            "signal_resolution": signal_resolution,
        }

    # ---------------------------------------------------------------------
    # Signal rows from generated signals
    # ---------------------------------------------------------------------

    @classmethod
    def _build_signal_rows(
        cls,
        signals: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        context = context or {}
        market_context = cls._as_dict(context.get("market_context"))

        for sig in signals:
            lifecycle = cls._upper(sig.get("lifecycle"))
            side = cls._upper(sig.get("side"))
            stage = cls._upper(sig.get("stage"))
            status = cls._upper(sig.get("status"))

            confidence = cls._fnum(sig.get("confidence"), 0.0) or 0.0
            quality = cls._upper(sig.get("quality")) or "LOW"

            # Signal decision vocabulary. Resolver decisions use signal_action/signal_state.
            signal_action = cls._upper(sig.get("signal_action")) or "WATCH"
            signal_state = cls._upper(sig.get("signal_state")) or "WATCH"
            signal_reason = cls._txt(sig.get("signal_reason"), "")
            trade_action = cls._upper(sig.get("trade_action"))

            blocked_by_policy = bool(sig.get("blocked_by_policy"))
            block_count = len(sig.get("blocks") or [])
            conflict_count = len(sig.get("conflicts") or [])
            warning_count = len(sig.get("warnings") or [])
            support_count = len(sig.get("supports") or [])

            entry_view = cls._entry_view_from_lifecycle(
                signal_action=signal_action,
                signal_state=signal_state,
                blocked_by_policy=blocked_by_policy,
                stage=stage,
                legacy_signal_action=signal_action,
                legacy_trade_action=trade_action,
            )

            state = cls._state_from_lifecycle(
                stage=stage,
                status=status,
                signal_action=signal_action,
                signal_state=signal_state,
                blocked_by_policy=blocked_by_policy,
            )

            continuation_view = cls._continuation_view_from_lifecycle(
                stage=stage,
                signal_action=signal_action,
                signal_state=signal_state,
            )

            market_view = cls._market_view_for_signal(
                lifecycle=lifecycle,
                side=side,
                market_context=market_context,
            )

            rank_score = cls._rank_score(
                stage=stage,
                confidence=confidence,
                quality=quality,
                signal_action=signal_action,
                signal_state=signal_state,
                blocked_by_policy=blocked_by_policy,
                block_count=block_count,
                conflict_count=conflict_count,
                market_view=market_view,
            )

            reason = cls._first_reason(sig) or signal_reason

            rows.append({
                "lifecycle": lifecycle,
                "signal_id": cls._txt(sig.get("signal_id"), ""),
                "side": side,
                "stage": stage,
                "status": status,

                "state": state,
                "entry_view": entry_view,
                "continuation_view": continuation_view,
                "transition": stage,

                "confidence": confidence,
                "quality": quality,

                "signal_action": signal_action,
                "signal_state": signal_state,
                "signal_reason": signal_reason,
                "trade_action": trade_action,

                "blocked_by_policy": blocked_by_policy,
                "blocked_by_policy_reason": cls._txt(sig.get("blocked_by_policy_reason"), ""),

                "block_count": block_count,
                "conflict_count": conflict_count,
                "warning_count": warning_count,
                "support_count": support_count,

                "first_seen_time": cls._txt(sig.get("first_seen_time"), ""),
                "last_eval_time": cls._txt(sig.get("last_eval_time"), ""),
                "last_snapshot_time": cls._txt(sig.get("last_snapshot_time"), ""),

                "summary": cls._summary_from_signal(
                    lifecycle=lifecycle,
                    side=side,
                    stage=stage,
                    confidence=confidence,
                    quality=quality,
                    signal_action=signal_action,
                    signal_state=signal_state,
                    blocked_by_policy=blocked_by_policy,
                    reason=reason,
                    market_view=market_view,
                ),
                "reason": reason,
                "reasons": cls._reasons(sig),

                "market_context": market_context,
                "market_view": market_view,

                "_rank_score": rank_score,
            })

        rows.sort(
            key=lambda r: (
                r.get("_rank_score", 0),
                cls._fnum(r.get("confidence"), 0.0) or 0.0,
                cls.STAGE_RANK.get(cls._upper(r.get("stage")), 0),
            ),
            reverse=True,
        )

        for row in rows:
            row.pop("_rank_score", None)

        return rows

    @classmethod
    def _resolve_preferred_signal(
        cls,
        *,
        signal_rows: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        lifecycle_statuses = cls._candidate_summary(signal_rows)

        if not signal_rows:
            return {
                "preferred_lifecycle": None,
                "preferred_signal_id": None,
                "side": None,
                "action": "WATCH",
                "summary": "No active lifecycle signal. Review stock context only.",
                "conflict": False,
                "conflict_summary": "",
                "candidates": [],
                "lifecycle_statuses": [],
                "all_lifecycles": [],
                "market_context": cls._as_dict(context.get("market_context")),
            }

        valid = [
            row for row in signal_rows
            if cls._upper(row.get("status")) == "OPEN"
            and not bool(row.get("blocked_by_policy"))
            and cls._upper(row.get("signal_state")) not in (
                "BLOCKED", "CLOSED", "REPLACED", "INVALIDATED", "EXPIRED", "CANCELLED"
            )
        ]

        if not valid:
            return {
                "preferred_lifecycle": None,
                "preferred_signal_id": None,
                "side": None,
                "action": "WATCH",
                "summary": "Lifecycle signals exist, but none are currently actionable.",
                "conflict": True,
                "conflict_summary": "All active signals are blocked or not currently tradable.",
                "candidates": lifecycle_statuses,
                "lifecycle_statuses": lifecycle_statuses,
                "all_lifecycles": lifecycle_statuses,
                "market_context": cls._as_dict(context.get("market_context")),
            }

        valid.sort(
            key=lambda r: (
                cls.SIGNAL_STATE_RANK.get(cls._upper(r.get("signal_state")), 0),
                cls.SIGNAL_DECISION_RANK.get(cls._upper(r.get("signal_action")), 0),
                cls.STAGE_RANK.get(cls._upper(r.get("stage")), 0),
                cls.QUALITY_RANK.get(cls._upper(r.get("quality")), 0),
                cls._fnum(r.get("confidence"), 0.0) or 0.0,
            ),
            reverse=True,
        )

        top = valid[0]

        # Tactical REVERSAL override:
        # The default resolver ranking favors the dominant continuation / breakout
        # narrative. That is correct most of the time, but it suppresses valid
        # intraday mean-reversion entries when price is visibly stretched.
        #
        # Keep lifecycle as the source of truth: the REVERSAL row must already be
        # entry-capable. This block only lets a strong REVERSAL candidate win the
        # resolver preference when exhaustion evidence exists in the snapshot
        # context. This keeps UI and backend trade gating aligned.
        contra_top = cls._select_tactical_contra_override(
            candidates=valid,
            context=context,
            current_top=top,
        )
        if contra_top is not None:
            top = contra_top

        top_lifecycle = cls._upper(top.get("lifecycle"))
        top_signal_id = cls._txt(top.get("signal_id"), "")
        top_side = cls._upper(top.get("side"))

        opposite = [
            row for row in valid[1:]
            if cls._upper(row.get("side")) in ("BUY", "SELL")
            and cls._upper(row.get("side")) != top_side
        ]

        conflict = bool(opposite)
        conflict_summary = ""

        if conflict:
            best_signal = opposite[0]
            conflict_summary = (
                f"{top_lifecycle} {top_side} is preferred, but "
                f"{cls._upper(best_signal.get('lifecycle'))} {cls._upper(best_signal.get('side'))} "
                f"also exists with confidence {cls._fnum(best_signal.get('confidence'), 0.0):.2f}."
            )

        action = cls._resolved_action(top)

        return {
            "preferred_lifecycle": top_lifecycle,
            "preferred_signal_id": top_signal_id,
            "side": top_side,
            "action": action,
            "summary": cls._resolved_summary(
                top=top,
                action=action,
                conflict=conflict,
                conflict_summary=conflict_summary,
            ),
            "conflict": conflict,
            "conflict_summary": conflict_summary,
            "candidates": cls._candidate_summary(valid),
            "lifecycle_statuses": lifecycle_statuses,
            "all_lifecycles": lifecycle_statuses,
            "market_context": cls._as_dict(context.get("market_context")),
        }


    # ---------------------------------------------------------------------
    # Tactical REVERSAL override
    # ---------------------------------------------------------------------

    @classmethod
    def _select_tactical_contra_override(
        cls,
        *,
        candidates: List[Dict[str, Any]],
        context: Dict[str, Any],
        current_top: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Allow strong tactical REVERSAL to become the resolver preference.

        This deliberately does not create a new lifecycle rule. It only changes
        ranking among already-generated lifecycle signals. A REVERSAL row
        must already be entry-capable according to lifecycle/resolver semantics.

        Use case: a symbol can have a bullish/bearish continuation narrative,
        but RSI/BB/VWAP stretch can make the immediate tactical edge a REVERSAL
        reversion. If we leave the continuation row as preferred, backend entry
        gating will suppress the useful REVERSAL trade even though the UI knows
        the stock is stretched.
        """
        if not candidates:
            return None

        current_lifecycle = cls._upper(current_top.get("lifecycle"))

        best: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for row in candidates:
            if cls._upper(row.get("lifecycle")) != "REVERSAL":
                continue

            if not cls._is_entry_capable_contra(row):
                continue

            score = cls._contra_exhaustion_score(row=row, context=context)

            if score > best_score:
                best = row
                best_score = score

        if best is None:
            return None

        # Conservative threshold: require real exhaustion evidence, not just
        # the presence of a REVERSAL row. This avoids making REVERSAL the default
        # whenever continuation and reversion coexist.
        if best_score < 70:
            return None

        # If REVERSAL is already top, leave it as-is.
        if current_lifecycle == "REVERSAL":
            return current_top

        # Otherwise let strong tactical REVERSAL override trend/breakout.
        best["resolver_override"] = "TACTICAL_REVERSAL"
        best["resolver_override_score"] = round(best_score, 2)
        return best

    @classmethod
    def _is_entry_capable_contra(cls, row: Dict[str, Any]) -> bool:
        if bool(row.get("blocked_by_policy")):
            return False

        if cls._upper(row.get("status")) != "OPEN":
            return False

        state = cls._upper(row.get("signal_state"))
        action = cls._upper(row.get("signal_action"))
        entry_view = cls._upper(row.get("entry_view"))

        if state in ("BLOCKED", "CLOSED", "REPLACED", "INVALIDATED", "EXPIRED", "CANCELLED"):
            return False

        if entry_view == "ENTER":
            return True

        if state in ("ACCEPTED", "READY") and action in ("CREATE", "PROMOTE", "REPLACE_CREATE"):
            return True

        return False

    @classmethod
    def _contra_exhaustion_score(
        cls,
        *,
        row: Dict[str, Any],
        context: Dict[str, Any],
    ) -> float:
        side = cls._upper(row.get("side"))
        score = 0.0

        confidence = cls._fnum(row.get("confidence"), 0.0) or 0.0
        quality = cls._upper(row.get("quality"))
        stage = cls._upper(row.get("stage"))

        if confidence >= 65:
            score += 15
        elif confidence >= 50:
            score += 10
        elif confidence >= 40:
            score += 5

        if quality == "HIGH":
            score += 12
        elif quality == "MEDIUM":
            score += 6

        if stage in ("BUILDING", "ACTIVE", "EXPAND", "PROTECT"):
            score += 8

        rsi = cls._fnum(context.get("rsi"), None)
        rsi_zone = cls._upper(context.get("rsi_zone"))
        bb_zone = cls._upper(context.get("bb_zone"))
        bb_position = cls._fnum(context.get("bb_position"), None)
        vwap_delta_pct = cls._fnum(context.get("vwap_delta_pct"), None)

        if side == "SELL":
            if rsi is not None:
                if rsi >= 75:
                    score += 35
                elif rsi >= 70:
                    score += 28
                elif rsi >= 65:
                    score += 16

            if any(tok in rsi_zone for tok in ("OVERBOUGHT", "EXTREME_HIGH", "HIGH", "UPPER")):
                score += 18

            if any(tok in bb_zone for tok in ("ABOVE", "UPPER", "OVERBOUGHT")):
                score += 30

            if bb_position is not None:
                if bb_position >= 1.0:
                    score += 20
                elif bb_position >= 0.85:
                    score += 12

            if vwap_delta_pct is not None:
                if vwap_delta_pct >= 1.0:
                    score += 18
                elif vwap_delta_pct >= 0.5:
                    score += 10

        elif side == "BUY":
            if rsi is not None:
                if rsi <= 25:
                    score += 35
                elif rsi <= 30:
                    score += 28
                elif rsi <= 35:
                    score += 16

            if any(tok in rsi_zone for tok in ("OVERSOLD", "EXTREME_LOW", "LOW", "LOWER")):
                score += 18

            if any(tok in bb_zone for tok in ("BELOW", "LOWER", "OVERSOLD")):
                score += 30

            if bb_position is not None:
                if bb_position <= 0.0:
                    score += 20
                elif bb_position <= 0.15:
                    score += 12

            if vwap_delta_pct is not None:
                if vwap_delta_pct <= -1.0:
                    score += 18
                elif vwap_delta_pct <= -0.5:
                    score += 10

        market_context = cls._as_dict(context.get("market_context"))
        flip_risk = cls._upper(market_context.get("flip_risk"))
        entry_posture = cls._upper(market_context.get("entry_posture"))
        trend_phase = cls._upper(market_context.get("trend_phase"))

        if flip_risk == "HIGH":
            score += 10
        elif flip_risk == "MEDIUM":
            score += 5

        if entry_posture in ("WAIT", "CAUTION", "BLOCK"):
            score += 8

        if any(tok in trend_phase for tok in ("MATURE", "EXHAUST", "REVERSAL", "FAILED")):
            score += 8

        return score


    # ---------------------------------------------------------------------
    # Backend-safe resolver consumption
    # ---------------------------------------------------------------------

    @classmethod
    def signal_to_resolver_input(cls, signal: Any) -> Dict[str, Any]:
        """
        Convert a SignalSchema-like object into the same compact row shape that
        /signals/data uses before calling build_signal_context().

        This keeps AUTO trade gating aligned with the UI resolver without making
        TradeDecisionHelper duplicate resolver logic.
        """
        meta = cls._as_dict(getattr(signal, "meta_json", None))
        lifecycle = cls._as_dict(meta.get("lifecycle"))

        def _ev(value: Any) -> Any:
            return getattr(value, "value", value)

        return {
            "signal_id": cls._txt(getattr(signal, "signal_id", ""), ""),
            "symbol": cls._txt(getattr(signal, "symbol", ""), "").upper(),
            "lifecycle": cls._upper(_ev(getattr(signal, "lifecycle", ""))),
            "side": cls._upper(_ev(getattr(signal, "side", ""))),
            "stage": cls._upper(_ev(lifecycle.get("stage") or getattr(signal, "stage", ""))),
            "status": cls._upper(_ev(getattr(signal, "status", ""))),

            "confidence": cls._fnum(lifecycle.get("confidence"), 0.0) or 0.0,
            "quality": cls._upper(_ev(lifecycle.get("quality"))) or "LOW",

            # Signal decision vocabulary. These fields drive resolver action.
            "signal_action": cls._upper(_ev(lifecycle.get("signal_action"))),
            "signal_state": cls._upper(_ev(lifecycle.get("signal_state"))),
            "signal_reason": cls._txt(lifecycle.get("signal_reason"), ""),
            "trade_action": cls._upper(_ev(lifecycle.get("trade_action"))),

            "blocked_by_policy": bool(lifecycle.get("blocked_by_policy")),
            "blocked_by_policy_reason": cls._txt(lifecycle.get("blocked_by_policy_reason"), ""),

            "supports": lifecycle.get("supports") or [],
            "warnings": lifecycle.get("warnings") or [],
            "conflicts": lifecycle.get("conflicts") or [],
            "blocks": lifecycle.get("blocks") or [],
            "negative_cluster": lifecycle.get("negative_cluster") or [],
            "confidence_factors": lifecycle.get("confidence_factors") or [],

            "summary": cls._txt(lifecycle.get("summary"), ""),
            "reason": cls._txt(getattr(signal, "status_reason", "") or lifecycle.get("reason") or meta.get("reason"), ""),
            "reasons": lifecycle.get("reasons") or [],

            "first_seen_time": cls._txt(getattr(signal, "first_seen_time", ""), ""),
            "last_eval_time": cls._txt(getattr(signal, "last_eval_time", ""), ""),
            "last_snapshot_time": cls._txt(getattr(signal, "last_snapshot_time", ""), ""),
        }

    @classmethod
    def resolve_entry_permission(
        cls,
        *,
        signal: Any,
        peer_signals: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        Backend-safe entry permission based on the same resolver action shown in UI.

        A trade can be auto-created only when the resolver would show ENTER and
        the preferred signal is the same signal being evaluated.
        """
        snapshot = cls._as_dict(getattr(signal, "snapshot_json", None))
        symbol = cls._txt(getattr(signal, "symbol", ""), "").upper()
        ltp = cls._fnum(getattr(signal, "last_price", None), None)
        if ltp is None:
            ltp = cls._fnum(getattr(signal, "ltp", None), None)
        if ltp is None:
            ltp = cls._fnum(cls._get(snapshot, ["close"]), 0.0) or 0.0

        peers = peer_signals or [signal]
        rows = [cls.signal_to_resolver_input(s) for s in peers]

        resolved_row = cls.build_signal_context(
            symbol=symbol,
            ltp=ltp,
            snapshot_time=getattr(signal, "last_snapshot_time", None),
            snapshot=snapshot,
            signals=rows,
        )

        resolution = cls._as_dict(resolved_row.get("signal_resolution") or resolved_row.get("resolved"))
        action = cls._upper(resolution.get("action")) or "WATCH"
        preferred_signal_id = cls._txt(resolution.get("preferred_signal_id"), "")
        signal_id = cls._txt(getattr(signal, "signal_id", ""), "")

        allowed = bool(action == "ENTER" and preferred_signal_id == signal_id)

        if action != "ENTER":
            reason = f"resolver_action_{action.lower()}"
        elif preferred_signal_id != signal_id:
            reason = "resolver_prefers_different_signal"
        else:
            reason = "resolver_enter"

        return {
            "allowed": allowed,
            "action": action,
            "reason": reason,
            "preferred_signal_id": preferred_signal_id,
            "preferred_lifecycle": cls._upper(resolution.get("preferred_lifecycle")),
            "side": cls._upper(resolution.get("side")),
            "summary": cls._txt(resolution.get("summary"), ""),
            "conflict": bool(resolution.get("conflict")),
            "conflict_summary": cls._txt(resolution.get("conflict_summary"), ""),
            "candidates": resolution.get("candidates") or [],
            "lifecycle_statuses": resolution.get("lifecycle_statuses") or [],
        }

    # ---------------------------------------------------------------------
    # Mapping helpers
    # ---------------------------------------------------------------------

    @classmethod
    def _resolved_action(cls, row: Dict[str, Any]) -> str:
        signal_action = cls._upper(row.get("signal_action"))
        signal_state = cls._upper(row.get("signal_state"))
        stage = cls._upper(row.get("stage"))

        if signal_state in ("BLOCKED", "CLOSED", "REPLACED", "INVALIDATED", "EXPIRED", "CANCELLED"):
            return "WATCH"

        if signal_state in ("ACCEPTED", "READY") and signal_action in (
            "CREATE", "PROMOTE", "REPLACE_CREATE"
        ):
            return "ENTER"

        if signal_action == "HOLD":
            return "HOLD"

        if signal_action == "DOWNGRADE" or stage in ("WEAKENING", "EXIT_BIAS"):
            return "MANAGE"

        if stage == "FORCE_EXIT":
            return "EXIT"

        if signal_action == "REVIEW_OPPOSITE":
            return "WAIT"

        return "WATCH"

    @classmethod
    def _entry_view_from_lifecycle(
        cls,
        *,
        signal_action: str,
        signal_state: str,
        blocked_by_policy: bool,
        stage: str = "",
        legacy_signal_action: str = "",
        legacy_trade_action: str = "",
    ) -> str:
        if blocked_by_policy or signal_state == "BLOCKED":
            return "AVOID"

        if signal_state in ("CLOSED", "REPLACED", "INVALIDATED", "EXPIRED", "CANCELLED"):
            return "AVOID"

        if signal_state in ("ACCEPTED", "READY") and signal_action in (
            "CREATE", "PROMOTE", "REPLACE_CREATE"
        ):
            return "ENTER"

        # Compatibility for old rows only.
        if legacy_trade_action == "CREATE_TRADE" and legacy_signal_action == "ALLOW":
            return "ENTER"

        if signal_action == "REVIEW_OPPOSITE":
            return "REVIEW"

        if signal_action == "DOWNGRADE" or stage in ("WEAKENING", "EXIT_BIAS"):
            return "MANAGE"

        return "WATCH"

    @classmethod
    def _state_from_lifecycle(
        cls,
        *,
        stage: str,
        status: str,
        signal_action: str,
        signal_state: str,
        blocked_by_policy: bool,
    ) -> str:
        if blocked_by_policy or signal_state == "BLOCKED":
            return "BLOCKED"

        if status and status != "OPEN":
            return status

        if signal_state in ("CLOSED", "REPLACED", "INVALIDATED", "EXPIRED", "CANCELLED"):
            return signal_state

        if signal_state in ("ACCEPTED", "READY") and signal_action in (
            "CREATE", "PROMOTE", "REPLACE_CREATE"
        ):
            return "READY"

        if signal_state:
            return signal_state

        if stage in ("EXPAND", "ACTIVE"):
            return "ACCEPTED"

        if stage in ("DISCOVERY", "BUILDING"):
            return "TRACKING"

        if stage in ("WEAKENING", "PROTECT", "EXIT_BIAS", "FORCE_EXIT"):
            return "MANAGE"

        return "WATCH"

    @classmethod
    def _continuation_view_from_lifecycle(
        cls,
        *,
        stage: str,
        signal_action: str,
        signal_state: str,
    ) -> str:
        if signal_state in ("BLOCKED", "CLOSED", "REPLACED", "INVALIDATED", "EXPIRED", "CANCELLED"):
            return "NA"

        if signal_state in ("ACCEPTED", "READY") and signal_action in (
            "CREATE", "PROMOTE", "REPLACE_CREATE"
        ):
            return "ENTRY_OK"

        if signal_action == "HOLD":
            return "HOLD_OK"

        if signal_action == "DOWNGRADE":
            return "WEAKENING"

        if stage == "EXPAND":
            return "STRONG"

        if stage == "ACTIVE":
            return "HOLD_OK"


        return "NA"

    @classmethod
    def _rank_score(
        cls,
        *,
        stage: str,
        confidence: float,
        quality: str,
        signal_action: str,
        signal_state: str,
        blocked_by_policy: bool,
        block_count: int,
        conflict_count: int,
        market_view: Optional[Dict[str, Any]] = None,
    ) -> float:
        score = 0.0

        score += cls.SIGNAL_STATE_RANK.get(signal_state, 0) * 10
        score += cls.SIGNAL_DECISION_RANK.get(signal_action, 0) * 5
        score += cls.STAGE_RANK.get(stage, 0) * 2
        score += cls.QUALITY_RANK.get(quality, 0) * 10
        score += float(confidence or 0.0)

        mv = market_view or {}
        score += float(mv.get("rank_adjustment") or 0.0)

        if blocked_by_policy:
            score -= 1000

        if block_count:
            score -= block_count * 200

        if conflict_count:
            score -= conflict_count * 75

        return score

    @classmethod
    def _resolved_summary(
        cls,
        *,
        top: Dict[str, Any],
        action: str,
        conflict: bool,
        conflict_summary: str,
    ) -> str:
        lifecycle = cls._upper(top.get("lifecycle"))
        side = cls._upper(top.get("side"))
        stage = cls._upper(top.get("stage"))
        signal_action = cls._upper(top.get("signal_action"))
        signal_state = cls._upper(top.get("signal_state"))
        confidence = cls._fnum(top.get("confidence"), 0.0) or 0.0
        quality = cls._upper(top.get("quality"))
        market_view = cls._as_dict(top.get("market_view"))

        msg = (
            f"Prefer {lifecycle} {side}: {stage}, "
            f"state {signal_state}, signal {signal_action}, "
            f"confidence {confidence:.2f}, quality {quality}, action {action}."
        )

        mv_summary = cls._txt(market_view.get("summary"), "")
        if mv_summary:
            msg += f" Market context: {mv_summary}"

        if cls._txt(top.get("resolver_override"), "") == "TACTICAL_REVERSAL":
            score = cls._fnum(top.get("resolver_override_score"), 0.0) or 0.0
            msg += f" Tactical REVERSAL override active due to exhaustion score {score:.0f}."

        if conflict and conflict_summary:
            msg += f" {conflict_summary}"

        return msg

    @classmethod
    def _summary_from_signal(
        cls,
        *,
        lifecycle: str,
        side: str,
        stage: str,
        confidence: float,
        quality: str,
        signal_action: str,
        signal_state: str,
        blocked_by_policy: bool,
        reason: str,
        market_view: Optional[Dict[str, Any]] = None,
    ) -> str:
        msg = (
            f"{lifecycle} {side or '—'} is {stage or 'UNKNOWN'} "
            f"with confidence {confidence:.2f} ({quality}). "
            f"Signal={signal_action}, State={signal_state}."
        )

        mv_summary = cls._txt((market_view or {}).get("summary"), "")
        if mv_summary:
            msg += f" Market context: {mv_summary}"

        if blocked_by_policy:
            msg += " Policy block is active."

        if reason:
            msg += f" {reason}"

        return msg

    @classmethod
    def _candidate_summary(cls, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []

        for row in rows:
            mv = cls._as_dict(row.get("market_view"))
            mc = cls._as_dict(row.get("market_context"))

            out.append({
                "lifecycle": cls._upper(row.get("lifecycle")),
                "signal_id": cls._txt(row.get("signal_id"), ""),
                "side": cls._upper(row.get("side")),
                "stage": cls._upper(row.get("stage")),
                "status": cls._upper(row.get("status")),
                "state": cls._upper(row.get("state")),
                "entry_view": cls._upper(row.get("entry_view")),
                "continuation_view": cls._upper(row.get("continuation_view")),
                "confidence": cls._fnum(row.get("confidence"), 0.0),
                "quality": cls._upper(row.get("quality")),
                "signal_action": cls._upper(row.get("signal_action")),
                "signal_state": cls._upper(row.get("signal_state")),
                "signal_reason": cls._txt(row.get("signal_reason"), ""),
                "signal_action": cls._upper(row.get("signal_action")),
                "trade_action": cls._upper(row.get("trade_action")),
                "blocked_by_policy": bool(row.get("blocked_by_policy")),
                "reason": cls._txt(row.get("reason"), ""),
                "summary": cls._txt(row.get("summary"), ""),

                "market_direction": cls._upper(mc.get("direction")),
                "market_entry_posture": cls._upper(mc.get("entry_posture")),
                "market_quality": cls._fnum(mc.get("quality"), None),
                "market_flip_risk": cls._upper(mc.get("flip_risk")),
                "market_trend_phase": cls._upper(mc.get("trend_phase")),
                "market_alignment_to_side": cls._upper(mv.get("alignment_to_side")),
                "market_summary": cls._txt(mv.get("summary"), ""),
            })

        return out

    # ---------------------------------------------------------------------
    # Market context interpretation for resolver display
    # ---------------------------------------------------------------------

    @classmethod
    def _market_view_for_signal(
        cls,
        *,
        lifecycle: str,
        side: str,
        market_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        mc = cls._as_dict(market_context)
        if not mc:
            return {
                "alignment_to_side": "UNKNOWN",
                "rank_adjustment": 0.0,
                "summary": "Market context unavailable.",
            }

        side = cls._upper(side)
        lifecycle = cls._upper(lifecycle)

        direction = cls._upper(mc.get("direction"))
        posture = cls._upper(mc.get("entry_posture"))
        trend_phase = cls._upper(mc.get("trend_phase"))
        structure_phase = cls._upper(mc.get("structure_phase"))
        flip_risk = cls._upper(mc.get("flip_risk"))
        deriv_alignment = cls._upper(mc.get("derivatives_alignment"))
        deriv_transition = cls._upper(mc.get("derivatives_transition"))
        quality = cls._fnum(mc.get("quality"), None)

        if direction in ("BUY", "SELL") and side in ("BUY", "SELL"):
            if direction == side:
                alignment_to_side = "SUPPORTS_SIDE"
            else:
                alignment_to_side = "OPPOSES_SIDE"
        elif direction == "NEUTRAL":
            alignment_to_side = "NEUTRAL"
        else:
            alignment_to_side = "UNKNOWN"

        rank_adjustment = 0.0

        if alignment_to_side == "SUPPORTS_SIDE":
            rank_adjustment += 10
        elif alignment_to_side == "OPPOSES_SIDE":
            rank_adjustment -= 15

        rank_adjustment += cls.ENTRY_POSTURE_RANK.get(posture, 0)

        if quality is not None:
            if quality >= 70:
                rank_adjustment += 8
            elif quality < 35:
                rank_adjustment -= 12

        if flip_risk == "HIGH":
            rank_adjustment -= 8
        elif flip_risk == "LOW":
            rank_adjustment += 4

        parts = []

        if posture:
            parts.append(f"posture {posture}")
        if quality is not None:
            parts.append(f"quality {quality:.0f}")
        if alignment_to_side not in ("UNKNOWN", ""):
            parts.append(f"{alignment_to_side.lower()}")
        if trend_phase:
            parts.append(trend_phase)
        if structure_phase:
            parts.append(structure_phase)
        if flip_risk:
            parts.append(f"flip risk {flip_risk}")
        if deriv_alignment:
            parts.append(f"derivatives {deriv_alignment}")
        if deriv_transition and deriv_transition != "STABLE":
            parts.append(f"transition {deriv_transition}")

        if lifecycle == "REVERSAL" and alignment_to_side == "OPPOSES_SIDE":
            parts.append("market context may oppose the contra setup")

        if lifecycle == "REVERSAL" and alignment_to_side == "SUPPORTS_SIDE":
            parts.append("market context is favorable for the contra side")

        summary = "; ".join(parts) if parts else "Market context available."

        return {
            "alignment_to_side": alignment_to_side,
            "rank_adjustment": rank_adjustment,
            "summary": summary,
            "direction": direction,
            "entry_posture": posture,
            "quality": quality,
            "flip_risk": flip_risk,
            "trend_phase": trend_phase,
            "structure_phase": structure_phase,
            "derivatives_alignment": deriv_alignment,
            "derivatives_transition": deriv_transition,
        }

    # ---------------------------------------------------------------------
    # Context only, not decision logic
    # ---------------------------------------------------------------------

    @classmethod
    def _build_context(cls, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        structure = cls._get(snapshot, ["structure"], {}) or {}
        anchors = cls._get(snapshot, ["structure", "anchors"], {}) or {}
        breakout_context = cls._get(snapshot, ["structure", "breakout_context"], {}) or {}
        indicators = cls._get(snapshot, ["indicators"], {}) or {}
        state_context = cls._get(snapshot, ["state_context"], {}) or {}
        price_action = cls._get(snapshot, ["price_action"], {}) or {}
        market_context = cls._derive_market_context(snapshot)

        return {
            "hma_state": cls._txt(cls._get(indicators, ["hma", "state"]), "N/A"),
            "hma_strength": cls._txt(cls._get(indicators, ["hma", "strength"]), "N/A"),
            "rsi": cls._fnum(cls._get(indicators, ["rsi", "value"]), None),
            "rsi_zone": cls._txt(cls._get(indicators, ["rsi", "zone"]), "N/A"),
            "bb_zone": cls._txt(cls._get(indicators, ["bollinger", "zone"]), "UNKNOWN"),
            "bb_position": cls._fnum(cls._get(indicators, ["bollinger", "position"]), None),
            "bb_width": cls._fnum(cls._get(indicators, ["bollinger", "bb_width"]), None),
            "vwap": cls._fnum(cls._get(indicators, ["vwap", "value"]), None),
            "vwap_delta_pct": cls._fnum(cls._get(indicators, ["vwap", "distance_pct"]), None),
            "vwap_side": cls._txt(cls._get(indicators, ["vwap", "side"]), "UNKNOWN"),
            "atr": cls._fnum(cls._get(indicators, ["atr", "value"]), None),
            "atr_band": cls._txt(cls._get(indicators, ["atr", "band"]), "N/A"),
            "adx": cls._fnum(cls._get(indicators, ["adx", "value"]), None),
            "adx_band": cls._txt(cls._get(indicators, ["adx", "band"]), "N/A"),

            "structure_state": cls._txt(cls._get(structure, ["accepted", "state"]), "UNKNOWN"),
            "structure_side": cls._txt(cls._get(structure, ["raw", "side"]), "NEUTRAL"),
            "structure_raw_state": cls._txt(cls._get(structure, ["raw", "state"]), "UNKNOWN"),
            "structure_raw_side": cls._txt(cls._get(structure, ["raw", "side"]), "NEUTRAL"),
            "structure_count": cls._fnum(cls._get(structure, ["count"]), 0),
            "structure_candidate_state": "ACTIVE" if bool(cls._get(structure, ["candidate", "active"], False)) else "",
            "structure_candidate_count": cls._fnum(cls._get(structure, ["candidate", "bars_confirmed"]), 0),
            "structure_flip_count_today": cls._fnum(cls._get(structure, ["flip_count_today"]), 0),
            "structure_reason": cls._txt(cls._get(structure, ["reason"]), ""),

            "active_anchor": cls._txt(cls._get(anchors, ["active_anchor"]), "UNKNOWN"),
            "range_high": cls._fnum(cls._get(structure, ["accepted", "range", "high"]), None),
            "range_low": cls._fnum(cls._get(structure, ["accepted", "range", "low"]), None),
            "range_width_pct": cls._fnum(cls._get(structure, ["accepted", "range", "width_pct"]), None),
            "recent_swing_high": cls._fnum(cls._get(structure, ["raw", "recent_swing_high"]), None),
            "recent_swing_low": cls._fnum(cls._get(structure, ["raw", "recent_swing_low"]), None),
            "orb_high": cls._fnum(cls._get(anchors, ["orb_high"]), None),
            "orb_low": cls._fnum(cls._get(anchors, ["orb_low"]), None),
            "pdh": cls._fnum(cls._get(anchors, ["pdh"]), None),
            "pdl": cls._fnum(cls._get(anchors, ["pdl"]), None),
            "recent15_high": cls._fnum(cls._get(anchors, ["recent15_high"]), None),
            "recent15_low": cls._fnum(cls._get(anchors, ["recent15_low"]), None),
            "swing_status": cls._txt(cls._get(breakout_context, ["swing"]), "UNKNOWN"),
            "orb_status": cls._txt(cls._get(breakout_context, ["orb"]), "UNKNOWN"),
            "pdh_pdl_status": cls._txt(cls._get(breakout_context, ["pdh_pdl"]), "UNKNOWN"),
            "recent15_status": cls._txt(cls._get(breakout_context, ["recent15"]), "UNKNOWN"),
            "state_context": state_context,
            "price_action": price_action,
            "market_context": market_context,
        }

    @classmethod
    def _derive_market_context(cls, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        indicators = cls._get(snapshot, ["indicators"], {}) or {}
        state_context = cls._get(snapshot, ["state_context"], {}) or {}
        structure = cls._get(snapshot, ["structure"], {}) or {}
        price_action = cls._get(snapshot, ["price_action"], {}) or {}
        derivatives = cls._get(snapshot, ["derivatives"], {}) or {}

        hma_state = cls._upper(cls._get(indicators, ["hma", "state"]))
        hma_strength = cls._upper(cls._get(indicators, ["hma", "strength"]))
        structure_side = cls._upper(cls._get(structure, ["raw", "side"]))
        raw_structure_state = cls._upper(cls._get(structure, ["raw", "state"]))
        candidate_status = cls._upper(cls._get(structure, ["candidate", "status"]))
        accepted_state = cls._upper(cls._get(structure, ["accepted", "state"]))
        accepted_quality = cls._fnum(cls._get(structure, ["accepted", "quality"]), None)

        direction = hma_state if hma_state in ("BUY", "SELL") else structure_side if structure_side in ("BUY", "SELL") else "NEUTRAL"
        hma_flips = cls._fnum(cls._get(state_context, ["hma", "flip_count_today"]), 0) or 0
        structure_flips = cls._fnum(cls._get(state_context, ["structure", "flip_count_today"]), 0) or 0
        flip_total = hma_flips + structure_flips
        if flip_total >= 4:
            flip_risk = "HIGH"
        elif flip_total >= 2:
            flip_risk = "MEDIUM"
        else:
            flip_risk = "LOW"

        move_15m_atr = cls._fnum(cls._get(price_action, ["moves", "15m", "atr"]), None)
        slope_state = cls._upper(cls._get(price_action, ["slope", "state"]))
        entry_posture = "WAIT"
        if direction == "BUY" and slope_state in ("UP_ACCELERATING", "UP_SLOWING", "TURNING_UP"):
            entry_posture = "OK"
        elif direction == "SELL" and slope_state in ("DOWN_ACCELERATING", "DOWN_SLOWING", "TURNING_DOWN"):
            entry_posture = "OK"
        if move_15m_atr is not None and abs(move_15m_atr) >= 2.0:
            entry_posture = "CAUTION"

        opt15 = cls._upper(cls._get(derivatives, ["option_sentiment_windows", "15m", "indication"]))
        fut15 = cls._upper(cls._get(derivatives, ["future_sentiment_windows", "15m", "label"]))
        deriv_alignment = "UNKNOWN"
        if direction == "BUY" and (opt15 == "BULLISH" or fut15 == "LONG_BUILDUP"):
            deriv_alignment = "ALIGNED"
        elif direction == "SELL" and (opt15 == "BEARISH" or fut15 == "SHORT_BUILDUP"):
            deriv_alignment = "ALIGNED"
        elif opt15 or fut15:
            deriv_alignment = "MIXED"

        return {
            "direction": direction,
            "trend_phase": raw_structure_state or "UNKNOWN",
            "entry_posture": entry_posture,
            "quality": accepted_quality,
            "flip_risk": flip_risk,
            "hma_state_age": cls._fnum(cls._get(state_context, ["hma", "age_bars"]), None),
            "hma_strength_age": cls._fnum(cls._get(state_context, ["hma_strength", "age_bars"]), None),
            "structure_side_age": cls._fnum(cls._get(state_context, ["structure", "age_bars"]), None),
            "vwap_side_age": cls._fnum(cls._get(state_context, ["vwap", "age_bars"]), None),
            "structure_phase": candidate_status or accepted_state or raw_structure_state or "UNKNOWN",
            "derivatives_alignment": deriv_alignment,
            "derivatives_transition": "STABLE",
            "vwap_side": cls._upper(cls._get(indicators, ["vwap", "side"])),
            "bollinger_zone": cls._upper(cls._get(indicators, ["bollinger", "zone"])),
            "rsi_zone": cls._upper(cls._get(indicators, ["rsi", "zone"])),
            "adx_band": cls._upper(cls._get(indicators, ["adx", "band"])),
            "atr_band": cls._upper(cls._get(indicators, ["atr", "band"])),
            "hma_strength": hma_strength,
        }

    # ---------------------------------------------------------------------
    # Reason helpers
    # ---------------------------------------------------------------------

    @classmethod
    def _first_reason(cls, sig: Dict[str, Any]) -> str:
        for key in ("blocks", "conflicts", "warnings", "supports"):
            arr = sig.get(key) or []
            if not arr:
                continue

            first = arr[0]
            if isinstance(first, dict):
                return cls._txt(first.get("message") or first.get("key"), "")

            return cls._txt(first, "")

        return cls._txt(sig.get("reason") or sig.get("summary"), "")

    @classmethod
    def _reasons(cls, sig: Dict[str, Any]) -> List[str]:
        out = []

        for key in ("blocks", "conflicts", "warnings", "supports"):
            for item in sig.get(key) or []:
                if isinstance(item, dict):
                    msg = cls._txt(item.get("message") or item.get("key"), "")
                else:
                    msg = cls._txt(item, "")

                if msg:
                    out.append(msg)

        if not out:
            reason = cls._txt(sig.get("reason") or sig.get("summary"), "")
            if reason:
                out.append(reason)

        return out[:5]

    # ---------------------------------------------------------------------
    # Primitive helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _as_dict(x: Any) -> Dict[str, Any]:
        if not x:
            return {}

        if isinstance(x, dict):
            return x

        if isinstance(x, str):
            try:
                value = json.loads(x) or {}
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}

        return {}

    @staticmethod
    def _get(dct: Dict[str, Any], path: List[str], default: Any = None) -> Any:
        cur: Any = dct

        for key in path:
            if not isinstance(cur, dict):
                return default

            cur = cur.get(key)

        return default if cur is None else cur

    @staticmethod
    def _txt(v: Any, default: str = "") -> str:
        try:
            if v is None:
                return default

            text = str(v).strip()
            return text if text else default

        except Exception:
            return default

    @staticmethod
    def _upper(v: Any) -> str:
        return str(v or "").strip().upper()

    @staticmethod
    def _fnum(v: Any, default: Optional[float] = 0.0) -> Optional[float]:
        try:
            return float(v) if v is not None else default
        except Exception:
            return default
