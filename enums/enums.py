# enums/enums.py

from enum import Enum


class BaseEnum(Enum):
    @classmethod
    def from_string(cls, value):
        if isinstance(value, cls):
            return value

        norm = str(value).strip().upper().replace(" ", "_").replace("-", "_")
        for member in cls:
            member_value_norm = (
                member.value.upper().replace(" ", "_").replace("-", "_")
                if isinstance(member.value, str)
                else member.value
            )
            if member.name == norm or member_value_norm == norm:
                return member

        raise ValueError(f"Unknown value for {cls.__name__}: {value!r}")

    def to_string(self) -> str:
        return self.name.upper()

    def __str__(self):
        return self.to_string()

    @classmethod
    def choices(cls):
        return [m.to_string() for m in cls]


# ============================================================
# Order enums
# ============================================================

class OrderVariety(BaseEnum):
    REGULAR = "regular"
    AMO = "amo"
    ICEBERG = "iceberg"


class OrderType(BaseEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SLM = "SL-M"


class ProductType(BaseEnum):
    CNC = "CNC"
    MIS = "MIS"
    NRML = "NRML"


class OrderStatus(BaseEnum):
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    OPEN = "OPEN"
    INVALID = "INVALID"
    TRIGGER_PENDING = "TRIGGER_PENDING"


# ============================================================
# Symbol enums
# ============================================================

class SymbolType(BaseEnum):
    EQ = "EQ"
    CE = "CE"
    PE = "PE"
    FUT = "FUT"


class ExchangeType(BaseEnum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    DEFAULT = "DEFAULT"


# ============================================================
# Market direction enums
# ============================================================



class SignalSide(BaseEnum):
    BUY = "BUY"
    SELL = "SELL"


class LifecycleSide(BaseEnum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


class TradeType(BaseEnum):
    BUY = "BUY"
    SELL = "SELL"


class TrendType(BaseEnum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TREND = "NO_TREND"

class StructureSide(BaseEnum):
    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"
    NONE = "NONE"


class StructureState(BaseEnum):
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"

    RANGE_ACCEPTED = "RANGE_ACCEPTED"
    COMPRESSION = "COMPRESSION"

    BREAKOUT_ATTEMPT = "BREAKOUT_ATTEMPT"
    BREAKOUT_TESTING = "BREAKOUT_TESTING"
    ACCEPTED_BREAKOUT = "ACCEPTED_BREAKOUT"

    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    RANGE_REABSORBED = "RANGE_REABSORBED"
    PULLBACK_HOLD = "PULLBACK_HOLD"

class BreakoutStatus(BaseEnum):
    NONE = "NONE"
    BREAKOUT_ATTEMPT = "BREAKOUT_ATTEMPT"
    BREAKOUT_TESTING = "BREAKOUT_TESTING"
    ACCEPTED_BREAKOUT = "ACCEPTED_BREAKOUT"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    RANGE_REABSORBED = "RANGE_REABSORBED"
    PULLBACK_HOLD = "PULLBACK_HOLD"


class StructureRangeSource(BaseEnum):
    UNKNOWN = "UNKNOWN"
    PDH_PDL = "PDH_PDL"
    INTRADAY = "INTRADAY"
    ORB = "ORB"
    RECENT15 = "RECENT15"
    SWING = "SWING"


class SessionPhase(BaseEnum):
    UNKNOWN = "UNKNOWN"
    OPENING = "OPENING"
    ACTIVE = "ACTIVE"
    LATE = "LATE"

# ============================================================
# Lifecycle enums
# ============================================================

class LifecycleStage(BaseEnum):
    """
    Generic operational lifecycle interpretation.

    These names are intentionally setup-neutral. The reason a
    signal exists belongs in setup_type/setup_state, not in the
    operational lifecycle stage.

    DISCOVERY  : Early state formation / first evidence.
    BUILDING   : Setup improving but not fully actionable.
    ACTIVE     : Deployable setup; trade generation may create/continue trade.
    EXPAND     : Strong deployable setup; adaptive management may expand.
    PROTECT    : Setup is still alive but should be protected/tightened.
    TRANSITION : Mixed/conflicting market state; pause fresh entry.
    WEAKENING  : Setup quality is deteriorating.
    EXIT_BIAS  : Exit/protection bias is building.
    FORCE_EXIT : Hard invalidation / exit immediately.

    Deprecated continuation names are kept only to read old persisted rows.
    New code must not emit them.
    """
    DISCOVERY = "DISCOVERY"
    BUILDING = "BUILDING"
    ACTIVE = "ACTIVE"
    EXPAND = "EXPAND"
    PROTECT = "PROTECT"
    TRANSITION = "TRANSITION"
    WEAKENING = "WEAKENING"
    EXIT_BIAS = "EXIT_BIAS"
    FORCE_EXIT = "FORCE_EXIT"

    # Deprecated aliases for historical persisted data only.
    EARLY_CONTINUATION = "EARLY_CONTINUATION"
    MATURE_CONTINUATION = "MATURE_CONTINUATION"
    PULLBACK_CONTINUATION = "PULLBACK_CONTINUATION"


class SetupType(BaseEnum):
    REVERSAL = "REVERSAL"
    EXPANSION = "EXPANSION"
    BREAKOUT = "BREAKOUT"
    BALANCE = "BALANCE"
    AUCTION_CONTINUATION = "AUCTION_CONTINUATION"
    NONE = "NONE"


class SetupState(BaseEnum):
    NONE = "NONE"
    REVERSAL_WATCH = "REVERSAL_WATCH"
    REVERSAL_CONFIRMED = "REVERSAL_CONFIRMED"
    REVERSAL_FAILED = "REVERSAL_FAILED"
    EXPANSION_WATCH = "EXPANSION_WATCH"
    EXPANSION_INITIATING = "EXPANSION_INITIATING"
    EXPANSION_CONFIRMED = "EXPANSION_CONFIRMED"
    BREAKOUT_WATCH = "BREAKOUT_WATCH"
    BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
    BALANCE_FORMING = "BALANCE_FORMING"
    BALANCE_ACCEPTED = "BALANCE_ACCEPTED"


class LifecycleQuality(BaseEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ============================================================
# Signal / trade action enums
# ============================================================






class SignalAction(BaseEnum):
    """Lifecycle action requested by the signal engine."""
    WATCH = "WATCH"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    HOLD = "HOLD"
    PROMOTE = "PROMOTE"
    DOWNGRADE = "DOWNGRADE"
    REVIEW_OPPOSITE = "REVIEW_OPPOSITE"
    INVALIDATE = "INVALIDATE"
    INVALIDATE_OPPOSITE = "INVALIDATE_OPPOSITE"

# ============================================================
# Signal persistence enums
# ============================================================

class SignalStage(BaseEnum):
    """
    Deprecated for lifecycle interpretation.

    Kept only if existing DB/routes still reference signal.stage.
    New code should prefer LifecycleStage.
    """
    NONE = "NONE"
    TRACKING = "TRACKING"
    QUALIFIED = "QUALIFIED"
    ACTIONABLE = "ACTIONABLE"


class SignalStatus(BaseEnum):
    """
    Persistence status of a signal record.

    OPEN        : Currently active.
    CLOSED      : Closed normally.
    INVALIDATED : Setup broken.
    EXPIRED     : Timed out / stale.
    REPLACED    : Closed because opposite-side setup replaced it.
    BLOCKED     : Blocked by policy/risk constraints.
    CANCELLED   : Manually/system cancelled.
    """
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    REPLACED = "REPLACED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


# ============================================================
# Signal/lifecycle state / UI view enums
# ============================================================



class EntryView(BaseEnum):
    ENTER = "ENTER"
    WATCH = "WATCH"
    LATE = "LATE"
    AVOID = "AVOID"


class ContinuationView(BaseEnum):
    STRONG = "STRONG"
    HOLD_OK = "HOLD_OK"
    WEAKENING = "WEAKENING"
    LOST = "LOST"
    NA = "NA"




# ============================================================
# Trades – ENTRY lifecycle persistence
# ============================================================

class EntryStatus(BaseEnum):
    CREATED = "CREATED"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    INVALID = "INVALID"


# ============================================================
# Trades – EXIT lifecycle persistence
# ============================================================

class ExitStatus(BaseEnum):
    NONE = "NONE"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# ============================================================
# Trades – monitor posture for adaptive management
# ============================================================

class TradePosture(BaseEnum):
    EXPAND = "EXPAND"
    HOLD = "HOLD"
    PROTECT = "PROTECT"
    EXIT = "EXIT"


# ============================================================
# Trade configuration helpers
# ============================================================

class PositionStyle(BaseEnum):
    NAKED = "NAKED"
    HEDGED = "HEDGED"


class StopLossMode(BaseEnum):
    NONE = "NONE"
    PCT = "PCT"
    ABS = "ABS"


class TargetMode(BaseEnum):
    NONE = "NONE"
    PCT = "PCT"
    ABS = "ABS"


class StopLossType(BaseEnum):
    PERCENT = "PERCENT"
    HMA = "HMA"
    ATR = "ATR"


class TrailingStoplossType(BaseEnum):
    STEP = "STEP"
    COST = "COST"
    HMA = "HMA"
    ATR = "ATR"


# ============================================================
# Indicator enums
# ============================================================

class IndicatorType(BaseEnum):
    HMA = "HMA"


# ============================================================
# Legacy trade status
# ============================================================

class TradeStatus(BaseEnum):
    """
    Legacy v3 trade status.

    Prefer EntryStatus and ExitStatus for vNext.
    """
    OPEN = "OPEN"
    ENTRY_PLACED = "ENTRY_PLACED"
    ENTRY_FILLED = "ENTRY_FILLED"
    SL_INITIATED = "SL_INITIATED"
    SL_PLACED = "SL_PLACED"
    SL_FILLED = "SL_FILLED"
    SL_EXECUTED = "SL_EXECUTED"
    COMPLETE = "COMPLETE"
    CANCELLED_RETRY_LIMIT = "CANCELLED_RETRY_LIMIT"
    CANCELLED = "CANCELLED"
    INVALID = "INVALID"
    REJECTED = "REJECTED"