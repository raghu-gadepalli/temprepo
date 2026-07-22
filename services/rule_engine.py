# services/rule_engine.py

import logging
from types import SimpleNamespace
from typing import Any, Dict

from asteval import Interpreter

logger = logging.getLogger(__name__)


class ParserError(Exception):
    """Raised when a rule cannot be parsed or evaluated."""
    pass


def _to_namespace(value: Any) -> Any:
    """
    Recursively convert nested dictionaries into SimpleNamespace so rules can use
    attribute access like `hma.state` instead of dict indexing.
    """
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_namespace(v) for v in value]
    return value


def evaluate_rule(rule: str, ctx: Dict[str, Any]) -> bool:
    """
    Safely evaluate a boolean expression `rule` against context `ctx`.

    Notes:
    - A fresh interpreter is used per call to avoid shared mutable state.
    - Nested dictionaries are converted to namespaces so expressions like
      `hma.state == 'BUY'` work naturally.
    """
    if not isinstance(rule, str) or not rule.strip():
        raise ParserError("Rule must be a non-empty string")

    try:
        aeval = Interpreter()
        aeval.error = []

        safe_ctx = {k: _to_namespace(v) for k, v in ctx.items()}
        aeval.symtable.update(safe_ctx)

        result = aeval.eval(rule)

        if aeval.error:
            err = aeval.error[0]
            raise ParserError(err.get_error())

    except ParserError:
        logger.exception("Rule evaluation failed: %s", rule)
        raise
    except Exception as e:
        logger.exception("Rule evaluation failed: %s", rule)
        raise ParserError(str(e)) from e

    if not isinstance(result, (bool, int, float)):
        raise ParserError(f"Expression did not return a boolean-like result: {rule!r}")

    return bool(result)