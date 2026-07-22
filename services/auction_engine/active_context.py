"""Read-only active-signal context for Auction evaluation.

The provider loads signal rows once per symbol/day through the existing schema
layer, then applies causal as-of filtering in memory for each replay snapshot.
Trades deliberately do not participate in this layer; user exposure remains a
downstream TradeGenerator/TradeManager responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.datetime_utils import to_ist_naive


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _cmp_time(value: Any) -> Optional[datetime]:
    """Normalize DB-naive and snapshot-aware timestamps to naive IST."""
    return to_ist_naive(value)


def _sort_time(value: Any) -> datetime:
    return _cmp_time(value) or datetime.min


@dataclass(frozen=True)
class ActiveContext:
    equity_ref: str
    snapshot_time: datetime
    active_signal_id: Optional[str] = None
    active_signal_side: Optional[str] = None
    active_signal_setup: Optional[str] = None
    active_signal_stage: Optional[str] = None
    active_signal_status: Optional[str] = None
    active_signal_opportunity_key: Optional[str] = None
    active_signal_entry_price: Optional[float] = None
    active_signal_last_snapshot_time: Optional[datetime] = None
    evaluation_status: str = "AVAILABLE"
    reason_codes: Tuple[str, ...] = ()
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return bool(self.active_signal_id)


class ActiveContextProvider:
    def __init__(self, lifecycle: str = "DEFAULT") -> None:
        self.lifecycle = str(lifecycle or "DEFAULT").strip().upper()
        self._signal_cache: Dict[Tuple[str, date], List[Any]] = {}

    def reset(self) -> None:
        self._signal_cache.clear()

    def evaluate(self, *, equity_ref: str, symbol: str, snapshot_time: datetime) -> ActiveContext:
        ref = str(equity_ref or symbol or "").strip().upper()
        if not ref:
            return ActiveContext(
                equity_ref="",
                snapshot_time=snapshot_time,
                evaluation_status="INVALID",
                reason_codes=("ACTIVE_CONTEXT_EQUITY_REF_MISSING",),
            )
        key = (ref, snapshot_time.date())
        try:
            if key not in self._signal_cache:
                from schemas.signal import SignalSchema
                self._signal_cache[key] = SignalSchema.fetch_for_active_context_day(
                    equity_ref=ref,
                    lifecycle=self.lifecycle,
                    trading_day=snapshot_time.date(),
                )
        except Exception as exc:  # defensive; schema helpers normally absorb errors
            return ActiveContext(
                equity_ref=ref,
                snapshot_time=snapshot_time,
                evaluation_status="ERROR",
                reason_codes=("ACTIVE_CONTEXT_READ_FAILED",),
                diagnostics={"error": f"{type(exc).__name__}: {exc}"},
            )

        active_signals = [
            row for row in self._signal_cache.get(key, ())
            if self._signal_active_as_of(row, snapshot_time)
        ]
        active_signals.sort(
            key=lambda row: (
                _sort_time(
                    row.first_seen_time
                    or row.actionable_time
                    or row.qualified_time
                    or row.last_snapshot_time
                ),
                row.id or 0,
            )
        )
        signal = active_signals[-1] if active_signals else None
        reasons = (
            ("ACTIVE_SIGNAL_PRESENT",)
            if signal is not None
            else ("NO_ACTIVE_SIGNAL_AS_OF_SNAPSHOT",)
        )

        return ActiveContext(
            equity_ref=ref,
            snapshot_time=snapshot_time,
            active_signal_id=str(signal.signal_id) if signal is not None else None,
            active_signal_side=_value(signal.side) if signal is not None else None,
            active_signal_setup=(
                (str(signal.setup or "").strip().upper() or None)
                if signal is not None else None
            ),
            active_signal_stage=_value(signal.stage) if signal is not None else None,
            active_signal_status=_value(signal.status) if signal is not None else None,
            active_signal_opportunity_key=None,
            active_signal_entry_price=None,
            active_signal_last_snapshot_time=(
                _cmp_time(signal.last_snapshot_time) if signal is not None else None
            ),
            evaluation_status="AVAILABLE",
            reason_codes=reasons,
            diagnostics={
                "lifecycle": self.lifecycle,
                "time_basis": "CAUSAL_AS_OF_SNAPSHOT",
                "cached_signal_rows": len(self._signal_cache.get(key, ())),
                "active_signal_count": len(active_signals),
            },
        )

    @staticmethod
    def _signal_active_as_of(row: Any, as_of: datetime) -> bool:
        compare_at = _cmp_time(as_of)
        start = _cmp_time(
            row.first_seen_time
            or row.actionable_time
            or row.qualified_time
            or row.last_snapshot_time
        )
        closed = _cmp_time(row.closed_time)
        if compare_at is None or start is None or start > compare_at:
            return False
        return closed is None or closed > compare_at


class NullActiveContextProvider(ActiveContextProvider):
    """Test/helper provider that performs no database reads."""

    def __init__(self) -> None:
        super().__init__("DEFAULT")

    def evaluate(self, *, equity_ref: str, symbol: str, snapshot_time: datetime) -> ActiveContext:
        return ActiveContext(
            equity_ref=str(equity_ref or symbol or "").strip().upper(),
            snapshot_time=snapshot_time,
            evaluation_status="NOT_EVALUATED",
            reason_codes=("ACTIVE_CONTEXT_PROVIDER_NOT_EVALUATED",),
        )


__all__ = ["ActiveContext", "ActiveContextProvider", "NullActiveContextProvider"]
