from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

from configs.evidence_config import EVIDENCE_CONFIG, EvidenceContributionConfig
from services.evidence.evidence_result import EvidenceContribution, EvidenceDataError


BUY = "BUY"
SELL = "SELL"
SIDES = (BUY, SELL)


def upper(value: Any) -> str:
    return str(value).strip().upper()


def as_dict(snapshot: Any) -> Dict[str, Any]:
    if isinstance(snapshot, dict):
        return snapshot
    if hasattr(snapshot, "model_dump"):
        return snapshot.model_dump(mode="python")
    raise EvidenceDataError(f"Snapshot is not a dict or pydantic model: {type(snapshot).__name__}")


def require_path(d: Dict[str, Any], path: str) -> Any:
    cur: Any = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise EvidenceDataError(f"Snapshot missing required path: {path}")
        cur = cur[key]
    return cur


def require_numeric(d: Dict[str, Any], path: str) -> float:
    value = require_path(d, path)
    if value is None:
        raise EvidenceDataError(f"Snapshot required numeric path is null: {path}")
    try:
        return float(value)
    except Exception as exc:
        raise EvidenceDataError(f"Snapshot required numeric path is not numeric: {path}={value!r}") from exc


def maybe_numeric(d: Dict[str, Any], path: str) -> float | None:
    value = require_path(d, path)
    if value is None:
        return None
    try:
        return float(value)
    except Exception as exc:
        raise EvidenceDataError(f"Snapshot path is not numeric: {path}={value!r}") from exc


def _normalize_flat_market_window_positions(d: Dict[str, Any]) -> None:
    """Backfill neutral close_position_in_range for flat market windows.

    Snapshot generation now writes 0.50 when a candle/window has high == low.
    This defensive normalizer keeps replay/backtest stable for already-stored
    snapshots that still contain null in market_windows.*.close_position_in_range.
    """
    windows = d.get("market_windows")
    if not isinstance(windows, dict):
        return
    for window in windows.values():
        if not isinstance(window, dict):
            continue
        if window.get("close_position_in_range") is not None:
            continue
        try:
            close = float(window.get("close"))
            low = float(window.get("low"))
            high = float(window.get("high"))
        except Exception:
            continue
        if high == low and close == high:
            window["close_position_in_range"] = 0.5


def validate_snapshot_contract(d: Dict[str, Any]) -> None:
    _normalize_flat_market_window_positions(d)

    cfg = EVIDENCE_CONFIG.data_quality
    for block in cfg.required_top_level_blocks:
        value = require_path(d, block)
        if not isinstance(value, dict):
            raise EvidenceDataError(f"Snapshot block must be a dict: {block}")

    market_windows = require_path(d, "market_windows")
    for window_name in cfg.required_market_windows:
        if window_name not in market_windows:
            raise EvidenceDataError(f"Snapshot missing market window: {window_name}")

    indicator_windows = require_path(d, "indicator_windows")
    for group_name in cfg.required_indicator_groups:
        if group_name not in indicator_windows:
            raise EvidenceDataError(f"Snapshot missing indicator window group: {group_name}")
        group_value = indicator_windows[group_name]
        if not isinstance(group_value, dict):
            raise EvidenceDataError(f"Snapshot indicator window group must be a dict: {group_name}")
        for window_name in cfg.required_indicator_windows:
            if window_name not in group_value:
                raise EvidenceDataError(
                    f"Snapshot missing indicator window: indicator_windows.{group_name}.{window_name}"
                )
            if not isinstance(group_value[window_name], dict):
                raise EvidenceDataError(
                    f"Snapshot indicator window must be a dict: indicator_windows.{group_name}.{window_name}"
                )

    for path in cfg.required_numeric_paths:
        require_numeric(d, path)


def contribution(
    *,
    key: str,
    label: str,
    side: str,
    score: float,
    weight: float,
    message: str,
    data: Dict[str, Any] | None = None,
) -> EvidenceContribution:
    return EvidenceContribution(
        key=key,
        label=label,
        side=upper(side),
        score=max(0.0, min(100.0, float(score))),
        weight=float(weight),
        message=message,
        data=data or {},
    )


def pair_contribution(
    *,
    key: str,
    label: str,
    confirm_side: str | None,
    cfg: EvidenceContributionConfig,
    neutral_message: str,
    confirm_message: str,
    oppose_message: str,
    data: Dict[str, Any] | None = None,
) -> Dict[str, EvidenceContribution]:
    confirm = upper(confirm_side) if confirm_side else "NONE"
    out: Dict[str, EvidenceContribution] = {}
    for side in SIDES:
        if confirm == side:
            score = cfg.confirm_score
            msg = confirm_message
        elif confirm in SIDES:
            score = cfg.oppose_score
            msg = oppose_message
        else:
            score = cfg.neutral_score
            msg = neutral_message
        out[side] = contribution(
            key=key,
            label=label,
            side=side,
            score=score,
            weight=cfg.weight,
            message=msg,
            data=data,
        )
    return out


def weighted_score(items: Sequence[EvidenceContribution]) -> float:
    if not items:
        raise EvidenceDataError("Cannot compute weighted score with zero evidence contributions")
    total_weight = sum(abs(x.weight) for x in items)
    if total_weight <= 0:
        raise EvidenceDataError("Cannot compute weighted score with non-positive total weight")
    raw = sum(x.score * abs(x.weight) for x in items) / total_weight
    return round(max(0.0, min(100.0, raw)), 2)


def side_from_signed_value(value: float, *, min_abs: float) -> str:
    if value >= min_abs:
        return BUY
    if value <= -min_abs:
        return SELL
    return "NONE"


def opposite_side(side: str) -> str:
    side_s = upper(side)
    if side_s == BUY:
        return SELL
    if side_s == SELL:
        return BUY
    return "NONE"


def average(values: Iterable[float]) -> float:
    vals = [float(v) for v in values]
    if not vals:
        raise EvidenceDataError("Cannot compute average of empty values")
    return sum(vals) / len(vals)
