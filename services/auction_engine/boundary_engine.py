"""Chronological unified boundary-episode engine (Phase 3A).

The boundary engine is report-only.  It owns one authoritative dynamic boundary
interaction per symbol and represents accepted/failed breakouts as alternate
terminal resolutions of the same immutable episode.  It does not create setup
candidates, write ``stock_setup_state`` or alter the existing signal pipeline.

Important lifecycle rules
-------------------------
* Only reviewed dynamic ranges are eligible when ``dynamic_boundaries_only`` is
  enabled.
* The event/attempt identity is deterministic and becomes immutable when the
  approach is first observed.
* Range values may evolve while merely APPROACHING; they are frozen at the first
  genuine outside attempt and never move afterwards.
* ACCEPTED and FAILED are alternate outcomes of one episode.
* Terminal episodes cannot reactivate.  A later same-boundary episode requires a
  material reset inside frozen value; a newer range may start a new episode.
* All calculations use the current completed bar plus prior in-memory state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from services.auction_engine.contracts import (
    AuctionState,
    BoundaryEpisode,
    BoundaryEpisodeStatus,
    BoundaryObservation,
    BoundaryResolution,
    BoundarySide,
    DirectionalBias,
    EvidenceFact,
    EvidencePolarity,
    EvidenceSnapshot,
    FrozenRange,
    QualityStatus,
    TradeSide,
    stable_key,
)


class BoundaryChronologyError(ValueError):
    """Raised when a symbol is evaluated out of chronological order."""


@dataclass(frozen=True)
class BoundaryEvaluation:
    # ``episode`` is the active episode after evaluating the current snapshot.
    # When a lifecycle handoff closes an old episode and opens a replacement on
    # the same snapshot, ``closed_episode`` carries the immutable old terminal
    # contract so reports preserve both causal events.
    episode: Optional[BoundaryEpisode]
    previous_status: Optional[BoundaryEpisodeStatus]
    transitioned: bool
    diagnostics: Dict[str, Any]
    closed_episode: Optional[BoundaryEpisode] = None


@dataclass
class _EpisodeMemory:
    event_key: str
    structural_key: str
    attempt_id: str
    sequence: int
    symbol: str
    trading_day: Any
    first_seen_time: datetime
    last_seen_time: datetime
    last_activity_time: datetime
    event_time: datetime
    boundary_id: str
    boundary_side: BoundarySide
    boundary_source: str
    boundary_price: float
    breakout_side: TradeSide
    failure_side: TradeSide
    range_id: str
    range_version: int
    range_source: str
    range_low: float
    range_high: float
    range_start_time: datetime
    range_end_time: Optional[datetime]
    range_basis: str
    range_quality_score: Optional[float]
    frozen_at: datetime
    range_frozen: bool
    status: BoundaryEpisodeStatus = BoundaryEpisodeStatus.APPROACHING
    resolution: BoundaryResolution = BoundaryResolution.UNRESOLVED
    attempt_time: Optional[datetime] = None
    acceptance_building_since: Optional[datetime] = None
    failure_building_since: Optional[datetime] = None
    accepted_time: Optional[datetime] = None
    failed_time: Optional[datetime] = None
    terminal: bool = False
    terminal_reason: Optional[str] = None
    superseded: bool = False
    superseded_by: Optional[str] = None
    consumed: bool = False
    current_offset_atr: Optional[float] = None
    max_outside_excursion_atr: float = 0.0
    max_close_outside_atr: float = 0.0
    total_outside_closes: int = 0
    consecutive_outside_closes: int = 0
    consecutive_acceptance_closes: int = 0
    consecutive_inside_closes: int = 0
    first_outside_close_time: Optional[datetime] = None
    last_outside_time: Optional[datetime] = None
    first_reentry_time: Optional[datetime] = None
    reentry_close: Optional[float] = None
    reentry_depth_atr: Optional[float] = None
    failure_followthrough_atr: float = 0.0
    failure_resolution_basis: Optional[str] = None
    resolution_time: Optional[datetime] = None
    terminal_time: Optional[datetime] = None
    archive_time: Optional[datetime] = None
    archive_reason: Optional[str] = None
    post_terminal_protection_bars: int = 0
    retest_detected: bool = False
    reset_inside_closes: int = 0
    reset_started_at: Optional[datetime] = None
    missing_boundary_bars: int = 0
    last_close: Optional[float] = None
    last_transition_reason: str = "APPROACHING_DYNAMIC_BOUNDARY"
    emitted_resolutions: Tuple[str, ...] = ()


class BoundaryEpisodeEngine:
    """In-memory, causal lifecycle for dynamic boundary episodes."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.cfg = config.boundary
        self.version = config.engine.config_version
        self._current: Dict[str, _EpisodeMemory] = {}
        self._last_time: Dict[str, datetime] = {}
        self._sequences: Dict[Tuple[str, str], int] = {}
        self._last_terminal: Dict[str, Dict[str, Any]] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._current.clear()
            self._last_time.clear()
            self._sequences.clear()
            self._last_terminal.clear()
            return
        key = str(symbol).strip().upper()
        self._current.pop(key, None)
        self._last_time.pop(key, None)
        self._last_terminal.pop(key, None)
        for seq_key in [item for item in self._sequences if item[0] == key]:
            self._sequences.pop(seq_key, None)

    def evaluate(
        self,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
    ) -> BoundaryEvaluation:
        symbol = evidence.symbol
        ts = evidence.snapshot_time
        last_time = self._last_time.get(symbol)
        if last_time is not None:
            if ts < last_time and self.config.state.strict_chronology:
                raise BoundaryChronologyError(
                    f"Out-of-order boundary snapshot for {symbol}: {ts} < {last_time}"
                )
            if ts.date() != last_time.date():
                self.reset(symbol)
        self._last_time[symbol] = ts

        observation = evidence.boundary
        eligible_observation = observation if self._observation_allowed(observation) else None
        memory = self._current.get(symbol)
        previous_status = memory.status if memory is not None else None
        closed_episode: Optional[BoundaryEpisode] = None
        handoff_reason: Optional[str] = None

        # A terminal ACCEPTED/FAILED episode remains protected until a material
        # reset inside frozen value or a genuinely newer range appears.  When it
        # is archived, the replacement boundary is evaluated on this same
        # snapshot so the true first attempt anchor is never delayed by one bar.
        if memory is not None and memory.terminal:
            new_range = self._new_range_key(eligible_observation)
            old_range = self._memory_range_key(memory)
            if new_range is not None and new_range != old_range:
                if memory.resolution_time is not None and ts > memory.resolution_time:
                    memory.post_terminal_protection_bars += 1
                memory.last_seen_time = ts
                self._archive_terminal(
                    memory,
                    reset_reason="TERMINAL_EPISODE_ARCHIVED_FOR_NEW_RANGE",
                )
                closed_episode = self._to_contract(memory, evidence, auction_state)
                self._current.pop(symbol, None)
                memory = None
                handoff_reason = "TERMINAL_EPISODE_ARCHIVED_FOR_NEW_RANGE"
            else:
                self._update_terminal_reset(memory, evidence)
                if memory.reset_inside_closes >= self.cfg.reset_required_inside_closes:
                    self._archive_terminal(
                        memory,
                        reset_reason="TERMINAL_EPISODE_RESET_INSIDE_VALUE",
                    )
                    closed_episode = self._to_contract(memory, evidence, auction_state)
                    self._current.pop(symbol, None)
                    memory = None
                    handoff_reason = "TERMINAL_EPISODE_RESET_INSIDE_VALUE"
                else:
                    episode = self._to_contract(memory, evidence, auction_state)
                    diagnostics = self._diagnostics(
                        memory,
                        evidence,
                        auction_state,
                        transition_reason="TERMINAL_EPISODE_PROTECTED",
                        observation=eligible_observation,
                    )
                    return BoundaryEvaluation(
                        episode=episode,
                        previous_status=previous_status,
                        transitioned=False,
                        diagnostics=self._attach_handoff_diagnostics(
                            diagnostics, None, episode, None
                        ),
                    )

        if memory is None:
            replacement = self._start_if_relevant(
                evidence, eligible_observation, auction_state
            )
            if replacement is not None:
                self._current[symbol] = replacement
                active_episode = self._to_contract(replacement, evidence, auction_state)
                diagnostics = self._diagnostics(
                    replacement,
                    evidence,
                    auction_state,
                    transition_reason=handoff_reason or replacement.last_transition_reason,
                    observation=eligible_observation,
                )
                return BoundaryEvaluation(
                    episode=active_episode,
                    previous_status=previous_status,
                    transitioned=True,
                    diagnostics=self._attach_handoff_diagnostics(
                        diagnostics, closed_episode, active_episode, handoff_reason
                    ),
                    closed_episode=closed_episode,
                )
            if closed_episode is not None:
                diagnostics = self._diagnostics(
                    None,
                    evidence,
                    auction_state,
                    transition_reason=handoff_reason or "EPISODE_ARCHIVED",
                    observation=eligible_observation,
                )
                return BoundaryEvaluation(
                    episode=None,
                    previous_status=previous_status,
                    transitioned=True,
                    diagnostics=self._attach_handoff_diagnostics(
                        diagnostics, closed_episode, None, handoff_reason
                    ),
                    closed_episode=closed_episode,
                )
            diagnostics = self._diagnostics(
                None,
                evidence,
                auction_state,
                transition_reason="NO_ELIGIBLE_DYNAMIC_BOUNDARY",
                observation=eligible_observation,
            )
            return BoundaryEvaluation(
                episode=None,
                previous_status=previous_status,
                transitioned=False,
                diagnostics=self._attach_handoff_diagnostics(
                    diagnostics, None, None, None
                ),
            )

        # A newer dynamic range supersedes the old non-terminal episode. Close
        # the old event and immediately evaluate the replacement on the same bar.
        if eligible_observation is not None and self._is_newer_range(memory, eligible_observation):
            memory.last_seen_time = ts
            replacement_structural_key = self._structural_key(evidence, eligible_observation)
            self._transition_terminal(
                memory,
                BoundaryEpisodeStatus.SUPERSEDED,
                "SUPERSEDED_BY_NEW_DYNAMIC_RANGE",
                superseded_by=replacement_structural_key,
            )
            self._archive_terminal(memory, reset_reason="SUPERSEDED_BY_NEW_DYNAMIC_RANGE")
            closed_episode = self._to_contract(memory, evidence, auction_state)
            self._current.pop(symbol, None)
            replacement = self._start_if_relevant(evidence, eligible_observation, auction_state)
            active_episode: Optional[BoundaryEpisode] = None
            if replacement is not None:
                self._current[symbol] = replacement
                active_episode = self._to_contract(replacement, evidence, auction_state)
            diagnostics = self._diagnostics(
                replacement,
                evidence,
                auction_state,
                transition_reason="SUPERSEDED_AND_REPLACEMENT_EVALUATED_SAME_SNAPSHOT",
                observation=eligible_observation,
            )
            return BoundaryEvaluation(
                episode=active_episode,
                previous_status=previous_status,
                transitioned=True,
                diagnostics=self._attach_handoff_diagnostics(
                    diagnostics,
                    closed_episode,
                    active_episode,
                    "SUPERSEDED_AND_REPLACEMENT_EVALUATED_SAME_SNAPSHOT",
                ),
                closed_episode=closed_episode,
            )

        # A nearest-edge switch while still APPROACHING is also a lifecycle
        # handoff. Close the provisional edge and evaluate the newly observed
        # opposite edge on the same snapshot, including an immediate attempt.
        if (
            memory.status is BoundaryEpisodeStatus.APPROACHING
            and eligible_observation is not None
            and self._new_range_key(eligible_observation) == self._memory_range_key(memory)
            and eligible_observation.boundary_side is not memory.boundary_side
        ):
            memory.last_seen_time = ts
            self._transition_terminal(
                memory,
                BoundaryEpisodeStatus.STALE,
                "APPROACH_EDGE_CHANGED_BEFORE_ATTEMPT",
            )
            self._archive_terminal(
                memory, reset_reason="APPROACH_EDGE_REPLACED_SAME_SNAPSHOT"
            )
            closed_episode = self._to_contract(memory, evidence, auction_state)
            self._current.pop(symbol, None)
            replacement = self._start_if_relevant(evidence, eligible_observation, auction_state)
            active_episode = None
            if replacement is not None:
                self._current[symbol] = replacement
                active_episode = self._to_contract(replacement, evidence, auction_state)
            diagnostics = self._diagnostics(
                replacement,
                evidence,
                auction_state,
                transition_reason="APPROACH_EDGE_REPLACED_SAME_SNAPSHOT",
                observation=eligible_observation,
            )
            return BoundaryEvaluation(
                episode=active_episode,
                previous_status=previous_status,
                transitioned=True,
                diagnostics=self._attach_handoff_diagnostics(
                    diagnostics,
                    closed_episode,
                    active_episode,
                    "APPROACH_EDGE_REPLACED_SAME_SNAPSHOT",
                ),
                closed_episode=closed_episode,
            )

        memory.last_seen_time = ts
        if eligible_observation is None:
            memory.missing_boundary_bars += 1
        else:
            memory.missing_boundary_bars = 0

        if memory.missing_boundary_bars >= self.cfg.range_missing_stale_bars:
            self._transition_terminal(memory, BoundaryEpisodeStatus.STALE, "DYNAMIC_RANGE_STALE_OR_MISSING")
        elif (
            memory.status is BoundaryEpisodeStatus.FAILURE_BUILDING
            and memory.failure_building_since is not None
            and ts - memory.failure_building_since
            > timedelta(minutes=self.cfg.failure_watch_valid_minutes)
        ):
            self._transition_terminal(memory, BoundaryEpisodeStatus.EXPIRED, "FAILURE_BUILDING_WATCH_EXPIRED")
        elif ts - memory.last_activity_time > timedelta(minutes=self.cfg.episode_idle_expiry_minutes):
            self._transition_terminal(memory, BoundaryEpisodeStatus.EXPIRED, "BOUNDARY_EPISODE_IDLE_EXPIRED")
        elif memory.status is BoundaryEpisodeStatus.APPROACHING:
            self._update_approach(memory, evidence, eligible_observation)
        else:
            self._update_attempted_episode(memory, evidence)

        transitioned = previous_status is not memory.status
        if memory.terminal and memory.status in {
            BoundaryEpisodeStatus.EXPIRED,
            BoundaryEpisodeStatus.STALE,
            BoundaryEpisodeStatus.SUPERSEDED,
        }:
            self._archive_terminal(memory, reset_reason=memory.terminal_reason)
            closed_episode = self._to_contract(memory, evidence, auction_state)
            self._current.pop(symbol, None)
            diagnostics = self._diagnostics(
                None,
                evidence,
                auction_state,
                transition_reason=memory.last_transition_reason,
                observation=eligible_observation,
            )
            return BoundaryEvaluation(
                episode=None,
                previous_status=previous_status,
                transitioned=transitioned,
                diagnostics=self._attach_handoff_diagnostics(
                    diagnostics, closed_episode, None, memory.last_transition_reason
                ),
                closed_episode=closed_episode,
            )

        episode = self._to_contract(memory, evidence, auction_state)
        diagnostics = self._diagnostics(
            memory,
            evidence,
            auction_state,
            transition_reason=memory.last_transition_reason,
            observation=eligible_observation,
        )
        return BoundaryEvaluation(
            episode=episode,
            previous_status=previous_status,
            transitioned=transitioned,
            diagnostics=self._attach_handoff_diagnostics(
                diagnostics, None, episode, None
            ),
        )

    def _start_if_relevant(
        self,
        evidence: EvidenceSnapshot,
        observation: Optional[BoundaryObservation],
        auction_state: AuctionState,
    ) -> Optional[_EpisodeMemory]:
        if observation is None or not self._has_usable_range(observation):
            return None
        distance = observation.distance_atr
        offsets = self._offsets(evidence, observation.boundary_side, observation.boundary_price)
        attempt_now = self._attempt_now(offsets)
        if not attempt_now and (distance is None or distance > self.cfg.approach_distance_atr):
            return None

        structural_key = self._structural_key(evidence, observation)
        seq_key = (evidence.symbol, structural_key)
        sequence = self._sequences.get(seq_key, 0) + 1
        self._sequences[seq_key] = sequence
        event_key = stable_key(
            "BOUNDARY_EVENT",
            evidence.symbol,
            evidence.trading_day,
            structural_key,
            sequence,
            evidence.snapshot_time,
        )
        attempt_id = stable_key("BOUNDARY_ATTEMPT", event_key)
        range_id = observation.range_id or stable_key(
            "DYNAMIC_RANGE",
            evidence.symbol,
            evidence.trading_day,
            observation.range_low,
            observation.range_high,
            observation.range_start_time or evidence.snapshot_time,
        )
        breakout_side = TradeSide.BUY if observation.boundary_side is BoundarySide.UPPER else TradeSide.SELL
        memory = _EpisodeMemory(
            event_key=event_key,
            structural_key=structural_key,
            attempt_id=attempt_id,
            sequence=sequence,
            symbol=evidence.symbol,
            trading_day=evidence.trading_day,
            first_seen_time=evidence.snapshot_time,
            last_seen_time=evidence.snapshot_time,
            last_activity_time=evidence.snapshot_time,
            event_time=evidence.snapshot_time,
            boundary_id=observation.boundary_id,
            boundary_side=observation.boundary_side,
            boundary_source=observation.boundary_source,
            boundary_price=observation.boundary_price,
            breakout_side=breakout_side,
            failure_side=breakout_side.opposite,
            range_id=range_id,
            range_version=observation.range_version or 1,
            range_source=observation.boundary_source,
            range_low=float(observation.range_low),
            range_high=float(observation.range_high),
            range_start_time=observation.range_start_time or evidence.snapshot_time,
            range_end_time=observation.range_end_time,
            range_basis=observation.range_basis or "DYNAMIC_RANGE",
            range_quality_score=observation.range_quality_score,
            frozen_at=evidence.snapshot_time,
            range_frozen=False,
            status=BoundaryEpisodeStatus.APPROACHING,
            current_offset_atr=offsets["close_offset_atr"],
            last_close=evidence.close,
        )
        if attempt_now:
            self._freeze_attempt(memory, evidence, observation, offsets)
        else:
            memory.last_transition_reason = "APPROACHING_DYNAMIC_BOUNDARY"
        return memory

    def _update_approach(
        self,
        memory: _EpisodeMemory,
        evidence: EvidenceSnapshot,
        observation: Optional[BoundaryObservation],
    ) -> None:
        memory.last_seen_time = evidence.snapshot_time
        if observation is None:
            return
        same_range = self._new_range_key(observation) == self._memory_range_key(memory)
        if same_range and observation.boundary_side is not memory.boundary_side:
            self._transition_terminal(memory, BoundaryEpisodeStatus.STALE, "APPROACH_EDGE_CHANGED_BEFORE_ATTEMPT")
            return
        if not same_range:
            return

        # Before the attempt, the current dynamic range can still evolve.  The
        # range and boundary are frozen only by _freeze_attempt().
        memory.boundary_id = observation.boundary_id
        memory.boundary_source = observation.boundary_source
        memory.boundary_price = observation.boundary_price
        memory.range_low = float(observation.range_low)
        memory.range_high = float(observation.range_high)
        memory.range_start_time = observation.range_start_time or memory.range_start_time
        memory.range_end_time = observation.range_end_time
        memory.range_basis = observation.range_basis or memory.range_basis
        memory.range_quality_score = observation.range_quality_score
        # APPROACHING is provisional. Keep the contract chronology valid while
        # the dynamic range is still allowed to evolve; the true immutable
        # frozen_at is set by _freeze_attempt().
        memory.frozen_at = evidence.snapshot_time
        offsets = self._offsets(evidence, memory.boundary_side, memory.boundary_price)
        memory.current_offset_atr = offsets["close_offset_atr"]
        if abs(offsets["close_offset_atr"]) <= self.cfg.approach_distance_atr:
            memory.last_activity_time = evidence.snapshot_time
        if self._attempt_now(offsets):
            self._freeze_attempt(memory, evidence, observation, offsets)
        else:
            memory.last_transition_reason = "APPROACHING_DYNAMIC_BOUNDARY"
        memory.last_close = evidence.close

    def _freeze_attempt(
        self,
        memory: _EpisodeMemory,
        evidence: EvidenceSnapshot,
        observation: BoundaryObservation,
        offsets: Dict[str, float],
    ) -> None:
        memory.range_frozen = True
        memory.frozen_at = evidence.snapshot_time
        memory.attempt_time = evidence.snapshot_time
        memory.boundary_id = observation.boundary_id
        memory.boundary_source = observation.boundary_source
        memory.boundary_price = observation.boundary_price
        memory.range_low = float(observation.range_low)
        memory.range_high = float(observation.range_high)
        memory.range_start_time = observation.range_start_time or memory.range_start_time
        memory.range_end_time = observation.range_end_time
        memory.range_basis = observation.range_basis or memory.range_basis
        memory.range_quality_score = observation.range_quality_score
        memory.status = BoundaryEpisodeStatus.OUTSIDE_ATTEMPT
        memory.last_transition_reason = "FIRST_GENUINE_OUTSIDE_ATTEMPT"
        memory.last_activity_time = evidence.snapshot_time
        self._update_excursion_counts(memory, evidence, offsets, count_acceptance=False)

    def _update_attempted_episode(self, memory: _EpisodeMemory, evidence: EvidenceSnapshot) -> None:
        ts = evidence.snapshot_time
        offsets = self._offsets(evidence, memory.boundary_side, memory.boundary_price)
        memory.last_seen_time = ts
        memory.current_offset_atr = offsets["close_offset_atr"]
        previous_status = memory.status
        self._update_excursion_counts(memory, evidence, offsets, count_acceptance=True)

        genuine_attempt = bool(
            memory.max_outside_excursion_atr >= self.cfg.attempt_excursion_atr
            and (
                memory.total_outside_closes >= 1
                or memory.max_outside_excursion_atr >= self.cfg.strong_wick_excursion_atr
            )
        )
        meaningful_reentry = bool(
            genuine_attempt
            and offsets["close_offset_atr"] <= -self.cfg.failure_reentry_depth_atr
        )
        acceptance_close = offsets["close_offset_atr"] >= self.cfg.acceptance_close_buffer_atr

        if self._detect_retest(memory, evidence):
            memory.retest_detected = True

        if meaningful_reentry:
            if memory.first_reentry_time is None:
                memory.first_reentry_time = ts
                memory.reentry_close = evidence.close
                memory.failure_building_since = ts
            memory.consecutive_inside_closes += 1
            memory.consecutive_acceptance_closes = 0
            depth = abs(offsets["close_offset_atr"])
            memory.reentry_depth_atr = max(memory.reentry_depth_atr or 0.0, depth)
            memory.failure_followthrough_atr = max(
                memory.failure_followthrough_atr,
                self._failure_followthrough(memory, evidence),
            )
            memory.status = BoundaryEpisodeStatus.FAILURE_BUILDING
            memory.last_transition_reason = "MEANINGFUL_REENTRY_INTO_FROZEN_RANGE"
            memory.last_activity_time = ts
            if memory.consecutive_inside_closes >= self.cfg.failure_required_inside_closes:
                directional_followthrough = bool(
                    memory.failure_followthrough_atr >= self.cfg.failure_followthrough_atr
                )
                deep_reentry_hold = bool(
                    (memory.reentry_depth_atr or 0.0)
                    >= self.cfg.failure_reentry_depth_atr
                    * self.cfg.failure_deep_reentry_multiplier
                )
                if directional_followthrough or deep_reentry_hold:
                    memory.failed_time = ts
                    memory.resolution_time = ts
                    memory.terminal_time = ts
                    memory.resolution = BoundaryResolution.FAILED
                    memory.status = BoundaryEpisodeStatus.FAILED
                    memory.terminal = True
                    if directional_followthrough:
                        memory.failure_resolution_basis = "DIRECTIONAL_FOLLOWTHROUGH"
                        memory.terminal_reason = (
                            "REENTRY_HOLD_AND_DIRECTIONAL_FOLLOWTHROUGH_CONFIRMED"
                        )
                    else:
                        memory.failure_resolution_basis = "DEEP_REENTRY_INSIDE_HOLD"
                        memory.terminal_reason = (
                            "DEEP_REENTRY_AND_INSIDE_HOLD_CONFIRMED"
                        )
                    memory.last_transition_reason = memory.terminal_reason
                    memory.emitted_resolutions = (BoundaryResolution.FAILED.value,)
        elif acceptance_close:
            memory.consecutive_inside_closes = 0
            if memory.acceptance_building_since is None:
                memory.acceptance_building_since = ts
            memory.status = BoundaryEpisodeStatus.ACCEPTANCE_BUILDING
            memory.last_transition_reason = "SUSTAINED_CLOSE_OUTSIDE_FROZEN_BOUNDARY"
            memory.last_activity_time = ts
            if memory.consecutive_acceptance_closes >= self.cfg.acceptance_required_outside_closes:
                memory.accepted_time = ts
                memory.resolution_time = ts
                memory.terminal_time = ts
                memory.resolution = BoundaryResolution.ACCEPTED
                memory.status = BoundaryEpisodeStatus.ACCEPTED
                memory.terminal = True
                memory.terminal_reason = "SUSTAINED_OUTSIDE_TRADE_CONFIRMED"
                memory.last_transition_reason = memory.terminal_reason
                memory.emitted_resolutions = (BoundaryResolution.ACCEPTED.value,)
        else:
            memory.consecutive_acceptance_closes = 0
            if offsets["close_offset_atr"] < 0.0:
                memory.consecutive_inside_closes = 0
            if previous_status in {
                BoundaryEpisodeStatus.OUTSIDE_ATTEMPT,
                BoundaryEpisodeStatus.ACCEPTANCE_BUILDING,
                BoundaryEpisodeStatus.FAILURE_BUILDING,
            }:
                memory.status = BoundaryEpisodeStatus.UNRESOLVED
                memory.last_transition_reason = "BOUNDARY_ATTEMPT_REMAINS_UNRESOLVED"

        current_activity = bool(
            abs(offsets["close_offset_atr"]) <= self.cfg.approach_distance_atr
            or offsets["extreme_offset_atr"] >= self.cfg.attempt_excursion_atr
            or meaningful_reentry
            or acceptance_close
        )
        if current_activity:
            memory.last_activity_time = ts
        memory.last_close = evidence.close

    def _update_excursion_counts(
        self,
        memory: _EpisodeMemory,
        evidence: EvidenceSnapshot,
        offsets: Dict[str, float],
        *,
        count_acceptance: bool,
    ) -> None:
        ts = evidence.snapshot_time
        close_offset = offsets["close_offset_atr"]
        extreme_offset = offsets["extreme_offset_atr"]
        memory.current_offset_atr = close_offset
        memory.max_outside_excursion_atr = max(memory.max_outside_excursion_atr, extreme_offset)
        memory.max_close_outside_atr = max(memory.max_close_outside_atr, max(0.0, close_offset))
        if close_offset > 0.0:
            memory.total_outside_closes += 1
            memory.consecutive_outside_closes += 1
            memory.first_outside_close_time = memory.first_outside_close_time or ts
            memory.last_outside_time = ts
        else:
            memory.consecutive_outside_closes = 0
        if count_acceptance and close_offset >= self.cfg.acceptance_close_buffer_atr:
            memory.consecutive_acceptance_closes += 1
        elif count_acceptance:
            memory.consecutive_acceptance_closes = 0

    def _detect_retest(self, memory: _EpisodeMemory, evidence: EvidenceSnapshot) -> bool:
        if memory.first_outside_close_time is None:
            return False
        tolerance = self.cfg.acceptance_retest_tolerance_atr * (evidence.atr or 0.0)
        if memory.boundary_side is BoundarySide.UPPER:
            touched = evidence.bar.low <= memory.boundary_price + tolerance
            held = evidence.close >= memory.boundary_price - tolerance
        else:
            touched = evidence.bar.high >= memory.boundary_price - tolerance
            held = evidence.close <= memory.boundary_price + tolerance
        return bool(touched and held)

    def _failure_followthrough(self, memory: _EpisodeMemory, evidence: EvidenceSnapshot) -> float:
        if memory.reentry_close is None or not evidence.atr:
            return 0.0
        if memory.failure_side is TradeSide.SELL:
            return max(0.0, (memory.reentry_close - evidence.close) / evidence.atr)
        return max(0.0, (evidence.close - memory.reentry_close) / evidence.atr)

    def _update_terminal_reset(self, memory: _EpisodeMemory, evidence: EvidenceSnapshot) -> None:
        if memory.resolution_time is not None and evidence.snapshot_time > memory.resolution_time:
            memory.post_terminal_protection_bars += 1
        offsets = self._offsets(evidence, memory.boundary_side, memory.boundary_price)
        materially_inside = offsets["close_offset_atr"] <= -self.cfg.terminal_reset_depth_atr
        if materially_inside:
            memory.reset_inside_closes += 1
            memory.reset_started_at = memory.reset_started_at or evidence.snapshot_time
        else:
            memory.reset_inside_closes = 0
            memory.reset_started_at = None
        memory.last_seen_time = evidence.snapshot_time
        memory.current_offset_atr = offsets["close_offset_atr"]
        memory.last_close = evidence.close

    def _transition_terminal(
        self,
        memory: _EpisodeMemory,
        status: BoundaryEpisodeStatus,
        reason: str,
        *,
        superseded_by: Optional[str] = None,
    ) -> None:
        memory.status = status
        memory.terminal = True
        memory.terminal_time = memory.last_seen_time
        memory.terminal_reason = reason
        memory.last_transition_reason = reason
        if status is BoundaryEpisodeStatus.SUPERSEDED:
            memory.superseded = True
            memory.superseded_by = superseded_by

    def _archive_terminal(self, memory: _EpisodeMemory, *, reset_reason: Optional[str] = None) -> None:
        memory.archive_time = memory.last_seen_time
        memory.archive_reason = reset_reason or memory.terminal_reason or "EPISODE_ARCHIVED"
        self._last_terminal[memory.symbol] = {
            "event_key": memory.event_key,
            "attempt_id": memory.attempt_id,
            "status": memory.status.value,
            "resolution": memory.resolution.value,
            "resolution_basis": memory.failure_resolution_basis,
            "terminal_reason": memory.terminal_reason,
            "terminal_time": memory.terminal_time or memory.accepted_time or memory.failed_time or memory.last_seen_time,
            "resolution_time": memory.resolution_time,
            "archive_reason": memory.archive_reason,
            "archive_time": memory.archive_time,
            "post_terminal_protection_bars": memory.post_terminal_protection_bars,
            "range_id": memory.range_id,
            "range_version": memory.range_version,
            "boundary_side": memory.boundary_side.value,
            "boundary_price": memory.boundary_price,
        }

    def _to_contract(
        self,
        memory: _EpisodeMemory,
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
    ) -> BoundaryEpisode:
        acceptance_facts = []
        failure_facts = []
        if memory.total_outside_closes:
            acceptance_facts.append(self._fact(
                "BOUNDARY_OUTSIDE_CLOSES_OBSERVED",
                evidence,
                memory.total_outside_closes,
                "boundary.total_outside_closes",
            ))
        if memory.retest_detected:
            acceptance_facts.append(self._fact(
                "BOUNDARY_RETEST_HOLD_OBSERVED",
                evidence,
                True,
                "boundary.retest_detected",
            ))
        if memory.first_reentry_time is not None:
            failure_facts.append(self._fact(
                "BOUNDARY_MEANINGFUL_REENTRY_OBSERVED",
                evidence,
                memory.reentry_depth_atr,
                "boundary.reentry_depth_atr",
            ))
        if memory.consecutive_inside_closes:
            failure_facts.append(self._fact(
                "BOUNDARY_INSIDE_HOLD_OBSERVED",
                evidence,
                memory.consecutive_inside_closes,
                "boundary.consecutive_inside_closes",
            ))

        return BoundaryEpisode(
            event_key=memory.event_key,
            structural_key=memory.structural_key,
            attempt_id=memory.attempt_id,
            episode_sequence=memory.sequence,
            symbol=memory.symbol,
            trading_day=memory.trading_day,
            snapshot_time=evidence.snapshot_time,
            event_time=memory.event_time,
            first_seen_time=memory.first_seen_time,
            last_seen_time=evidence.snapshot_time,
            attempt_time=memory.attempt_time,
            first_outside_close_time=memory.first_outside_close_time,
            last_outside_time=memory.last_outside_time,
            first_reentry_time=memory.first_reentry_time,
            boundary_id=memory.boundary_id,
            boundary_side=memory.boundary_side,
            boundary_source=memory.boundary_source,
            boundary_price=memory.boundary_price,
            breakout_side=memory.breakout_side,
            failure_side=memory.failure_side,
            frozen_range=FrozenRange(
                range_id=memory.range_id,
                range_version=memory.range_version,
                source=memory.range_source,
                low=memory.range_low,
                high=memory.range_high,
                start_time=memory.range_start_time,
                end_time=memory.range_end_time,
                frozen_at=memory.frozen_at,
                basis=memory.range_basis,
                quality_score=memory.range_quality_score,
                diagnostics={
                    "range_frozen": memory.range_frozen,
                    "auction_state_at_snapshot": auction_state.current_state.value,
                },
            ),
            status=memory.status,
            resolution=memory.resolution,
            acceptance_building_since=memory.acceptance_building_since,
            failure_building_since=memory.failure_building_since,
            accepted_time=memory.accepted_time,
            failed_time=memory.failed_time,
            expires_at=memory.last_activity_time + timedelta(minutes=self.cfg.episode_idle_expiry_minutes),
            current_offset_atr=memory.current_offset_atr,
            max_outside_excursion_atr=memory.max_outside_excursion_atr,
            max_close_outside_atr=memory.max_close_outside_atr,
            total_outside_closes=memory.total_outside_closes,
            consecutive_outside_closes=memory.consecutive_outside_closes,
            consecutive_inside_closes=memory.consecutive_inside_closes,
            reentry_depth_atr=memory.reentry_depth_atr,
            retest_detected=memory.retest_detected,
            reset_inside_closes=memory.reset_inside_closes,
            reset_started_at=memory.reset_started_at,
            acceptance_evidence=tuple(acceptance_facts),
            failure_evidence=tuple(failure_facts),
            reason_codes=(memory.last_transition_reason,),
            terminal=memory.terminal,
            consumed=memory.consumed,
            superseded=memory.superseded,
            terminal_reason=memory.terminal_reason,
            superseded_by=memory.superseded_by,
            emitted_resolutions=memory.emitted_resolutions,
            diagnostics={
                "range_frozen": memory.range_frozen,
                "consecutive_acceptance_closes": memory.consecutive_acceptance_closes,
                "failure_followthrough_atr": memory.failure_followthrough_atr,
                "failure_resolution_basis": memory.failure_resolution_basis,
                "resolution_time": memory.resolution_time,
                "terminal_time": memory.terminal_time,
                "archive_time": memory.archive_time,
                "archive_reason": memory.archive_reason,
                "post_terminal_protection_bars": memory.post_terminal_protection_bars,
                "missing_boundary_bars": memory.missing_boundary_bars,
                "last_activity_time": memory.last_activity_time,
                "auction_state": auction_state.current_state.value,
            },
            config_version=self.version,
        )

    def _diagnostics(
        self,
        memory: Optional[_EpisodeMemory],
        evidence: EvidenceSnapshot,
        auction_state: AuctionState,
        *,
        transition_reason: str,
        observation: Optional[BoundaryObservation],
    ) -> Dict[str, Any]:
        diagnostics: Dict[str, Any] = {
            "report_only": True,
            "transition_reason": transition_reason,
            "auction_state": auction_state.current_state.value,
            "observation_allowed": observation is not None,
            "observed_boundary_id": observation.boundary_id if observation else None,
            "observed_boundary_side": observation.boundary_side.value if observation else None,
            "observed_boundary_price": observation.boundary_price if observation else None,
            "observed_boundary_source": observation.boundary_source if observation else None,
            "observed_boundary_offset_atr": observation.current_offset_atr if observation else None,
            "observed_range_id": observation.range_id if observation else None,
            "observed_range_version": observation.range_version if observation else None,
            "observed_range_low": observation.range_low if observation else None,
            "observed_range_high": observation.range_high if observation else None,
            "last_terminal": self._last_terminal.get(evidence.symbol),
        }
        if memory is None:
            return diagnostics
        diagnostics.update({
            "event_key": memory.event_key,
            "attempt_id": memory.attempt_id,
            "episode_sequence": memory.sequence,
            "status": memory.status.value,
            "resolution": memory.resolution.value,
            "range_frozen": memory.range_frozen,
            "frozen_range_id": memory.range_id,
            "frozen_range_version": memory.range_version,
            "frozen_range_low": memory.range_low,
            "frozen_range_high": memory.range_high,
            "episode_boundary_id": memory.boundary_id,
            "episode_boundary_side": memory.boundary_side.value,
            "episode_boundary_price": memory.boundary_price,
            "episode_boundary_source": memory.boundary_source,
            "boundary_side": memory.boundary_side.value,
            "boundary_price": memory.boundary_price,
            "current_offset_atr": memory.current_offset_atr,
            "max_outside_excursion_atr": memory.max_outside_excursion_atr,
            "max_close_outside_atr": memory.max_close_outside_atr,
            "total_outside_closes": memory.total_outside_closes,
            "consecutive_outside_closes": memory.consecutive_outside_closes,
            "consecutive_acceptance_closes": memory.consecutive_acceptance_closes,
            "consecutive_inside_closes": memory.consecutive_inside_closes,
            "reentry_depth_atr": memory.reentry_depth_atr,
            "failure_followthrough_atr": memory.failure_followthrough_atr,
            "failure_resolution_basis": memory.failure_resolution_basis,
            "resolution_time": memory.resolution_time,
            "terminal_time": memory.terminal_time,
            "archive_time": memory.archive_time,
            "archive_reason": memory.archive_reason,
            "post_terminal_protection_bars": memory.post_terminal_protection_bars,
            "retest_detected": memory.retest_detected,
            "missing_boundary_bars": memory.missing_boundary_bars,
            "reset_inside_closes": memory.reset_inside_closes,
            "last_activity_time": memory.last_activity_time,
            "terminal": memory.terminal,
            "terminal_reason": memory.terminal_reason,
            "superseded_by": memory.superseded_by,
        })
        return diagnostics

    @staticmethod
    def _attach_handoff_diagnostics(
        diagnostics: Dict[str, Any],
        closed_episode: Optional[BoundaryEpisode],
        active_episode: Optional[BoundaryEpisode],
        handoff_reason: Optional[str],
    ) -> Dict[str, Any]:
        enriched = dict(diagnostics)
        enriched.update({
            "handoff_occurred": bool(closed_episode is not None and active_episode is not None),
            "handoff_reason": handoff_reason,
            "active_event_key": active_episode.event_key if active_episode else None,
            "active_episode_status": active_episode.status.value if active_episode else None,
            "closed_event_key": closed_episode.event_key if closed_episode else None,
            "closed_episode_status": closed_episode.status.value if closed_episode else None,
            "closed_episode": (
                closed_episode.to_storage_dict(exclude_none=False)
                if closed_episode is not None
                else None
            ),
        })
        return enriched

    def _observation_allowed(self, observation: Optional[BoundaryObservation]) -> bool:
        if observation is None or not self._has_usable_range(observation):
            return False
        if not self.cfg.dynamic_boundaries_only:
            return True
        source = str(observation.boundary_source or "").strip().upper()
        return any(token in source for token in self.cfg.allowed_dynamic_sources)

    @staticmethod
    def _has_usable_range(observation: BoundaryObservation) -> bool:
        return bool(
            observation.range_low is not None
            and observation.range_high is not None
            and observation.range_high > observation.range_low
        )

    def _attempt_now(self, offsets: Dict[str, float]) -> bool:
        return bool(
            offsets["close_offset_atr"] > 0.0
            or offsets["extreme_offset_atr"] >= self.cfg.attempt_excursion_atr
        )

    @staticmethod
    def _offsets(
        evidence: EvidenceSnapshot,
        side: BoundarySide,
        boundary_price: float,
    ) -> Dict[str, float]:
        atr = evidence.atr or max(boundary_price * 0.001, 1e-9)
        if side is BoundarySide.UPPER:
            close_offset = (evidence.close - boundary_price) / atr
            extreme_offset = max(0.0, (evidence.bar.high - boundary_price) / atr)
        else:
            close_offset = (boundary_price - evidence.close) / atr
            extreme_offset = max(0.0, (boundary_price - evidence.bar.low) / atr)
        return {
            "close_offset_atr": float(close_offset),
            "extreme_offset_atr": float(extreme_offset),
        }

    def _structural_key(
        self,
        evidence: EvidenceSnapshot,
        observation: BoundaryObservation,
    ) -> str:
        return stable_key(
            "BOUNDARY_STRUCTURE",
            evidence.symbol,
            evidence.trading_day,
            observation.range_id or "NO_RANGE_ID",
            observation.range_version or 1,
            observation.boundary_side,
            round(observation.boundary_price, 8),
        )

    @staticmethod
    def _new_range_key(observation: Optional[BoundaryObservation]) -> Optional[Tuple[str, int]]:
        if observation is None:
            return None
        return (str(observation.range_id or ""), int(observation.range_version or 1))

    @staticmethod
    def _memory_range_key(memory: _EpisodeMemory) -> Tuple[str, int]:
        return (memory.range_id, memory.range_version)

    def _is_newer_range(self, memory: _EpisodeMemory, observation: BoundaryObservation) -> bool:
        new_key = self._new_range_key(observation)
        old_key = self._memory_range_key(memory)
        if new_key == old_key:
            return False
        if observation.range_id == memory.range_id:
            return int(observation.range_version or 1) > memory.range_version
        observed_start = observation.range_start_time
        if observed_start is not None:
            return observed_start >= memory.range_start_time
        return True

    @staticmethod
    def _fact(
        code: str,
        evidence: EvidenceSnapshot,
        value: Any,
        source_path: str,
    ) -> EvidenceFact:
        return EvidenceFact(
            code=code,
            domain="boundary",
            polarity=EvidencePolarity.SUPPORT,
            observed_at=evidence.snapshot_time,
            value=value,
            source_path=source_path,
            quality=QualityStatus.GOOD,
        )


__all__ = [
    "BoundaryChronologyError",
    "BoundaryEvaluation",
    "BoundaryEpisodeEngine",
]
