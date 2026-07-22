"""Thin orthogonal Context Advisor for Phase 4A.1.

Local stock structure is intentionally excluded.  The Advisor consumes the
actual AutoTrades derivatives snapshot shape (option/future sentiment windows)
and converts absolute derivatives bias into candidate-relative advice.  NIFTY,
BANKNIFTY, VIX and sector remain explicit deferred placeholders.  Default
enforcement is LOG_ONLY.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Tuple

from configs.auction_engine_config import AuctionEngineConfig
from services.auction_engine.contracts import (
    AdvisorChannel,
    AdvisorDecision,
    AdvisorRecommendation,
    ContextAlignment,
    DirectionalBias,
    EvidenceSnapshot,
    QualityStatus,
    SetupCandidate,
    TradeSide,
)


class ContextAdvisor:
    def __init__(self, config: AuctionEngineConfig) -> None:
        self.config = config

    def evaluate(self, evidence: EvidenceSnapshot, candidate: SetupCandidate) -> AdvisorDecision:
        d = evidence.derivatives
        futures_relative = self._relative_alignment(d.futures_bias, candidate.side)
        options_relative = self._relative_alignment(d.options_bias, candidate.side)
        directional = tuple(
            value
            for value in (futures_relative, options_relative)
            if value in (ContextAlignment.SUPPORT, ContextAlignment.CONFLICT)
        )

        if d.quality.status in {QualityStatus.MISSING, QualityStatus.INVALID, QualityStatus.STALE}:
            recommendation = AdvisorRecommendation.WATCH
            combined = ContextAlignment.UNKNOWN
            reasons = ("DERIVATIVES_CONTEXT_UNAVAILABLE",)
        elif ContextAlignment.SUPPORT in directional and ContextAlignment.CONFLICT in directional:
            recommendation = AdvisorRecommendation.WATCH
            combined = ContextAlignment.NEUTRAL
            reasons = ("DERIVATIVES_CHANNELS_MIXED",)
        elif directional and all(value is ContextAlignment.CONFLICT for value in directional):
            recommendation = AdvisorRecommendation.BLOCK
            combined = ContextAlignment.CONFLICT
            reasons = ("DERIVATIVES_OPPOSE_SELECTED_SIDE",)
        elif directional and all(value is ContextAlignment.SUPPORT for value in directional):
            recommendation = AdvisorRecommendation.ALLOW
            combined = ContextAlignment.SUPPORT
            reasons = ("DERIVATIVES_SUPPORT_SELECTED_SIDE",)
        else:
            recommendation = AdvisorRecommendation.WATCH
            combined = ContextAlignment.NEUTRAL
            reasons = ("DERIVATIVES_CONTEXT_NEUTRAL_OR_PARTIAL",)

        futures_quality = self._channel_quality(d.futures_bias, d.futures_status)
        options_quality = self._channel_quality(d.options_bias, d.options_status)
        channels = (
            AdvisorChannel(
                name="FUTURES",
                alignment=futures_relative,
                quality=futures_quality,
                score=(d.futures_strength * 100.0) if d.futures_strength is not None else None,
                reason_codes=self._channel_reasons("FUTURES", futures_relative, d.futures_label),
                diagnostics={
                    "evaluation_status": "AVAILABLE" if d.futures_bias is not DirectionalBias.UNKNOWN else "PARTIAL",
                    "window": d.futures_window,
                    "status": d.futures_status,
                    "raw_label": d.futures_label,
                    "absolute_bias": d.futures_bias.value,
                    "candidate_relative_alignment": futures_relative.value,
                    "strength": d.futures_strength,
                    "fut_ltp_delta": d.futures_ltp_delta,
                    "fut_oi_delta": d.futures_oi_delta,
                    "futures_oi_change_pct": d.futures_oi_change_pct,
                    "basis_points": d.basis_points,
                },
            ),
            AdvisorChannel(
                name="OPTIONS",
                alignment=options_relative,
                quality=options_quality,
                score=(d.options_strength * 100.0) if d.options_strength is not None else None,
                reason_codes=self._channel_reasons("OPTIONS", options_relative, d.options_indication),
                diagnostics={
                    "evaluation_status": "AVAILABLE" if d.options_bias is not DirectionalBias.UNKNOWN else "PARTIAL",
                    "window": d.options_window,
                    "status": d.options_status,
                    "raw_indication": d.options_indication,
                    "absolute_bias": d.options_bias.value,
                    "candidate_relative_alignment": options_relative.value,
                    "strength": d.options_strength,
                    "pcr": d.pcr,
                    "pcr_delta": d.pcr_delta,
                },
            ),
            self._deferred_channel("NIFTY"),
            self._deferred_channel("BANKNIFTY"),
            self._deferred_channel("VIX"),
            self._deferred_channel("SECTOR"),
        )
        return AdvisorDecision(
            symbol=evidence.symbol,
            snapshot_time=evidence.snapshot_time,
            family=candidate.family,
            side=candidate.side,
            candidate_id=candidate.candidate_id,
            recommendation=recommendation,
            derivatives_alignment=combined,
            data_quality=d.quality.status,
            channels=channels,
            reason_codes=reasons,
            valid_until=evidence.snapshot_time + timedelta(minutes=self.config.advisor.watch_valid_minutes),
            observation_only=True,
            diagnostics={
                "enforcement_mode": self.config.advisor.enforcement_mode,
                "local_stock_context_recomputed": False,
                "derivatives_contract": "DERIVATIVESCHAIN_V2_DERIVED_WINDOWS",
                "futures_bias": d.futures_bias.value,
                "options_bias": d.options_bias.value,
                "futures_window": d.futures_window,
                "options_window": d.options_window,
                "raw_derivatives_diagnostics": d.raw_diagnostics,
            },
            config_version=self.config.engine.config_version,
        )

    @staticmethod
    def _relative_alignment(bias: DirectionalBias, side: TradeSide) -> ContextAlignment:
        if bias in (DirectionalBias.UNKNOWN, DirectionalBias.MIXED):
            return ContextAlignment.UNKNOWN
        if bias is DirectionalBias.NEUTRAL:
            return ContextAlignment.NEUTRAL
        candidate_up = side is TradeSide.BUY
        supports = (candidate_up and bias is DirectionalBias.UP) or (
            not candidate_up and bias is DirectionalBias.DOWN
        )
        return ContextAlignment.SUPPORT if supports else ContextAlignment.CONFLICT

    @staticmethod
    def _channel_quality(bias: DirectionalBias, status: str | None) -> QualityStatus:
        if str(status or "").upper() == "ERROR":
            return QualityStatus.INVALID
        if bias is DirectionalBias.UNKNOWN:
            return QualityStatus.PARTIAL if status else QualityStatus.MISSING
        return QualityStatus.GOOD

    @staticmethod
    def _channel_reasons(prefix: str, alignment: ContextAlignment, raw_value: str | None) -> Tuple[str, ...]:
        if not raw_value:
            return (f"{prefix}_SENTIMENT_MISSING",)
        if alignment is ContextAlignment.SUPPORT:
            return (f"{prefix}_SUPPORTS_SELECTED_SIDE",)
        if alignment is ContextAlignment.CONFLICT:
            return (f"{prefix}_OPPOSES_SELECTED_SIDE",)
        if alignment is ContextAlignment.NEUTRAL:
            return (f"{prefix}_NEUTRAL",)
        return (f"{prefix}_DIRECTION_UNKNOWN",)

    @staticmethod
    def _deferred_channel(name: str) -> AdvisorChannel:
        return AdvisorChannel(
            name=name,
            alignment=ContextAlignment.UNKNOWN,
            quality=QualityStatus.UNKNOWN,
            reason_codes=("DEFERRED_TO_PHASE4B",),
            diagnostics={"evaluation_status": "NOT_EVALUATED"},
        )


__all__ = ["ContextAdvisor"]
