"""Causal setup-candidate engine for the Auction pipeline.

The setup engine interprets the already-causal Evidence Ledger, persistent
Auction State and immutable Unified Boundary Episode.  It does **not** discover
its own boundaries, mutate auction state, call external context, select a winning
setup, persist state or create a signal.

Active candidate families
---------------------------
* BREAKOUT_INITIATION -- the early displacement + immediate hold/retest path.
* ACCEPTED_BREAKOUT -- a resolved accepted boundary episode.
* FAILED_BREAKOUT -- a resolved failed episode, context-classified as
  trend-aligned, neutral-range or countertrend.
* CONTINUATION -- an accepted new-range breakout aligned with an established
  orderly trend.
* REVERSAL -- a confirmed opposite trend after a persistent TREND_FAILURE
  episode, classified as NORMAL_REVERSAL or EXHAUSTION_REVERSAL.

Candidates are deliberately reported even when WATCH/INELIGIBLE so later
outcome analysis can identify false negatives without weakening the causal
rules.  Candidate identity is stable across a short watch lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from services.auction_engine.contracts import (
    AuctionState,
    AuctionStateName,
    BoundaryEpisode,
    BoundaryEpisodeStatus,
    BoundaryResolution,
    BoundarySide,
    CandidateEligibility,
    CandidateRole,
    ConfidenceChannel,
    DirectionalBias,
    EvidenceFact,
    EvidencePolarity,
    EvidenceSnapshot,
    QualityStatus,
    SetupCandidate,
    SetupFamily,
    TradeSide,
    stable_key,
)


@dataclass
class _InitiationWatch:
    symbol: str
    candidate_id: str
    event_key: str
    side: TradeSide
    event_time: datetime
    attempt_time: datetime
    boundary_price: float
    subtype: str
    pre_attempt_state: AuctionStateName
    state_at_attempt: AuctionStateName
    established_trend_side_at_attempt: str
    pause_context_at_attempt: bool
    recent_balance_context_at_attempt: bool
    max_close_outside_atr: float
    max_outside_excursion_atr: float
    source_boundary_status: BoundaryEpisodeStatus
    source_boundary_resolution: BoundaryResolution
    source_boundary_resolution_basis: Optional[str]
    source_boundary_id: str
    source_boundary_side: BoundarySide
    source_boundary_source: str
    source_frozen_range_id: str
    source_frozen_range_version: int
    source_frozen_range_low: float
    source_frozen_range_high: float
    expires_at: datetime
    emitted_terminal: bool = False


@dataclass
class _FailedWatch:
    symbol: str
    candidate_id: str
    event_key: str
    side: TradeSide
    subtype: str
    event_time: datetime
    failed_time: datetime
    resolution_price: float
    boundary_price: float
    frozen_low: float
    frozen_high: float
    source_boundary_status: BoundaryEpisodeStatus
    source_boundary_resolution: BoundaryResolution
    source_boundary_id: str
    source_boundary_side: BoundarySide
    source_boundary_source: str
    source_frozen_range_id: str
    source_frozen_range_version: int
    resolution_basis: str
    state_at_failure: AuctionStateName
    expires_at: datetime
    renewed_acceptance_closes: int = 0
    emitted_terminal: bool = False


class SetupCandidateEngine:
    """Causal in-memory setup interpretation for one chronological replay."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.version = config.engine.config_version
        self._initiation: Dict[str, _InitiationWatch] = {}
        self._failed: Dict[str, _FailedWatch] = {}
        self._emitted_once: Set[str] = set()
        self._completed: Set[str] = set()
        self._last_time: Dict[str, datetime] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._initiation.clear()
            self._failed.clear()
            self._emitted_once.clear()
            self._completed.clear()
            self._last_time.clear()
            return
        key = str(symbol).strip().upper()
        self._last_time.pop(key, None)
        self._initiation = {
            cid: item for cid, item in self._initiation.items()
            if item.symbol != key
        }
        self._failed = {
            cid: item for cid, item in self._failed.items()
            if item.symbol != key
        }
        # Completed one-shot identities include the boundary event key (which
        # itself includes trading-day identity), so they cannot collide with a
        # later replay day.

    def evaluate(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        boundary_episode: Optional[BoundaryEpisode],
        *,
        state_diagnostics: Optional[Mapping[str, Any]] = None,
        closed_episode: Optional[BoundaryEpisode] = None,
    ) -> Tuple[SetupCandidate, ...]:
        symbol = evidence.symbol
        ts = evidence.snapshot_time
        prior = self._last_time[symbol] if symbol in self._last_time else None
        if prior is not None and ts.date() != prior.date():
            self.reset(symbol)
        self._last_time[symbol] = ts
        state_diag = dict(state_diagnostics or {})

        candidates: List[SetupCandidate] = []

        # Administrative same-snapshot closures do not normally create setup
        # candidates.  A resolved episode was already emitted at resolution;
        # SUPERSEDED/STALE/EXPIRED are objective lifecycle endings only.
        if closed_episode is not None and closed_episode.resolution in {
            BoundaryResolution.ACCEPTED,
            BoundaryResolution.FAILED,
        }:
            candidates.extend(
                self._resolution_candidates(
                    evidence,
                    auction_state,
                    closed_episode,
                    state_diag,
                    allow_only_at_resolution=True,
                )
            )

        if boundary_episode is not None:
            self._register_initiation_watch(
                evidence,
                auction_state,
                boundary_episode,
                state_diag,
            )
            candidates.extend(
                self._resolution_candidates(
                    evidence,
                    auction_state,
                    boundary_episode,
                    state_diag,
                    allow_only_at_resolution=True,
                )
            )
            self._register_failed_watch(
                evidence, auction_state, boundary_episode, state_diag
            )

        candidates.extend(
            self._evaluate_initiation_watches(
                evidence, auction_state, boundary_episode, state_diag
            )
        )
        candidates.extend(
            self._evaluate_failed_watches(
                evidence, auction_state, boundary_episode, state_diag
            )
        )
        candidates.extend(
            self._reversal_candidates(
                evidence, auction_state, state_diag
            )
        )

        # Stable deterministic order makes replay/report comparisons simple.
        candidates.sort(key=lambda item: (item.family.value, item.subtype, item.candidate_id))
        return tuple(candidates)

    # ------------------------------------------------------------------
    # Breakout initiation
    # ------------------------------------------------------------------
    def _register_initiation_watch(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        episode: BoundaryEpisode,
        state_diag: Mapping[str, Any],
    ) -> None:
        if not self.config.initiation.enabled or episode.attempt_time is None:
            return
        if episode.event_key in self._completed:
            return
        subtype, established_at_attempt, pause_context, recent_balance_context = (
            self._initial_initiation_context(
                episode.breakout_side,
                auction_state,
                state_diag,
            )
        )
        candidate_id = self._candidate_id(
            "INIT",
            evidence.symbol,
            episode.event_key,
            SetupFamily.BREAKOUT_INITIATION,
            subtype,
        )
        if candidate_id in self._initiation or candidate_id in self._completed:
            return
        # Register only on the actual attempt snapshot.  Later terminal-protected
        # rows must not invent an initiation candidate after the opportunity.
        if evidence.snapshot_time != episode.attempt_time:
            return
        interval = self.config.engine.snapshot_interval_minutes
        window_bars = self.config.initiation.confirmation_window_bars
        pre_state = auction_state.previous_state
        current_state = auction_state.current_state
        self._initiation[candidate_id] = _InitiationWatch(
            symbol=evidence.symbol,
            candidate_id=candidate_id,
            event_key=episode.event_key,
            side=episode.breakout_side,
            event_time=episode.event_time,
            attempt_time=episode.attempt_time,
            boundary_price=episode.boundary_price,
            subtype=subtype,
            pre_attempt_state=pre_state,
            state_at_attempt=current_state,
            established_trend_side_at_attempt=established_at_attempt,
            pause_context_at_attempt=pause_context,
            recent_balance_context_at_attempt=recent_balance_context,
            max_close_outside_atr=episode.max_close_outside_atr,
            max_outside_excursion_atr=episode.max_outside_excursion_atr,
            source_boundary_status=episode.status,
            source_boundary_resolution=episode.resolution,
            source_boundary_resolution_basis=(
                str(episode.diagnostics["failure_resolution_basis"])
                if episode.diagnostics["failure_resolution_basis"] is not None
                else None
            ),
            source_boundary_id=episode.boundary_id,
            source_boundary_side=episode.boundary_side,
            source_boundary_source=episode.boundary_source,
            source_frozen_range_id=episode.frozen_range.range_id,
            source_frozen_range_version=episode.frozen_range.range_version,
            source_frozen_range_low=episode.frozen_range.low,
            source_frozen_range_high=episode.frozen_range.high,
            expires_at=episode.attempt_time + timedelta(minutes=interval * window_bars),
        )

    def _evaluate_initiation_watches(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        episode: Optional[BoundaryEpisode],
        state_diag: Mapping[str, Any],
    ) -> List[SetupCandidate]:
        out: List[SetupCandidate] = []
        for candidate_id, watch in list(self._initiation.items()):
            if watch.emitted_terminal or watch.event_key in self._completed:
                self._initiation.pop(candidate_id, None)
                continue
            if evidence.symbol != watch.symbol:
                continue
            age_minutes = max(0.0, (evidence.snapshot_time - watch.attempt_time).total_seconds() / 60.0)
            age_bars = int(round(age_minutes / self.config.engine.snapshot_interval_minutes))
            same_episode = episode is not None and episode.event_key == watch.event_key

            terminal_status = episode.status if same_episode else None
            blockers, dynamic_watch = self._common_breakout_blockers(
                evidence,
                auction_state,
                side=watch.side,
                boundary_price=watch.boundary_price,
                event_time=watch.attempt_time,
                state_diag=state_diag,
                max_entry_distance_atr=self.config.initiation.max_entry_distance_atr,
                minimum_session_minutes=self.config.initiation.minimum_session_minutes,
            )

            strong_displacement = (
                watch.max_close_outside_atr >= self.config.initiation.minimum_close_outside_atr
                and watch.max_outside_excursion_atr >= self.config.initiation.minimum_displacement_atr
            )
            if not strong_displacement:
                blockers.append("INITIATION_STRONG_DISPLACEMENT_NOT_CONFIRMED")

            recent_valid_balance = watch.pre_attempt_state in {
                AuctionStateName.BALANCE,
                AuctionStateName.COMPRESSION,
                AuctionStateName.BOUNDARY_INTERACTION,
            } or watch.state_at_attempt in {
                AuctionStateName.BOUNDARY_INTERACTION,
                AuctionStateName.FRESH_EXPANSION,
            }
            if self.config.initiation.require_recent_valid_balance and not recent_valid_balance:
                blockers.append("INITIATION_RECENT_VALID_BALANCE_NOT_CONFIRMED")

            current_offset = self._signed_offset_atr(
                evidence.close, watch.boundary_price, watch.side, evidence.atr
            )
            immediate_hold = (
                age_bars >= 1
                and current_offset is not None
                and current_offset >= -self.config.initiation.shallow_retest_tolerance_atr
            )
            outside_reclaimed = current_offset is not None and current_offset >= 0.0
            if age_bars == 0:
                blockers.append("INITIATION_WAITING_FOR_IMMEDIATE_HOLD_OR_RETEST")
                dynamic_watch = True
            elif not immediate_hold:
                blockers.append("INITIATION_IMMEDIATE_HOLD_OR_RETEST_FAILED")
            elif not outside_reclaimed:
                # A shallow retest is valid WATCH evidence, but CREATE must wait
                # until the close is back on the breakout side of the boundary.
                blockers.append("INITIATION_WAITING_FOR_OUTSIDE_RECLAIM")
                dynamic_watch = True

            if same_episode and terminal_status is BoundaryEpisodeStatus.FAILED:
                blockers.append("INITIATION_BOUNDARY_ATTEMPT_FAILED")
            if same_episode and terminal_status in {
                BoundaryEpisodeStatus.EXPIRED,
                BoundaryEpisodeStatus.SUPERSEDED,
                BoundaryEpisodeStatus.STALE,
            }:
                blockers.append(f"INITIATION_EPISODE_{terminal_status.value}")

            if evidence.snapshot_time > watch.expires_at:
                eligibility = CandidateEligibility.EXPIRED
                blockers.append("INITIATION_CONFIRMATION_WINDOW_EXPIRED")
                terminal = True
            elif age_bars == 0:
                eligibility = CandidateEligibility.WATCH
                terminal = False
            elif blockers:
                # Location/state blockers can clear only inside the short
                # initiation window. Structural failure is terminal.
                hard = any(code in blockers for code in (
                    "INITIATION_BOUNDARY_ATTEMPT_FAILED",
                    "INITIATION_IMMEDIATE_HOLD_OR_RETEST_FAILED",
                )) or any(code.startswith("INITIATION_EPISODE_") for code in blockers)
                eligibility = CandidateEligibility.INELIGIBLE if hard else CandidateEligibility.WATCH
                terminal = hard
            else:
                eligibility = CandidateEligibility.ELIGIBLE
                terminal = True

            candidate = self._candidate(
                evidence,
                auction_state,
                candidate_id=candidate_id,
                family=SetupFamily.BREAKOUT_INITIATION,
                subtype=watch.subtype,
                candidate_role=CandidateRole.EARLY_INITIATION,
                side=watch.side,
                event_key=watch.event_key,
                event_time=watch.event_time,
                candidate_time=watch.attempt_time,
                boundary_price=watch.boundary_price,
                source_boundary=self._source_boundary_from_initiation_watch(watch),
                eligibility=eligibility,
                blockers=blockers,
                terminal=terminal,
                valid_until=None if terminal else watch.expires_at,
                target_basis="OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET",
                diagnostics={
                    **self._breakout_reference_diagnostics(
                        evidence, watch.side, watch.boundary_price,
                        self._source_boundary_from_initiation_watch(watch),
                    ),
                    "candidate_stage": "ATTEMPT" if age_bars == 0 else "IMMEDIATE_CONFIRMATION",
                    "age_bars": age_bars,
                    "age_minutes": age_minutes,
                    "pre_attempt_state": watch.pre_attempt_state.value,
                    "state_at_attempt": watch.state_at_attempt.value,
                    "frozen_subtype": watch.subtype,
                    "established_trend_side_at_attempt": watch.established_trend_side_at_attempt,
                    "pause_context_at_attempt": watch.pause_context_at_attempt,
                    "recent_balance_context_at_attempt": watch.recent_balance_context_at_attempt,
                    "recent_valid_balance": recent_valid_balance,
                    "strong_displacement": strong_displacement,
                    "immediate_hold_or_shallow_retest": immediate_hold,
                    "outside_reclaimed": outside_reclaimed,
                    "current_boundary_offset_atr": current_offset,
                    "originated_before_acceptance": True,
                    "dynamic_watch": dynamic_watch,
                },
            )
            out.append(candidate)
            if terminal:
                watch.emitted_terminal = True
                self._completed.add(watch.event_key)
                self._initiation.pop(candidate_id, None)
        return out

    # ------------------------------------------------------------------
    # Accepted and continuation candidates
    # ------------------------------------------------------------------
    def _resolution_candidates(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        episode: BoundaryEpisode,
        state_diag: Mapping[str, Any],
        *,
        allow_only_at_resolution: bool,
    ) -> List[SetupCandidate]:
        out: List[SetupCandidate] = []
        resolution_time = episode.accepted_time or episode.failed_time
        if allow_only_at_resolution and resolution_time != evidence.snapshot_time:
            return out
        if episode.resolution is BoundaryResolution.ACCEPTED and self.config.acceptance.enabled:
            accepted_id = self._candidate_id(
                "ACCEPT", evidence.symbol, episode.event_key,
                SetupFamily.ACCEPTED_BREAKOUT, "ACCEPTED_BREAKOUT",
            )
            if accepted_id not in self._emitted_once:
                self._emitted_once.add(accepted_id)
                out.append(
                    self._accepted_candidate(
                        evidence, auction_state, episode, state_diag, accepted_id
                    )
                )

            if self.config.continuation.enabled and self.config.continuation.boundary_continuation_enabled:
                continuation_id = self._candidate_id(
                    "CONT", evidence.symbol, episode.event_key,
                    SetupFamily.CONTINUATION, "BOUNDARY_CONTINUATION_ACCEPTANCE",
                )
                if continuation_id not in self._emitted_once:
                    self._emitted_once.add(continuation_id)
                    out.append(
                        self._continuation_candidate(
                            evidence, auction_state, episode, state_diag, continuation_id
                        )
                    )
        return out

    def _accepted_candidate(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        episode: BoundaryEpisode,
        state_diag: Mapping[str, Any],
        candidate_id: str,
    ) -> SetupCandidate:
        blockers, _ = self._common_breakout_blockers(
            evidence,
            auction_state,
            side=episode.breakout_side,
            boundary_price=episode.boundary_price,
            event_time=episode.attempt_time or episode.event_time,
            state_diag=state_diag,
            max_entry_distance_atr=self.config.acceptance.max_entry_distance_atr,
            minimum_session_minutes=self.config.acceptance.minimum_session_minutes,
        )
        if self.config.acceptance.require_hold_or_retest and not (
            episode.retest_detected
            or episode.consecutive_outside_closes >= self.config.boundary.acceptance_required_outside_closes
        ):
            blockers.append("ACCEPTED_HOLD_OR_RETEST_NOT_CONFIRMED")
        if self.config.acceptance.require_value_beyond_boundary and episode.max_close_outside_atr < self.config.boundary.acceptance_close_buffer_atr:
            blockers.append("ACCEPTED_VALUE_BEYOND_BOUNDARY_NOT_CONFIRMED")
        first_move_consumed = False
        subtype = (
            "CONTINUATION_ACCEPTANCE"
            if self._side_matches_established_trend(episode.breakout_side, state_diag)
            else "FRESH_ACCEPTANCE"
        )
        return self._candidate(
            evidence,
            auction_state,
            candidate_id=candidate_id,
            family=SetupFamily.ACCEPTED_BREAKOUT,
            subtype=subtype,
            candidate_role=CandidateRole.ACCEPTED_RESOLUTION_ENTRY,
            side=episode.breakout_side,
            event_key=episode.event_key,
            event_time=episode.event_time,
            candidate_time=episode.accepted_time or evidence.snapshot_time,
            boundary_price=episode.boundary_price,
            source_boundary=self._source_boundary_from_episode(episode),
            eligibility=(CandidateEligibility.INELIGIBLE if blockers else CandidateEligibility.ELIGIBLE),
            blockers=blockers,
            terminal=True,
            valid_until=None,
            first_move_consumed=first_move_consumed,
            target_basis="OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET",
            diagnostics={
                **self._breakout_reference_diagnostics(
                    evidence, episode.breakout_side, episode.boundary_price,
                    self._source_boundary_from_episode(episode),
                ),
                "resolution": "ACCEPTED",
                "episode_status": episode.status.value,
                "max_close_outside_atr": episode.max_close_outside_atr,
                "outside_closes": episode.total_outside_closes,
                "retest_detected": episode.retest_detected,
                "same_side_established_trend": self._side_matches_established_trend(
                    episode.breakout_side, state_diag
                ),
            },
        )

    def _continuation_candidate(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        episode: BoundaryEpisode,
        state_diag: Mapping[str, Any],
        candidate_id: str,
    ) -> SetupCandidate:
        blockers, _ = self._common_breakout_blockers(
            evidence,
            auction_state,
            side=episode.breakout_side,
            boundary_price=episode.boundary_price,
            event_time=episode.attempt_time or episode.event_time,
            state_diag=state_diag,
            max_entry_distance_atr=self.config.continuation.max_entry_distance_atr,
            minimum_session_minutes=self.config.continuation.minimum_session_minutes,
        )
        aligned = self._side_matches_established_trend(episode.breakout_side, state_diag)
        if self.config.continuation.require_established_orderly_trend and not aligned:
            blockers.append("CONTINUATION_ESTABLISHED_TREND_NOT_ALIGNED")
        if self.config.continuation.require_retained_trend_structure and evidence.trend.retained_structure is False:
            blockers.append("CONTINUATION_TREND_STRUCTURE_NOT_RETAINED")
        pause_context = auction_state.previous_state in {
            AuctionStateName.CONTROLLED_PULLBACK,
            AuctionStateName.RECOMPRESSION,
            AuctionStateName.BALANCE,
            AuctionStateName.COMPRESSION,
        } or auction_state.current_state in {
            AuctionStateName.REACCELERATION,
            AuctionStateName.ORDERLY_UPTREND,
            AuctionStateName.ORDERLY_DOWNTREND,
            AuctionStateName.FRESH_EXPANSION,
        }
        if self.config.continuation.require_controlled_pullback_or_recompression and not pause_context:
            blockers.append("CONTINUATION_PAUSE_CONTEXT_NOT_CONFIRMED")
        if self.config.continuation.require_fresh_price_action_displacement and episode.max_close_outside_atr < self.config.boundary.acceptance_close_buffer_atr:
            blockers.append("CONTINUATION_FRESH_DISPLACEMENT_NOT_CONFIRMED")
        return self._candidate(
            evidence,
            auction_state,
            candidate_id=candidate_id,
            family=SetupFamily.CONTINUATION,
            subtype="BOUNDARY_CONTINUATION_ACCEPTANCE",
            candidate_role=CandidateRole.CONTINUATION_INTERPRETATION,
            side=episode.breakout_side,
            event_key=episode.event_key,
            event_time=episode.event_time,
            candidate_time=episode.accepted_time or evidence.snapshot_time,
            boundary_price=episode.boundary_price,
            source_boundary=self._source_boundary_from_episode(episode),
            eligibility=(CandidateEligibility.INELIGIBLE if blockers else CandidateEligibility.ELIGIBLE),
            blockers=blockers,
            terminal=True,
            valid_until=None,
            target_basis="OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET",
            diagnostics={
                **self._breakout_reference_diagnostics(
                    evidence, episode.breakout_side, episode.boundary_price,
                    self._source_boundary_from_episode(episode),
                ),
                "resolution": "ACCEPTED",
                "same_side_established_trend": aligned,
                "pause_context": pause_context,
            },
        )

    # ------------------------------------------------------------------
    # Failed-auction watch and subtype classification
    # ------------------------------------------------------------------
    def _register_failed_watch(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        episode: BoundaryEpisode,
        state_diag: Mapping[str, Any],
    ) -> None:
        if not self.config.failure.enabled:
            return
        if episode.resolution is not BoundaryResolution.FAILED or episode.failed_time is None:
            return
        if episode.failed_time != evidence.snapshot_time:
            return
        subtype = self._failed_subtype(episode.failure_side, auction_state, state_diag)
        candidate_id = self._candidate_id(
            "FAIL", evidence.symbol, episode.event_key,
            SetupFamily.FAILED_BREAKOUT, subtype,
        )
        if candidate_id in self._failed or candidate_id in self._completed:
            return
        self._failed[candidate_id] = _FailedWatch(
            symbol=evidence.symbol,
            candidate_id=candidate_id,
            event_key=episode.event_key,
            side=episode.failure_side,
            subtype=subtype,
            event_time=episode.event_time,
            failed_time=episode.failed_time,
            resolution_price=evidence.close,
            boundary_price=episode.boundary_price,
            frozen_low=episode.frozen_range.low,
            frozen_high=episode.frozen_range.high,
            source_boundary_status=episode.status,
            source_boundary_resolution=episode.resolution,
            source_boundary_id=episode.boundary_id,
            source_boundary_side=episode.boundary_side,
            source_boundary_source=episode.boundary_source,
            source_frozen_range_id=episode.frozen_range.range_id,
            source_frozen_range_version=episode.frozen_range.range_version,
            resolution_basis=_required_failure_resolution_basis(episode),
            state_at_failure=auction_state.current_state,
            expires_at=episode.failed_time + timedelta(minutes=self.config.boundary.failure_watch_valid_minutes),
        )

    def _evaluate_failed_watches(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        episode: Optional[BoundaryEpisode],
        state_diag: Mapping[str, Any],
    ) -> List[SetupCandidate]:
        out: List[SetupCandidate] = []
        for candidate_id, watch in list(self._failed.items()):
            if evidence.symbol != watch.symbol:
                continue
            if watch.emitted_terminal:
                self._failed.pop(candidate_id, None)
                continue
            age_minutes = max(0.0, (evidence.snapshot_time - watch.failed_time).total_seconds() / 60.0)
            favorable_atr = self._signed_progress_atr(
                evidence.close, watch.resolution_price, watch.side, evidence.atr
            )
            outside_again = self._signed_offset_atr(
                evidence.close, watch.boundary_price, watch.side.opposite, evidence.atr
            )
            # For a failed upper breakout, renewed acceptance is BUY/outside;
            # for a failed lower breakout it is SELL/outside.  ``side.opposite``
            # is therefore the original breakout side.
            if outside_again is not None and outside_again >= self.config.boundary.acceptance_close_buffer_atr:
                watch.renewed_acceptance_closes += 1
            else:
                watch.renewed_acceptance_closes = 0

            blockers: List[str] = []
            dynamic_watch = False
            state_blockers = self._state_blockers(
                auction_state.current_state, watch.side, state_diag, prefix="FAILED"
            )
            # Preserve the auction context at the original failed resolution.
            # A short-lived UNKNOWN state caused by terminal boundary reset must
            # not erase an otherwise valid frozen-event interpretation.
            if (
                "FAILED_STATE_UNKNOWN" in state_blockers
                and watch.state_at_failure is not AuctionStateName.UNKNOWN
            ):
                state_blockers.remove("FAILED_STATE_UNKNOWN")
            blockers.extend(state_blockers)

            room = self._failed_room(evidence, watch)
            if room[1] is None or room[1] < self.config.failure.minimum_room_atr:
                blockers.append("FAILED_OPPOSITE_RANGE_EDGE_ROOM_ATR_INSUFFICIENT")
            if room[2] is None or room[2] < self.config.failure.minimum_room_pct:
                blockers.append("FAILED_OPPOSITE_RANGE_EDGE_ROOM_BELOW_MINIMUM_PCT")
            first_move_consumed = bool(room[1] is not None and room[1] <= 1e-9)
            if first_move_consumed:
                blockers.append("FAILED_RETURN_TO_RANGE_ALREADY_COMPLETE")

            followthrough_confirmed = (
                watch.resolution_basis == "DIRECTIONAL_FOLLOWTHROUGH"
                or (favorable_atr is not None and favorable_atr >= self.config.failure.post_resolution_followthrough_atr)
            )
            if self.config.failure.require_directional_followthrough and not followthrough_confirmed:
                blockers.append("FAILED_DIRECTIONAL_FOLLOWTHROUGH_PENDING")
                dynamic_watch = True

            if watch.subtype == "COUNTERTREND_FAILED_AUCTION":
                blockers.append("FAILED_COUNTERTREND_POLICY_DEFERRED")
                dynamic_watch = True

            create_window = self._create_window_position(evidence.snapshot_time)
            if create_window == "BEFORE":
                blockers.append("FAILED_CREATE_WINDOW_NOT_OPEN")
                dynamic_watch = True
            elif create_window == "AFTER":
                blockers.append("FAILED_INSUFFICIENT_SESSION_TIME")

            # Terminal conditions must be resolved before a non-terminal
            # renewed-outside WATCH.  Otherwise a watch whose valid window has
            # already elapsed can be emitted as active with valid_until in the
            # past, violating the candidate contract.
            hard_blockers = [
                code for code in blockers
                if code in {
                    "FAILED_RETURN_TO_RANGE_ALREADY_COMPLETE",
                    "FAILED_INSUFFICIENT_SESSION_TIME",
                }
            ]
            if watch.renewed_acceptance_closes >= self.config.boundary.acceptance_required_outside_closes:
                blockers.append("FAILED_LEVEL_REACCEPTED_OUTSIDE")
                eligibility = CandidateEligibility.INELIGIBLE
                terminal = True
            elif evidence.snapshot_time > watch.expires_at:
                blockers.append("FAILED_WATCH_WINDOW_EXPIRED")
                eligibility = CandidateEligibility.EXPIRED
                terminal = True
            elif hard_blockers:
                eligibility = CandidateEligibility.INELIGIBLE
                terminal = True
            elif watch.renewed_acceptance_closes >= 1:
                # The first renewed outside close blocks entry immediately but
                # remains non-terminal until configured reacceptance confirms.
                blockers.append("FAILED_RENEWED_OUTSIDE_ENTRY_BLOCKED")
                eligibility = CandidateEligibility.WATCH
                terminal = False
                dynamic_watch = True
            elif blockers:
                eligibility = CandidateEligibility.WATCH
                terminal = False
            else:
                eligibility = CandidateEligibility.ELIGIBLE
                terminal = True

            supporting = [
                self._fact("FAILED_GENUINE_OUTSIDE_ATTEMPT", evidence, True, "boundary.attempt_time"),
                self._fact("FAILED_MEANINGFUL_REENTRY", evidence, True, "boundary.first_reentry_time"),
                self._fact("FAILED_INSIDE_HOLD", evidence, True, "boundary.consecutive_inside_closes"),
            ]
            if followthrough_confirmed:
                supporting.append(
                    self._fact(
                        "FAILED_DIRECTIONAL_FOLLOWTHROUGH_CONFIRMED",
                        evidence,
                        favorable_atr,
                        "setup.failed.followthrough_atr",
                    )
                )

            candidate = self._candidate(
                evidence,
                auction_state,
                candidate_id=candidate_id,
                family=SetupFamily.FAILED_BREAKOUT,
                subtype=watch.subtype,
                candidate_role=CandidateRole.FAILED_RESOLUTION_ENTRY,
                side=watch.side,
                event_key=watch.event_key,
                event_time=watch.event_time,
                candidate_time=watch.failed_time,
                boundary_price=watch.boundary_price,
                source_boundary=self._source_boundary_from_failed_watch(watch),
                eligibility=eligibility,
                blockers=blockers,
                terminal=terminal,
                valid_until=None if terminal else watch.expires_at,
                first_move_consumed=first_move_consumed,
                target_basis=room[3],
                target_reference_price=room[0],
                supporting_evidence=supporting,
                diagnostics={
                    "resolution": "FAILED",
                    "resolution_basis": watch.resolution_basis,
                    "state_at_failure": watch.state_at_failure.value,
                    "watch_age_minutes": age_minutes,
                    "favorable_progress_atr": favorable_atr,
                    "followthrough_confirmed": followthrough_confirmed,
                    "renewed_acceptance_closes": watch.renewed_acceptance_closes,
                    "dynamic_watch": dynamic_watch,
                    "room_target_type": room[3],
                    **self._failed_value_diagnostics(evidence, watch, room),
                    "create_window_position": create_window,
                },
            )
            out.append(candidate)
            if terminal:
                watch.emitted_terminal = True
                self._completed.add(candidate_id)
                self._failed.pop(candidate_id, None)
        return out

    # ------------------------------------------------------------------
    # Confirmed normal / exhaustion reversal
    # ------------------------------------------------------------------
    def _reversal_candidates(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        state_diag: Mapping[str, Any],
    ) -> List[SetupCandidate]:
        policy = self.config.reversal
        if not policy.enabled or not policy.create_enabled:
            return []
        if auction_state.current_state is not AuctionStateName.REVERSAL:
            return []
        # Emit only on the confirmed transition. The Opportunity Ledger carries
        # the eligible record forward to the next snapshot for no-same-pass
        # signal replacement/creation.
        if auction_state.previous_state is not AuctionStateName.TREND_FAILURE:
            return []

        event_key = str(self._mapping_value(state_diag, "last_failure_terminal_key") or "").strip()
        if not event_key:
            return []
        side = self._trade_side_from_direction(
            self._mapping_value(state_diag, "last_failure_side")
            or self._mapping_value(state_diag, "established_trend_side")
        )
        if side not in (TradeSide.BUY, TradeSide.SELL):
            return []

        subtype, exhaustion_diag = self._reversal_subtype(
            evidence,
            state_diag,
        )
        candidate_id = self._candidate_id(
            "REV",
            evidence.symbol,
            event_key,
            SetupFamily.REVERSAL,
            subtype,
        )
        if candidate_id in self._emitted_once:
            return []

        source = self._reversal_source_structure(
            evidence,
            side,
            event_key,
            state_diag,
        )
        if source is None:
            # A REVERSAL without its frozen failure structure violates the strict
            # state/setup handoff. Fail visibly instead of fabricating a level.
            raise ValueError(
                "REVERSAL_STATE_MISSING_FROZEN_TREND_FAILURE_STRUCTURE"
            )

        blockers: List[str] = []
        if policy.require_confirmed_reversal_state and (
            auction_state.previous_state is not AuctionStateName.TREND_FAILURE
            or auction_state.current_state is not AuctionStateName.REVERSAL
        ):
            blockers.append("REVERSAL_STATE_NOT_CONFIRMED")
        if policy.require_structural_trend_failure and str(
            self._mapping_value(state_diag, "last_failure_terminal_reason") or ""
        ).strip().upper() != "CONFIRMED_OPPOSITE_REVERSAL":
            blockers.append("REVERSAL_NOT_LINKED_TO_CONFIRMED_TREND_FAILURE")

        failure_level = float(source["boundary_price"])
        if policy.require_failure_level and failure_level <= 0:
            blockers.append("REVERSAL_FAILURE_LEVEL_MISSING")
        if side is TradeSide.BUY and failure_level >= evidence.close:
            blockers.append("REVERSAL_BUY_STOP_NOT_BELOW_ENTRY")
        if side is TradeSide.SELL and failure_level <= evidence.close:
            blockers.append("REVERSAL_SELL_STOP_NOT_ABOVE_ENTRY")

        entry_distance = self._entry_distance_atr(
            evidence.close,
            failure_level,
            evidence.atr,
        )
        if (
            entry_distance is None
            or entry_distance > policy.max_entry_distance_from_failure_level_atr
        ):
            blockers.append("REVERSAL_ENTRY_TOO_FAR_FROM_FAILURE_LEVEL")

        target_price, target_basis = self._reversal_target(
            evidence,
            side,
            state_diag,
        )
        room_points = self._favorable_points(
            evidence.close,
            target_price,
            side,
        )
        room_atr = (
            room_points / evidence.atr
            if room_points is not None and evidence.atr
            else None
        )
        room_pct = (
            room_points / evidence.close
            if room_points is not None and evidence.close
            else None
        )
        if policy.require_opportunity_room:
            if target_price is None:
                blockers.append("REVERSAL_STRUCTURAL_TARGET_UNAVAILABLE")
            if room_atr is None or room_atr < policy.minimum_room_atr:
                blockers.append("REVERSAL_ROOM_ATR_INSUFFICIENT")
            if room_pct is None or room_pct < policy.minimum_room_pct:
                blockers.append("REVERSAL_ROOM_BELOW_MINIMUM_PCT")

        if (
            evidence.opportunity.session_minutes_remaining is None
            or evidence.opportunity.session_minutes_remaining
            < policy.minimum_session_minutes
        ):
            blockers.append("REVERSAL_INSUFFICIENT_SESSION_TIME")

        if subtype == "EXHAUSTION_REVERSAL" and not policy.exhaustion_reversal_enabled:
            blockers.append("EXHAUSTION_REVERSAL_DISABLED")
        if subtype == "NORMAL_REVERSAL" and not policy.normal_reversal_enabled:
            blockers.append("NORMAL_REVERSAL_DISABLED")

        event_time = self._diag_datetime(
            self._mapping_value(state_diag, "last_failure_watch_onset")
        ) or self._diag_datetime(
            self._mapping_value(state_diag, "last_failure_terminal_time")
        ) or auction_state.transition_time
        candidate_time = auction_state.transition_time

        supporting = [
            self._fact(
                "REVERSAL_CONFIRMED_AFTER_TREND_FAILURE",
                evidence,
                True,
                "auction_state.current_state",
            ),
            self._fact(
                "REVERSAL_OPPOSITE_FOLLOWTHROUGH_CONFIRMED",
                evidence,
                self._mapping_value(state_diag, "last_failure_side"),
                "state.last_failure_side",
            ),
            self._fact(
                "REVERSAL_FAILURE_LEVEL_FROZEN",
                evidence,
                failure_level,
                "state.last_failure_level",
            ),
        ]
        if subtype == "EXHAUSTION_REVERSAL":
            supporting.append(
                self._fact(
                    "REVERSAL_PRIOR_MOVE_EXHAUSTED",
                    evidence,
                    exhaustion_diag,
                    "evidence.extension",
                )
            )

        candidate = self._candidate(
            evidence,
            auction_state,
            candidate_id=candidate_id,
            family=SetupFamily.REVERSAL,
            subtype=subtype,
            candidate_role=CandidateRole.REVERSAL_ENTRY,
            side=side,
            event_key=event_key,
            event_time=event_time,
            candidate_time=candidate_time,
            boundary_price=failure_level,
            source_boundary=source,
            eligibility=(
                CandidateEligibility.INELIGIBLE
                if blockers
                else CandidateEligibility.ELIGIBLE
            ),
            blockers=blockers,
            terminal=True,
            valid_until=None,
            target_basis=target_basis,
            target_reference_price=target_price,
            stop_anchor_type="CONFIRMED_TREND_FAILURE_LEVEL",
            supporting_evidence=supporting,
            diagnostics={
                "reversal_family": "GENERAL_REVERSAL",
                "reversal_subtype": subtype,
                "prior_trend_side": self._mapping_value(
                    state_diag, "last_failure_original_trend_side"
                ),
                "reversal_side": side.value,
                "trend_failure_event_key": event_key,
                "trend_failure_terminal_reason": self._mapping_value(
                    state_diag, "last_failure_terminal_reason"
                ),
                "failure_level": failure_level,
                "failure_level_source": self._mapping_value(
                    state_diag, "last_failure_level_source"
                ),
                "distance_from_failure_level_atr": entry_distance,
                "target_basis": target_basis,
                "target_reference_price": target_price,
                "room_atr": room_atr,
                "room_pct": room_pct,
                "exhaustion_classification": exhaustion_diag,
                "no_same_pass_reversal": True,
            },
        )
        self._emitted_once.add(candidate_id)
        self._completed.add(candidate_id)
        return [candidate]

    def _reversal_subtype(
        self,
        evidence: EvidenceSnapshot,
        state_diag: Mapping[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        """Classify the confirmed reversal from frozen prior-trend evidence.

        The current snapshot's extension block may already describe the new
        direction.  The state engine therefore freezes the failed trend anchor,
        extreme and ATR before establishing the opposite trend.  Those causal
        values are the primary extension measurement used here.
        """
        policy = self.config.reversal
        extension = evidence.extension

        prior_anchor = self._diag_float(
            self._mapping_value(state_diag, "last_failure_trend_anchor_price")
        )
        prior_extreme = self._diag_float(
            self._mapping_value(state_diag, "last_failure_trend_extreme_price")
        )
        prior_atr = self._diag_float(self._mapping_value(state_diag, "last_failure_atr"))
        if prior_atr is None:
            prior_atr = evidence.atr

        frozen_prior_move_atr = None
        if (
            prior_anchor is not None
            and prior_extreme is not None
            and prior_atr is not None
            and prior_atr > 0
        ):
            frozen_prior_move_atr = abs(prior_anchor - prior_extreme) / prior_atr

        current_extension_move_atr = (
            abs(float(extension.move_from_anchor_atr))
            if extension.move_from_anchor_atr is not None
            else None
        )
        classification_move_atr = (
            frozen_prior_move_atr
            if frozen_prior_move_atr is not None
            else current_extension_move_atr
        )
        decay = extension.progress_decay
        rejection = bool(
            evidence.price_action.rejection
            or evidence.price_action.failed_extreme
            or extension.failed_extreme_count > 0
        )
        extension_large = bool(
            classification_move_atr is not None
            and classification_move_atr
            >= policy.exhaustion_extension_atr_min
        )
        progress_lost = bool(
            decay is not None
            and decay >= policy.exhaustion_progress_decay_min
        )
        exhaustion = bool(
            extension_large
            and (
                extension.mature is True
                or extension.extended is True
                or progress_lost
                or rejection
            )
        )
        diagnostics = {
            "extended": extension.extended,
            "mature": extension.mature,
            "frozen_prior_move_atr": frozen_prior_move_atr,
            "current_extension_move_atr": current_extension_move_atr,
            "classification_move_atr": classification_move_atr,
            "progress_decay": decay,
            "failed_extreme_count": extension.failed_extreme_count,
            "rejection_or_failed_extreme": rejection,
            "extension_large": extension_large,
            "progress_lost": progress_lost,
            "classified_exhaustion": exhaustion,
        }
        return (
            "EXHAUSTION_REVERSAL" if exhaustion else "NORMAL_REVERSAL",
            diagnostics,
        )

    def _reversal_source_structure(
        self,
        evidence: EvidenceSnapshot,
        side: TradeSide,
        event_key: str,
        state_diag: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        level = self._diag_float(self._mapping_value(state_diag, "last_failure_level"))
        low = self._diag_float(self._mapping_value(state_diag, "last_failure_structure_low"))
        high = self._diag_float(self._mapping_value(state_diag, "last_failure_structure_high"))
        if level is None or low is None or high is None or high <= low:
            return None
        boundary_side = (
            BoundarySide.UPPER if side is TradeSide.BUY else BoundarySide.LOWER
        )
        expected = high if boundary_side is BoundarySide.UPPER else low
        tolerance = max(1e-9, abs(expected) * 1e-9)
        if abs(level - expected) > tolerance:
            return None
        version = max(
            1,
            int(self._mapping_value(state_diag, "last_failure_level_version") or 0),
        )
        structure_id = str(
            self._mapping_value(state_diag, "last_failure_level_episode_key")
            or stable_key(
                "trend-failure-structure",
                evidence.symbol,
                evidence.trading_day,
                event_key,
                level,
                version,
            )
        )
        return {
            "status": BoundaryEpisodeStatus.FAILED,
            "resolution": BoundaryResolution.FAILED,
            "resolution_basis": "CONFIRMED_TREND_FAILURE_REVERSAL",
            "boundary_id": f"{structure_id}:{boundary_side.value}",
            "boundary_side": boundary_side,
            "boundary_source": str(
                self._mapping_value(state_diag, "last_failure_level_source")
                or "TREND_PROTECTION_FAILURE_LEVEL"
            ),
            "boundary_price": level,
            "frozen_range_id": structure_id,
            "frozen_range_version": version,
            "frozen_range_low": low,
            "frozen_range_high": high,
        }

    def _reversal_target(
        self,
        evidence: EvidenceSnapshot,
        side: TradeSide,
        state_diag: Mapping[str, Any],
    ) -> Tuple[Optional[float], str]:
        prior_anchor = self._diag_float(
            self._mapping_value(state_diag, "last_failure_trend_anchor_price")
        )
        if self._favorable_points(evidence.close, prior_anchor, side) not in (
            None,
            0.0,
        ):
            return prior_anchor, "PRIOR_FAILED_TREND_ANCHOR"

        levels = _required_raw_levels(evidence)
        candidates: List[Tuple[float, str]] = []
        for label, raw in (
            ("TODAY_OPEN", self._mapping_value(levels, "today_open")),
            ("VWAP", self._mapping_value(levels, "vwap")),
            ("PREV_DAY_HIGH", self._mapping_value(levels, "prev_day_high")),
            ("OPENING_RANGE_HIGH", self._mapping_value(levels, "opening_range_high")),
            ("PREV_DAY_LOW", self._mapping_value(levels, "prev_day_low")),
            ("OPENING_RANGE_LOW", self._mapping_value(levels, "opening_range_low")),
        ):
            price = self._diag_float(raw)
            points = self._favorable_points(evidence.close, price, side)
            if price is not None and points is not None and points > 0:
                candidates.append((price, label))
        if not candidates:
            return None, "NO_STRUCTURAL_REVERSAL_TARGET"
        price, label = min(
            candidates,
            key=lambda item: abs(item[0] - evidence.close),
        )
        return price, f"NEAREST_REVERSAL_LEVEL_{label}"

    @staticmethod
    def _trade_side_from_direction(value: Any) -> TradeSide:
        raw = str(getattr(value, "value", value) or "").strip().upper()
        if raw in {"UP", "BUY"}:
            return TradeSide.BUY
        if raw in {"DOWN", "SELL"}:
            return TradeSide.SELL
        return TradeSide.NONE

    @staticmethod
    def _mapping_value(
        mapping: Mapping[str, Any],
        key: str,
        default: Any = None,
    ) -> Any:
        return mapping[key] if key in mapping else default

    @staticmethod
    def _diag_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            parsed = float(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _diag_datetime(value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    # ------------------------------------------------------------------
    # Candidate construction and objective gates
    # ------------------------------------------------------------------
    def _candidate(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        *,
        candidate_id: str,
        family: SetupFamily,
        subtype: str,
        candidate_role: CandidateRole,
        side: TradeSide,
        event_key: str,
        event_time: datetime,
        candidate_time: datetime,
        boundary_price: float,
        source_boundary: Mapping[str, Any],
        eligibility: CandidateEligibility,
        blockers: Sequence[str],
        terminal: bool,
        valid_until: Optional[datetime],
        first_move_consumed: bool = False,
        target_basis: str,
        target_reference_price: Optional[float] = None,
        stop_anchor_type: str = "FROZEN_BOUNDARY",
        supporting_evidence: Sequence[EvidenceFact] = (),
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> SetupCandidate:
        blockers_clean = tuple(dict.fromkeys(str(code).strip().upper() for code in blockers if str(code).strip()))
        # Room belongs only to an explicit structural target.  Accepted and
        # initiating breakouts are open-ended and therefore carry no assumed
        # target/room fields.  Failed auctions pass the opposite frozen-range
        # edge explicitly.
        room_target = target_reference_price
        room_points = self._favorable_points(evidence.close, room_target, side)
        room_atr = room_points / evidence.atr if room_points is not None and evidence.atr else None
        room_pct = room_points / evidence.close if room_points is not None and evidence.close else None
        entry_distance = self._entry_distance_atr(evidence.close, boundary_price, evidence.atr)
        freshness = max(0.0, (evidence.snapshot_time - candidate_time).total_seconds() / 60.0)

        supporting = list(supporting_evidence)
        supporting.extend([
            self._fact(
                f"{family.value}_BOUNDARY_EVENT_LINKED",
                evidence,
                event_key,
                "boundary.event_key",
            ),
            self._fact(
                f"{family.value}_AUCTION_STATE_OBSERVED",
                evidence,
                auction_state.current_state.value,
                "auction_state.current_state",
            ),
        ])
        opposing = [
            self._fact(code, evidence, True, "setup.blockers", polarity=EvidencePolarity.CONTRADICT)
            for code in blockers_clean
        ]
        channels = self._confidence_channels(
            blockers_clean,
            room_atr=room_atr,
            freshness_minutes=freshness,
            evidence=evidence,
        )
        reason_codes = tuple(dict.fromkeys((
            f"{family.value}_{eligibility.value}",
            subtype,
            *blockers_clean,
        )))
        stop_anchor = boundary_price
        stop_type = str(stop_anchor_type or "FROZEN_BOUNDARY").strip().upper()
        opportunity_key = self._opportunity_key(event_key, side)
        boundary_thesis_key = self._boundary_thesis_key(event_key)
        return SetupCandidate(
            candidate_id=candidate_id,
            symbol=evidence.symbol,
            trading_day=evidence.trading_day,
            snapshot_time=evidence.snapshot_time,
            candidate_time=candidate_time,
            family=family,
            subtype=subtype,
            side=side,
            event_key=event_key,
            event_time=event_time,
            opportunity_key=opportunity_key,
            boundary_thesis_key=boundary_thesis_key,
            support_group_key=opportunity_key,
            candidate_role=candidate_role,
            source_boundary_event_key=event_key,
            source_boundary_status=source_boundary["status"],
            source_boundary_resolution=source_boundary["resolution"],
            source_boundary_resolution_basis=source_boundary["resolution_basis"],
            source_boundary_id=str(source_boundary["boundary_id"]),
            source_boundary_side=source_boundary["boundary_side"],
            source_boundary_source=str(source_boundary["boundary_source"]),
            source_boundary_price=float(source_boundary["boundary_price"]),
            source_frozen_range_id=str(source_boundary["frozen_range_id"]),
            source_frozen_range_version=int(source_boundary["frozen_range_version"]),
            source_frozen_range_low=float(source_boundary["frozen_range_low"]),
            source_frozen_range_high=float(source_boundary["frozen_range_high"]),
            entry_price=evidence.close,
            stop_anchor_price=stop_anchor,
            stop_anchor_type=stop_type,
            target_basis=target_basis,
            target_reference_price=room_target,
            room_points=room_points,
            room_atr=room_atr,
            room_pct=room_pct,
            entry_distance_atr=entry_distance,
            freshness_minutes=freshness,
            first_move_consumed=first_move_consumed,
            auction_state=auction_state.current_state,
            eligibility=eligibility,
            blockers=blockers_clean,
            supporting_evidence=tuple(supporting),
            opposing_evidence=tuple(opposing),
            confidence_channels=channels,
            reason_codes=reason_codes,
            terminal=terminal,
            valid_until=valid_until,
            dynamic_watch=bool(
                diagnostics["dynamic_watch"]
                if diagnostics is not None and "dynamic_watch" in diagnostics
                else False
            ),
            diagnostics=dict(diagnostics or {}),
            config_version=self.version,
        )

    def _common_breakout_blockers(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        *,
        side: TradeSide,
        boundary_price: float,
        event_time: datetime,
        state_diag: Mapping[str, Any],
        max_entry_distance_atr: float,
        minimum_session_minutes: float,
    ) -> Tuple[List[str], bool]:
        blockers = self._state_blockers(
            auction_state.current_state, side, state_diag, prefix="BREAKOUT"
        )
        # Accepted/initiating breakout reward is not knowable at entry.  Do not
        # turn a measured move or external reference into a hard terminal target.
        # Freshness remains an objective distance-from-boundary gate, while
        # MATURE_EXTENSION is handled by the auction-state blockers above.
        entry_distance = self._entry_distance_atr(evidence.close, boundary_price, evidence.atr)
        if entry_distance is None or entry_distance > max_entry_distance_atr:
            blockers.append("BREAKOUT_ENTRY_TOO_FAR_FROM_BOUNDARY")
        if evidence.opportunity.session_minutes_remaining is None or evidence.opportunity.session_minutes_remaining < minimum_session_minutes:
            blockers.append("BREAKOUT_INSUFFICIENT_SESSION_TIME")
        dynamic_watch = any(code in blockers for code in {
            "BREAKOUT_STATE_UNKNOWN",
            "BREAKOUT_TREND_SIDE_CONFLICT",
        })
        return blockers, dynamic_watch

    def _state_blockers(
        self,
        state: AuctionStateName,
        side: TradeSide,
        state_diag: Mapping[str, Any],
        *,
        prefix: str,
    ) -> List[str]:
        blockers: List[str] = []
        if state is AuctionStateName.UNKNOWN:
            blockers.append(f"{prefix}_STATE_UNKNOWN")
        if state is AuctionStateName.CHAOTIC_ROTATION:
            blockers.append(f"{prefix}_CHAOTIC_ROTATION")
        if state is AuctionStateName.MATURE_EXTENSION:
            blockers.append(f"{prefix}_MATURE_EXTENSION_LATE_ENTRY")
        if state is AuctionStateName.TREND_FAILURE:
            blockers.append(f"{prefix}_ACTIVE_TREND_FAILURE")
        if state is AuctionStateName.REVERSAL:
            blockers.append(f"{prefix}_REVERSAL_TRANSITION_UNSETTLED")
        established = self._established_side(state_diag)
        if established in (TradeSide.BUY, TradeSide.SELL) and established is not side:
            # Failed-auction countertrend classification handles this explicitly
            # rather than treating it as a generic contradiction.
            if prefix != "FAILED":
                blockers.append(f"{prefix}_TREND_SIDE_CONFLICT")
        return blockers

    def _failed_subtype(
        self,
        candidate_side: TradeSide,
        auction_state: AuctionState,
        state_diag: Mapping[str, Any],
    ) -> str:
        established = self._established_side(state_diag)
        if established in (TradeSide.BUY, TradeSide.SELL):
            if established is candidate_side:
                return "TREND_ALIGNED_FAILED_AUCTION"
            return "COUNTERTREND_FAILED_AUCTION"
        if auction_state.current_state in {
            AuctionStateName.BALANCE,
            AuctionStateName.COMPRESSION,
            AuctionStateName.BOUNDARY_INTERACTION,
            AuctionStateName.UNKNOWN,
            AuctionStateName.CHAOTIC_ROTATION,
        }:
            return "NEUTRAL_RANGE_FAILED_AUCTION"
        return "NEUTRAL_RANGE_FAILED_AUCTION"

    def _failed_room(
        self,
        evidence: EvidenceSnapshot,
        watch: _FailedWatch,
    ) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
        # A failed auction has a known structural destination: the opposite edge
        # of the immutable frozen range.  Midpoint and VWAP can be intermediate
        # management references, but they must not reduce setup room.
        opposite_edge = watch.frozen_high if watch.side is TradeSide.BUY else watch.frozen_low
        points = self._favorable_points(evidence.close, opposite_edge, watch.side)
        room_atr = points / evidence.atr if points is not None and evidence.atr else None
        room_pct = points / evidence.close if points is not None and evidence.close else None
        return opposite_edge, room_atr, room_pct, "FROZEN_RANGE_OPPOSITE_EDGE"

    def _actual_external_barriers(
        self,
        evidence: EvidenceSnapshot,
        side: TradeSide,
        boundary_price: float,
    ) -> List[Tuple[float, str]]:
        levels = _required_raw_levels(evidence)
        candidates: List[Tuple[float, str]] = []
        for label, raw in (
            ("PREV_DAY_HIGH", levels["prev_day_high"]),
            ("OPENING_RANGE_HIGH", levels["opening_range_high"]),
            ("PREV_DAY_LOW", levels["prev_day_low"]),
            ("OPENING_RANGE_LOW", levels["opening_range_low"]),
        ):
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            minimum_gap = _required_evidence_atr(evidence) * self.config.boundary.acceptance_close_buffer_atr
            beyond_broken_boundary = (
                price > boundary_price + minimum_gap
                if side is TradeSide.BUY
                else price < boundary_price - minimum_gap
            )
            points = self._favorable_points(evidence.close, price, side)
            if beyond_broken_boundary and points is not None and points > 0:
                candidates.append((price, label))
        return sorted(candidates, key=lambda item: abs(item[0] - evidence.close))

    def _breakout_reference_diagnostics(
        self,
        evidence: EvidenceSnapshot,
        side: TradeSide,
        boundary_price: float,
        source_boundary: Mapping[str, Any],
    ) -> Dict[str, Any]:
        barriers = self._actual_external_barriers(evidence, side, boundary_price)
        barrier_price = barriers[0][0] if barriers else None
        barrier_type = barriers[0][1] if barriers else None
        barrier_points = self._favorable_points(evidence.close, barrier_price, side)
        measured = self._measured_move_from_source(side, boundary_price, source_boundary)
        measured_points = self._favorable_points(evidence.close, measured, side)
        entry_distance = self._entry_distance_atr(evidence.close, boundary_price, evidence.atr)
        return {
            "reward_model": "OPEN_ENDED_BREAKOUT",
            "assumed_target_hard_gate": False,
            "distance_from_boundary_atr": entry_distance,
            "nearest_actual_barrier_type": barrier_type,
            "nearest_actual_barrier_price": barrier_price,
            "nearest_actual_barrier_distance_points": barrier_points,
            "nearest_actual_barrier_distance_atr": (
                barrier_points / evidence.atr
                if barrier_points is not None and evidence.atr
                else None
            ),
            "nearest_actual_barrier_distance_pct": (
                barrier_points / evidence.close
                if barrier_points is not None and evidence.close
                else None
            ),
            "actual_barrier_candidates": [
                {"price": price, "type": label} for price, label in barriers
            ],
            "measured_move_reference_price": measured,
            "measured_move_reference_is_diagnostic_only": True,
            "measured_move_distance_from_entry_points": measured_points,
            "measured_move_distance_from_entry_atr": (
                measured_points / evidence.atr
                if measured_points is not None and evidence.atr
                else None
            ),
            "measured_move_distance_from_entry_pct": (
                measured_points / evidence.close
                if measured_points is not None and evidence.close
                else None
            ),
        }

    def _measured_move_from_source(
        self,
        side: TradeSide,
        boundary_price: float,
        source_boundary: Mapping[str, Any],
    ) -> Optional[float]:
        try:
            low = float(source_boundary["frozen_range_low"])
            high = float(source_boundary["frozen_range_high"])
        except (KeyError, TypeError, ValueError):
            return None
        width = high - low
        if width <= 0:
            return None
        return boundary_price + width if side is TradeSide.BUY else boundary_price - width

    def _failed_value_diagnostics(
        self,
        evidence: EvidenceSnapshot,
        watch: _FailedWatch,
        room: Tuple[Optional[float], Optional[float], Optional[float], str],
    ) -> Dict[str, Any]:
        midpoint = (watch.frozen_low + watch.frozen_high) / 2.0
        vwap = self._vwap_price(evidence)
        width = watch.frozen_high - watch.frozen_low
        progress_points = (
            evidence.close - watch.boundary_price
            if watch.side is TradeSide.BUY
            else watch.boundary_price - evidence.close
        )
        midpoint_points = self._favorable_points(evidence.close, midpoint, watch.side)
        vwap_points = self._favorable_points(evidence.close, vwap, watch.side)
        return {
            "reward_model": "RETURN_TO_OPPOSITE_FROZEN_RANGE_EDGE",
            "failed_opposite_range_edge_price": room[0],
            "failed_opposite_range_edge_room_atr": room[1],
            "failed_opposite_range_edge_room_pct": room[2],
            "failed_range_width_points": width,
            "failed_range_progress_points": max(0.0, progress_points),
            "failed_range_progress_fraction": (
                max(0.0, progress_points) / width if width > 0 else None
            ),
            "failed_midpoint_price": midpoint,
            "failed_midpoint_distance_atr": (
                midpoint_points / evidence.atr
                if midpoint_points is not None and evidence.atr
                else None
            ),
            "failed_vwap_price": vwap,
            "failed_vwap_distance_atr": (
                vwap_points / evidence.atr
                if vwap_points is not None and evidence.atr
                else None
            ),
            "midpoint_vwap_are_diagnostic_only": True,
        }

    def _vwap_price(self, evidence: EvidenceSnapshot) -> Optional[float]:
        if evidence.atr is None or evidence.trend.vwap_distance_atr is None:
            return None
        distance = abs(evidence.trend.vwap_distance_atr) * evidence.atr
        side = str(evidence.trend.vwap_side or "").upper()
        if side == "ABOVE":
            return evidence.close - distance
        if side == "BELOW":
            return evidence.close + distance
        return evidence.close

    def _confidence_channels(
        self,
        blockers: Sequence[str],
        *,
        room_atr: Optional[float],
        freshness_minutes: Optional[float],
        evidence: EvidenceSnapshot,
    ) -> Tuple[ConfidenceChannel, ...]:
        blocker_set = set(blockers)
        structural_blockers = tuple(code for code in blockers if any(token in code for token in (
            "STATE", "TREND", "FAILED_LEVEL", "ATTEMPT", "HOLD", "DISPLACEMENT",
            "REVERSAL", "CHAOTIC", "EXTENSION", "STRUCTURE", "RECENT_VALID_BALANCE",
        )))
        opportunity_blockers = tuple(code for code in blockers if any(token in code for token in (
            "ROOM", "ENTRY", "FIRST_MOVE", "SESSION",
        )))
        return (
            ConfidenceChannel(
                name="structural_validity",
                score=100.0 if not structural_blockers else 0.0,
                quality=evidence.data_quality.status,
                contradicting_fact_codes=structural_blockers,
                reason_codes=structural_blockers,
            ),
            ConfidenceChannel(
                name="opportunity",
                score=100.0 if not opportunity_blockers else 0.0,
                quality=evidence.opportunity.quality.status,
                contradicting_fact_codes=opportunity_blockers,
                reason_codes=opportunity_blockers,
            ),
            ConfidenceChannel(
                name="freshness",
                score=(
                    100.0 if freshness_minutes is not None and freshness_minutes <= 3.5
                    else 50.0 if freshness_minutes is not None and freshness_minutes <= 9.5
                    else 0.0
                ),
                quality=QualityStatus.GOOD,
                reason_codes=("FRESHNESS_FROM_EVENT_CHRONOLOGY",),
            ),
        )

    def _fact(
        self,
        code: str,
        evidence: EvidenceSnapshot,
        value: Any,
        source_path: str,
        *,
        polarity: EvidencePolarity = EvidencePolarity.SUPPORT,
    ) -> EvidenceFact:
        return EvidenceFact(
            code=code,
            domain="setup",
            polarity=polarity,
            observed_at=evidence.snapshot_time,
            value=value,
            source_path=source_path,
            quality=evidence.data_quality.status,
        )

    def _established_side(self, state_diag: Mapping[str, Any]) -> TradeSide:
        raw = str(state_diag["established_trend_side"]).upper()
        if raw in {"UP", "BUY"}:
            return TradeSide.BUY
        if raw in {"DOWN", "SELL"}:
            return TradeSide.SELL
        return TradeSide.NONE

    def _side_matches_established_trend(
        self, side: TradeSide, state_diag: Mapping[str, Any]
    ) -> bool:
        established = self._established_side(state_diag)
        return established in (TradeSide.BUY, TradeSide.SELL) and established is side

    def _initial_initiation_context(
        self,
        side: TradeSide,
        auction_state: AuctionState,
        state_diag: Mapping[str, Any],
    ) -> Tuple[str, str, bool, bool]:
        """Freeze the initiation thesis at the attempt snapshot.

        Later state transitions may block, expire or confirm the candidate, but
        they must never rename the thesis under the same immutable candidate ID.
        """

        established = self._established_side(state_diag)
        established_text = (
            established.value
            if established in (TradeSide.BUY, TradeSide.SELL)
            else "UNKNOWN"
        )
        pause_states = {
            AuctionStateName.CONTROLLED_PULLBACK,
            AuctionStateName.RECOMPRESSION,
            AuctionStateName.REACCELERATION,
            AuctionStateName.ORDERLY_UPTREND,
            AuctionStateName.ORDERLY_DOWNTREND,
        }
        balance_states = {
            AuctionStateName.BALANCE,
            AuctionStateName.COMPRESSION,
            AuctionStateName.BOUNDARY_INTERACTION,
            AuctionStateName.FRESH_EXPANSION,
        }
        pause_context = (
            auction_state.previous_state in pause_states
            or auction_state.current_state in pause_states
        )
        recent_balance_context = (
            auction_state.previous_state in balance_states
            or auction_state.current_state in balance_states
        )
        same_side_established = (
            established in (TradeSide.BUY, TradeSide.SELL)
            and established is side
        )
        subtype = (
            "CONTINUATION_INITIATION"
            if same_side_established and pause_context
            else "FRESH_EXPANSION_INITIATION"
        )
        return subtype, established_text, pause_context, recent_balance_context

    @staticmethod
    def _source_boundary_from_episode(episode: BoundaryEpisode) -> Dict[str, Any]:
        return {
            "status": episode.status,
            "resolution": episode.resolution,
            "resolution_basis": episode.diagnostics["failure_resolution_basis"],
            "boundary_id": episode.boundary_id,
            "boundary_side": episode.boundary_side,
            "boundary_source": episode.boundary_source,
            "boundary_price": episode.boundary_price,
            "frozen_range_id": episode.frozen_range.range_id,
            "frozen_range_version": episode.frozen_range.range_version,
            "frozen_range_low": episode.frozen_range.low,
            "frozen_range_high": episode.frozen_range.high,
        }

    @staticmethod
    def _source_boundary_from_initiation_watch(
        watch: _InitiationWatch,
    ) -> Dict[str, Any]:
        return {
            "status": watch.source_boundary_status,
            "resolution": watch.source_boundary_resolution,
            "resolution_basis": watch.source_boundary_resolution_basis,
            "boundary_id": watch.source_boundary_id,
            "boundary_side": watch.source_boundary_side,
            "boundary_source": watch.source_boundary_source,
            "boundary_price": watch.boundary_price,
            "frozen_range_id": watch.source_frozen_range_id,
            "frozen_range_version": watch.source_frozen_range_version,
            "frozen_range_low": watch.source_frozen_range_low,
            "frozen_range_high": watch.source_frozen_range_high,
        }

    @staticmethod
    def _source_boundary_from_failed_watch(watch: _FailedWatch) -> Dict[str, Any]:
        return {
            "status": watch.source_boundary_status,
            "resolution": watch.source_boundary_resolution,
            "resolution_basis": watch.resolution_basis,
            "boundary_id": watch.source_boundary_id,
            "boundary_side": watch.source_boundary_side,
            "boundary_source": watch.source_boundary_source,
            "boundary_price": watch.boundary_price,
            "frozen_range_id": watch.source_frozen_range_id,
            "frozen_range_version": watch.source_frozen_range_version,
            "frozen_range_low": watch.frozen_low,
            "frozen_range_high": watch.frozen_high,
        }

    def _create_window_position(self, snapshot_time: datetime) -> str:
        current = snapshot_time.time()
        earliest = datetime.strptime(
            self.config.engine.earliest_create_time, "%H:%M:%S"
        ).time()
        latest = datetime.strptime(
            self.config.engine.latest_create_time, "%H:%M:%S"
        ).time()
        if current < earliest:
            return "BEFORE"
        if current > latest:
            return "AFTER"
        return "OPEN"

    @staticmethod
    def _opportunity_key(event_key: str, side: TradeSide) -> str:
        return stable_key("OPPORTUNITY", event_key, side.value)

    @staticmethod
    def _boundary_thesis_key(event_key: str) -> str:
        return stable_key("BOUNDARY_THESIS", event_key)

    @staticmethod
    def _candidate_id(
        prefix: str,
        symbol: str,
        event_key: str,
        family: SetupFamily,
        subtype: str,
    ) -> str:
        # Prefix includes symbol for easier diagnostics while the hash preserves
        # deterministic compact identity.
        digest = stable_key(prefix, symbol, event_key, family.value, subtype)
        return f"{prefix}:{symbol}:{digest.split(':', 1)[1]}"

    @staticmethod
    def _signed_offset_atr(
        price: float,
        boundary_price: float,
        side: TradeSide,
        atr: Optional[float],
    ) -> Optional[float]:
        if atr is None or atr <= 0:
            return None
        return (
            (price - boundary_price) / atr
            if side is TradeSide.BUY
            else (boundary_price - price) / atr
        )

    @staticmethod
    def _signed_progress_atr(
        price: float,
        anchor: float,
        side: TradeSide,
        atr: Optional[float],
    ) -> Optional[float]:
        if atr is None or atr <= 0:
            return None
        return (
            (price - anchor) / atr
            if side is TradeSide.BUY
            else (anchor - price) / atr
        )

    @staticmethod
    def _entry_distance_atr(
        price: float,
        boundary_price: float,
        atr: Optional[float],
    ) -> Optional[float]:
        if atr is None or atr <= 0:
            return None
        return abs(price - boundary_price) / atr

    @staticmethod
    def _favorable_points(
        entry_price: float,
        target_price: Optional[float],
        side: TradeSide,
    ) -> Optional[float]:
        if target_price is None:
            return None
        points = target_price - entry_price if side is TradeSide.BUY else entry_price - target_price
        return max(0.0, points)

    @staticmethod
    def _append_once(items: List[str], code: str) -> None:
        if code not in items:
            items.append(code)


def _required_raw_levels(evidence: EvidenceSnapshot) -> Mapping[str, Any]:
    if "source_levels" not in evidence.raw_facts:
        raise ValueError("Evidence raw_facts.source_levels is required")
    levels = evidence.raw_facts["source_levels"]
    if not isinstance(levels, Mapping):
        raise ValueError("Evidence raw_facts.source_levels must be a mapping")
    required = {
        "prev_day_high",
        "opening_range_high",
        "prev_day_low",
        "opening_range_low",
    }
    missing = required.difference(levels)
    if missing:
        raise ValueError(
            f"Evidence raw_facts.source_levels missing keys: {sorted(missing)}"
        )
    return levels


def _required_failure_resolution_basis(episode: BoundaryEpisode) -> str:
    if "failure_resolution_basis" not in episode.diagnostics:
        raise ValueError("Boundary episode failure_resolution_basis is required")
    value = episode.diagnostics["failure_resolution_basis"]
    if value is None or not str(value).strip():
        raise ValueError("Boundary episode failure_resolution_basis cannot be empty")
    return str(value).strip().upper()


def _required_evidence_atr(evidence: EvidenceSnapshot) -> float:
    if evidence.atr is None or evidence.atr <= 0:
        raise ValueError("Auction evidence ATR is required and must be positive")
    return float(evidence.atr)


__all__ = ["SetupCandidateEngine"]
