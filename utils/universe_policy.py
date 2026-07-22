from __future__ import annotations

from typing import Iterable, Set

from configs.scanner_config import SCANNER_CONFIG


def _norm(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _norm_set(items: Iterable[str]) -> Set[str]:
    return {_norm(x) for x in (items or []) if _norm(x)}


def universe_blacklist() -> Set[str]:
    """Symbols structurally excluded from the tradable universe."""
    return _norm_set(SCANNER_CONFIG.universe.blacklist)


def universe_whitelist() -> Set[str]:
    """Symbols protected from structural monthly/expiry filtering."""
    return _norm_set(SCANNER_CONFIG.universe.whitelist)


def is_blacklisted(symbol: str) -> bool:
    return _norm(symbol) in universe_blacklist()


def is_whitelisted(symbol: str) -> bool:
    return _norm(symbol) in universe_whitelist()
