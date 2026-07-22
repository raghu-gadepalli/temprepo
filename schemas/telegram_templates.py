from __future__ import annotations
from pydantic import BaseModel
from typing import Literal, Dict


class TelegramTemplate(BaseModel):
    """
    Describes one Telegram message template.
    """
    key: str
    # a Python format string; context must supply matching keys
    template: str
    # not used yet, but reserved for future Markdown/HTML
    parse_mode: Literal["", "Markdown", "HTML"] = ""


# Updated templates: removed buy_weight/sell_weight (no longer in SnapshotSchema)
# Added state, strength, hma_slope_conviction, intraday_intensity_strength
TEMPLATES: Dict[str, TelegramTemplate] = {
    "signal.entry": TelegramTemplate(
        key="signal.entry",
        template=(
            "{signal_type} signal on {symbol} at {time} @ {price:.2f}\n"
            "Lifecycle: {lifecycle} | State: {state} | Strength: {strength}\n"
            "Changes: {changed_freqs}"
        )
    ),
    "signal.exit": TelegramTemplate(
        key="signal.exit",
        template=(
            "{signal_type} signal on {symbol} at {time} @ {price:.2f}\n"
            "Lifecycle: {lifecycle} | State: {state} | Strength: {strength}\n"
            "Changes: {changed_freqs}\n"
            "P&L: {pnl:+.2f}"
        )
    ),
}
