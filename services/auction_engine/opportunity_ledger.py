"""In-memory stock-day operational-opportunity ledger for Phase 4A.1.

The ledger is a factual projection over candidate aliases and source boundary
lifecycle.  It does not discover setups, recalculate eligibility or choose a
trade side.  Candidate-level terminal states are retained inside
``candidate_interpretations``; the top-level opportunity becomes terminal only
when no live interpretation can still emerge from the source boundary event.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.auction_engine.contracts import (
    BoundaryEpisode,
    BoundaryEpisodeStatus,
    BoundaryResolution,
    CandidateEligibility,
    SetupCandidate,
    TradeSide,
)


_TERMINAL = {"INELIGIBLE", "EXPIRED", "SUPERSEDED", "CONSUMED"}
_NONTERMINAL_BOUNDARY = {
    BoundaryEpisodeStatus.APPROACHING,
    BoundaryEpisodeStatus.OUTSIDE_ATTEMPT,
    BoundaryEpisodeStatus.UNRESOLVED,
    BoundaryEpisodeStatus.ACCEPTANCE_BUILDING,
    BoundaryEpisodeStatus.FAILURE_BUILDING,
}
_ROLE_RANK = {
    "EARLY_INITIATION": 0,
    "ACCEPTED_RESOLUTION_ENTRY": 1,
    "FAILED_RESOLUTION_ENTRY": 1,
    "REVERSAL_ENTRY": 0,
    "CONTINUATION_INTERPRETATION": 2,
}
_ELIGIBILITY_RANK = {
    CandidateEligibility.ELIGIBLE: 0,
    CandidateEligibility.WATCH: 1,
    CandidateEligibility.INELIGIBLE: 2,
    CandidateEligibility.EXPIRED: 3,
    CandidateEligibility.SUPERSEDED: 4,
    CandidateEligibility.CONSUMED: 5,
}
_SUBTYPE_RANK = {
    # Once opposite structural control is established, the open-ended normal
    # reversal is the primary interpretation.  The earlier exhaustion
    # interpretation remains in candidate history as the causal precursor.
    "NORMAL_REVERSAL": 0,
    "EXHAUSTION_REVERSAL": 1,
}


@dataclass
class OpportunityEvent:
    opportunity_key: str
    symbol: str
    trading_day: date
    event_time: datetime
    event_type: str
    previous_state: Optional[str]
    new_state: str
    candidate_id: Optional[str] = None
    reason_codes: Tuple[str, ...] = ()
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "opportunity_key": self.opportunity_key,
            "symbol": self.symbol,
            "trading_day": self.trading_day.isoformat(),
            "event_time": self.event_time.isoformat(sep=" "),
            "event_type": self.event_type,
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "candidate_id": self.candidate_id,
            "reason_codes": list(self.reason_codes),
            "details": dict(self.details),
        }


@dataclass
class OpportunityRecord:
    opportunity_key: str
    symbol: str
    trading_day: date
    side: TradeSide
    boundary_event_key: str
    first_observed_time: datetime
    last_observed_time: datetime
    lifecycle_state: str
    primary_candidate: SetupCandidate
    candidates: Dict[str, SetupCandidate] = field(default_factory=dict)
    candidate_ids: List[str] = field(default_factory=list)
    supporting_candidate_ids: List[str] = field(default_factory=list)
    boundary_status: BoundaryEpisodeStatus = BoundaryEpisodeStatus.UNRESOLVED
    boundary_resolution: BoundaryResolution = BoundaryResolution.UNRESOLVED
    boundary_terminal: bool = False
    boundary_terminal_reason: Optional[str] = None
    eligible_time: Optional[datetime] = None
    terminal_time: Optional[datetime] = None
    consumed_time: Optional[datetime] = None
    selected_time: Optional[datetime] = None
    selected_candidate_id: Optional[str] = None
    superseded_time: Optional[datetime] = None
    superseded_by_opportunity_key: Optional[str] = None
    decision_count: int = 0
    reason_codes: Tuple[str, ...] = ()

    @property
    def active_eligible(self) -> bool:
        return self.lifecycle_state == "ELIGIBLE" and self.consumed_time is None

    @property
    def active_watch(self) -> bool:
        if self.lifecycle_state != "WATCH" or self.consumed_time is not None:
            return False
        return any(
            candidate.eligibility is CandidateEligibility.WATCH
            and not candidate.terminal
            for candidate in self.candidates.values()
        )

    def eligible_candidates(self) -> Tuple[SetupCandidate, ...]:
        return tuple(
            sorted(
                (
                    candidate
                    for candidate in self.candidates.values()
                    if candidate.eligibility is CandidateEligibility.ELIGIBLE
                ),
                key=_candidate_sort_key,
            )
        )

    def watch_candidates(self, now: Optional[datetime] = None) -> Tuple[SetupCandidate, ...]:
        rows = []
        for candidate in self.candidates.values():
            if candidate.eligibility is not CandidateEligibility.WATCH or candidate.terminal:
                continue
            if now is not None and candidate.valid_until is not None and candidate.valid_until < now:
                continue
            rows.append(candidate)
        return tuple(sorted(rows, key=_candidate_sort_key))

    def selected_candidate(self) -> Optional[SetupCandidate]:
        eligible = self.eligible_candidates()
        return eligible[0] if eligible else None

    def candidate_interpretations(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for candidate in sorted(self.candidates.values(), key=_candidate_sort_key):
            rows.append({
                "candidate_id": candidate.candidate_id,
                "family": candidate.family.value,
                "subtype": candidate.subtype,
                "role": candidate.candidate_role.value,
                "eligibility": candidate.eligibility.value,
                "candidate_time": candidate.candidate_time.isoformat(sep=" "),
                "last_observed_time": candidate.snapshot_time.isoformat(sep=" "),
                "entry_price": candidate.entry_price,
                "stop_anchor_price": candidate.stop_anchor_price,
                "valid_until": candidate.valid_until.isoformat(sep=" ") if candidate.valid_until else None,
                "terminal": candidate.terminal,
                "blockers": list(candidate.blockers),
                "reason_codes": list(candidate.reason_codes),
                "diagnostics": dict(candidate.diagnostics),
            })
        return rows

    def to_dict(self, event_history: Iterable[OpportunityEvent] = ()) -> Dict[str, Any]:
        c = self.primary_candidate
        return {
            "opportunity_key": self.opportunity_key,
            "symbol": self.symbol,
            "trading_day": self.trading_day.isoformat(),
            "side": self.side.value,
            "boundary_event_key": self.boundary_event_key,
            "boundary_status": self.boundary_status.value,
            "boundary_resolution": self.boundary_resolution.value,
            "boundary_terminal": self.boundary_terminal,
            "boundary_terminal_reason": self.boundary_terminal_reason,
            "lifecycle_state": self.lifecycle_state,
            "first_observed_time": self.first_observed_time.isoformat(sep=" "),
            "last_observed_time": self.last_observed_time.isoformat(sep=" "),
            "eligible_time": self.eligible_time.isoformat(sep=" ") if self.eligible_time else None,
            "terminal_time": self.terminal_time.isoformat(sep=" ") if self.terminal_time else None,
            "selected_time": self.selected_time.isoformat(sep=" ") if self.selected_time else None,
            "selected_candidate_id": self.selected_candidate_id,
            "consumed_time": self.consumed_time.isoformat(sep=" ") if self.consumed_time else None,
            "superseded_time": self.superseded_time.isoformat(sep=" ") if self.superseded_time else None,
            "superseded_by_opportunity_key": self.superseded_by_opportunity_key,
            "decision_count": self.decision_count,
            "primary_candidate_id": c.candidate_id,
            "primary_family": c.family.value,
            "primary_subtype": c.subtype,
            "primary_role": c.candidate_role.value,
            "primary_eligibility": c.eligibility.value,
            "entry_price": c.entry_price,
            "stop_anchor_price": c.stop_anchor_price,
            "candidate_ids": list(self.candidate_ids),
            "supporting_candidate_ids": list(self.supporting_candidate_ids),
            "candidate_interpretations": self.candidate_interpretations(),
            "event_history": [event.to_dict() for event in event_history],
            "reason_codes": list(self.reason_codes),
        }


class OpportunityLedger:
    """Per-symbol/day factual opportunity projection."""

    def __init__(self) -> None:
        self._records: Dict[str, OpportunityRecord] = {}
        self._events: List[OpportunityEvent] = []
        self._last_day: Dict[str, date] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._records.clear()
            self._events.clear()
            self._last_day.clear()
            return
        key = str(symbol).strip().upper()
        self._records = {k: v for k, v in self._records.items() if v.symbol != key}
        self._events = [e for e in self._events if e.symbol != key]
        self._last_day.pop(key, None)

    def update(
        self,
        symbol: str,
        snapshot_time: datetime,
        candidates: Iterable[SetupCandidate],
        *,
        boundary_episode: Optional[BoundaryEpisode] = None,
        closed_episode: Optional[BoundaryEpisode] = None,
    ) -> Tuple[OpportunityRecord, ...]:
        symbol = symbol.upper()
        day = snapshot_time.date()
        prior_day = self._last_day[symbol] if symbol in self._last_day else None
        if prior_day is not None and prior_day != day:
            self.reset(symbol)
        self._last_day[symbol] = day

        touched: set[str] = set()
        candidate_rows = tuple(candidates)
        for candidate in candidate_rows:
            self._upsert(candidate)
            touched.add(candidate.opportunity_key)
        for candidate in candidate_rows:
            if (
                candidate.family.value == "REVERSAL"
                and candidate.eligibility is CandidateEligibility.ELIGIBLE
            ):
                touched.update(self._supersede_opposite_for_reversal(candidate))
        for episode in (boundary_episode, closed_episode):
            if episode is not None:
                touched.update(self._sync_boundary_episode(episode))
        for opportunity_key in touched:
            self._refresh_record(self._records[opportunity_key], snapshot_time)
        return self.records(symbol)

    def _upsert(self, candidate: SetupCandidate) -> None:
        record = (
            self._records[candidate.opportunity_key]
            if candidate.opportunity_key in self._records
            else None
        )
        if record is None:
            record = OpportunityRecord(
                opportunity_key=candidate.opportunity_key,
                symbol=candidate.symbol,
                trading_day=candidate.trading_day,
                side=candidate.side,
                boundary_event_key=candidate.event_key,
                first_observed_time=candidate.snapshot_time,
                last_observed_time=candidate.snapshot_time,
                lifecycle_state="WATCH",
                primary_candidate=candidate,
                candidates={candidate.candidate_id: candidate},
                candidate_ids=[candidate.candidate_id],
                boundary_status=candidate.source_boundary_status,
                boundary_resolution=candidate.source_boundary_resolution,
                boundary_terminal=candidate.source_boundary_status in {
                    BoundaryEpisodeStatus.ACCEPTED,
                    BoundaryEpisodeStatus.FAILED,
                    BoundaryEpisodeStatus.EXPIRED,
                    BoundaryEpisodeStatus.SUPERSEDED,
                    BoundaryEpisodeStatus.STALE,
                },
                reason_codes=tuple(candidate.reason_codes),
            )
            self._records[candidate.opportunity_key] = record
            self._events.append(OpportunityEvent(
                candidate.opportunity_key,
                candidate.symbol,
                candidate.trading_day,
                candidate.snapshot_time,
                "CREATED",
                None,
                "WATCH",
                candidate.candidate_id,
                tuple(candidate.reason_codes),
                {"candidate_eligibility": candidate.eligibility.value},
            ))
        else:
            record.last_observed_time = max(record.last_observed_time, candidate.snapshot_time)
            record.candidates[candidate.candidate_id] = candidate
            if candidate.candidate_id not in record.candidate_ids:
                record.candidate_ids.append(candidate.candidate_id)
            record.reason_codes = tuple(dict.fromkeys((*record.reason_codes, *candidate.reason_codes)))
        self._select_primary(record)

    def _supersede_opposite_for_reversal(
        self,
        reversal: SetupCandidate,
    ) -> Tuple[str, ...]:
        """A confirmed reversal terminates older opposite-side opportunities."""
        touched: List[str] = []
        for key, record in self._records.items():
            if key == reversal.opportunity_key:
                continue
            if record.symbol != reversal.symbol or record.trading_day != reversal.trading_day:
                continue
            if record.side is reversal.side:
                continue
            if record.first_observed_time > reversal.snapshot_time:
                continue
            if record.consumed_time is not None or record.superseded_time is not None:
                continue
            if record.lifecycle_state not in {"WATCH", "ELIGIBLE"}:
                continue

            previous = record.lifecycle_state
            record.superseded_time = reversal.snapshot_time
            record.superseded_by_opportunity_key = reversal.opportunity_key
            record.terminal_time = reversal.snapshot_time
            record.boundary_terminal_reason = "SUPERSEDED_BY_CONFIRMED_REVERSAL"
            record.reason_codes = tuple(dict.fromkeys((
                *record.reason_codes,
                "SUPERSEDED_BY_CONFIRMED_REVERSAL",
                f"REVERSAL_CANDIDATE_{reversal.candidate_id}",
            )))
            touched.append(key)
            self._events.append(OpportunityEvent(
                key,
                record.symbol,
                record.trading_day,
                reversal.snapshot_time,
                "SUPERSEDED_BY_REVERSAL",
                previous,
                "SUPERSEDED",
                reversal.candidate_id,
                ("SUPERSEDED_BY_CONFIRMED_REVERSAL",),
                {
                    "reversal_opportunity_key": reversal.opportunity_key,
                    "reversal_side": reversal.side.value,
                },
            ))
        return tuple(touched)

    def _sync_boundary_episode(self, episode: BoundaryEpisode) -> Tuple[str, ...]:
        touched: List[str] = []
        for opportunity_key, record in self._records.items():
            if record.boundary_event_key != episode.event_key:
                continue
            record.boundary_status = episode.status
            record.boundary_resolution = episode.resolution
            record.boundary_terminal = bool(episode.terminal)
            record.boundary_terminal_reason = episode.terminal_reason
            record.last_observed_time = max(record.last_observed_time, episode.snapshot_time)
            touched.append(opportunity_key)
        return tuple(touched)

    def _refresh_record(self, record: OpportunityRecord, when: datetime) -> None:
        previous = record.lifecycle_state
        new_state = self._derive_state(record)
        self._select_primary(record)
        record.supporting_candidate_ids = [
            candidate_id
            for candidate_id in record.candidate_ids
            if candidate_id != record.primary_candidate.candidate_id
        ]
        if new_state == "ELIGIBLE" and previous != "ELIGIBLE":
            eligible = record.selected_candidate()
            if eligible is not None and record.eligible_time is None:
                record.eligible_time = eligible.snapshot_time
        if new_state in _TERMINAL and previous not in _TERMINAL:
            record.terminal_time = when
        elif new_state not in _TERMINAL:
            # Candidate alias expiry must not leave a stale terminal timestamp on
            # an opportunity whose source boundary can still resolve later.
            record.terminal_time = None
        if new_state != previous:
            record.lifecycle_state = new_state
            self._events.append(OpportunityEvent(
                record.opportunity_key,
                record.symbol,
                record.trading_day,
                when,
                "LIFECYCLE",
                previous,
                new_state,
                record.primary_candidate.candidate_id,
                tuple(record.primary_candidate.reason_codes),
                {
                    "boundary_status": record.boundary_status.value,
                    "boundary_resolution": record.boundary_resolution.value,
                    "candidate_states": {
                        key: value.eligibility.value for key, value in record.candidates.items()
                    },
                },
            ))

    @staticmethod
    def _derive_state(record: OpportunityRecord) -> str:
        if record.consumed_time is not None:
            return "CONSUMED"
        if record.superseded_time is not None:
            return "SUPERSEDED"
        states = {candidate.eligibility for candidate in record.candidates.values()}
        if CandidateEligibility.ELIGIBLE in states:
            return "ELIGIBLE"
        if CandidateEligibility.WATCH in states:
            return "WATCH"
        if record.boundary_status is BoundaryEpisodeStatus.SUPERSEDED:
            return "SUPERSEDED"
        if record.boundary_status in {BoundaryEpisodeStatus.EXPIRED, BoundaryEpisodeStatus.STALE}:
            return "EXPIRED"
        if record.boundary_status in _NONTERMINAL_BOUNDARY and not record.boundary_terminal:
            return "WATCH"
        if states and states <= {CandidateEligibility.EXPIRED}:
            return "EXPIRED"
        if states and states <= {CandidateEligibility.SUPERSEDED}:
            return "SUPERSEDED"
        return "INELIGIBLE"

    @staticmethod
    def _select_primary(record: OpportunityRecord) -> None:
        record.primary_candidate = min(record.candidates.values(), key=_candidate_sort_key)

    def mark_selected(
        self,
        opportunity_key: str,
        when: datetime,
        candidate_id: Optional[str] = None,
    ) -> None:
        record = self._records[opportunity_key]
        if candidate_id is None:
            raise ValueError("candidate_id is required when selecting an opportunity")
        if candidate_id not in record.candidates:
            raise ValueError(
                f"Opportunity {opportunity_key} does not contain candidate {candidate_id}"
            )
        selected = record.candidates[candidate_id]
        if selected.eligibility is not CandidateEligibility.ELIGIBLE:
            raise ValueError(
                f"Opportunity {opportunity_key} candidate {candidate_id} is not ELIGIBLE"
            )
        record.primary_candidate = selected
        record.selected_candidate_id = selected.candidate_id
        record.selected_time = when
        record.decision_count += 1
        self._events.append(OpportunityEvent(
            opportunity_key,
            record.symbol,
            record.trading_day,
            when,
            "DECISION_SELECTED",
            record.lifecycle_state,
            record.lifecycle_state,
            selected.candidate_id,
            ("MANAGER_SELECTED_ELIGIBLE_ALIAS",),
        ))

    def mark_consumed(
        self,
        opportunity_key: str,
        when: datetime,
        reason: str = "REPORT_ONLY_WOULD_CREATE_CONSUMED",
        candidate_id: Optional[str] = None,
    ) -> None:
        record = self._records[opportunity_key]
        previous = record.lifecycle_state
        record.lifecycle_state = "CONSUMED"
        record.consumed_time = when
        record.terminal_time = when
        if candidate_id:
            record.selected_candidate_id = candidate_id
        self._events.append(OpportunityEvent(
            opportunity_key,
            record.symbol,
            record.trading_day,
            when,
            "CONSUMED",
            previous,
            "CONSUMED",
            record.selected_candidate_id or record.primary_candidate.candidate_id,
            (reason,),
        ))

    def records(self, symbol: Optional[str] = None) -> Tuple[OpportunityRecord, ...]:
        rows: Iterable[OpportunityRecord] = self._records.values()
        if symbol is not None:
            key = symbol.upper()
            rows = [record for record in rows if record.symbol == key]
        return tuple(sorted(rows, key=lambda record: (record.first_observed_time, record.opportunity_key)))

    def active_eligible(self, symbol: str) -> Tuple[OpportunityRecord, ...]:
        return tuple(record for record in self.records(symbol) if record.active_eligible)

    def active_watch(self, symbol: str, now: datetime) -> Tuple[OpportunityRecord, ...]:
        return tuple(
            record
            for record in self.records(symbol)
            if record.active_watch and record.watch_candidates(now)
        )

    def events(self, symbol: Optional[str] = None) -> Tuple[OpportunityEvent, ...]:
        rows = self._events if symbol is None else [
            event for event in self._events if event.symbol == symbol.upper()
        ]
        return tuple(rows)

    def record_dicts(self, symbol: Optional[str] = None) -> Tuple[Dict[str, Any], ...]:
        events_by_key: Dict[str, List[OpportunityEvent]] = {}
        for event in self.events(symbol):
            events_by_key.setdefault(event.opportunity_key, []).append(event)
        return tuple(
            record.to_dict(
                events_by_key[record.opportunity_key]
                if record.opportunity_key in events_by_key
                else ()
            )
            for record in self.records(symbol)
        )


def _candidate_sort_key(
    candidate: SetupCandidate,
) -> Tuple[int, int, int, datetime, str]:
    return (
        _ELIGIBILITY_RANK[candidate.eligibility],
        _ROLE_RANK[candidate.candidate_role.value],
        _SUBTYPE_RANK[candidate.subtype]
        if candidate.subtype in _SUBTYPE_RANK
        else 0,
        candidate.candidate_time,
        candidate.candidate_id,
    )


__all__ = ["OpportunityLedger", "OpportunityRecord", "OpportunityEvent"]
