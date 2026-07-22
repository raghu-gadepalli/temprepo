from enum import Enum
import functools
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, Tuple

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException, TokenException, InputException
from tenacity import retry

from config import RetryConfig
from enums.enums import (
    OrderStatus,
    OrderType,
    OrderVariety,
)
from utils.datetime_utils import IST

logger = logging.getLogger(__name__)

# Broker-side protection for MARKET orders only.
# Applies directionally at broker side based on BUY/SELL.
DEFAULT_MARKET_PROTECTION = 2


RETRY_POLICIES = {
    "HISTORICAL": RetryConfig.HISTORICAL,
    "ORDER": RetryConfig.ORDER,
    "BASIC": RetryConfig.BASIC,
}


def retry_with(cfg_name: str):
    cfg = RETRY_POLICIES[cfg_name]
    return retry(stop=cfg["stop"], wait=cfg["wait"], reraise=True)


def handle_kite_errors(default):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (TokenException, InputException):
                raise
            except KiteException as e:
                code = getattr(e, "code", None)
                msg = getattr(e, "message", str(e))
                if code == 403:
                    raise TokenException(msg, code)
                if code in (400, 404):
                    raise InputException(msg, code)
                logger.error("KiteException in %s: %s", fn.__name__, e, exc_info=True)
                return default
            except Exception as e:
                logger.error("Unexpected error in %s: %s", fn.__name__, e, exc_info=True)
                return default
        return wrapper
    return decorator


class KiteConnectService:
    def __init__(self, api_key: str, access_token: str):
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

    # ---------------------------------------------------------------------
    # Time helpers
    # ---------------------------------------------------------------------
    @staticmethod
    def _now_ist_naive() -> datetime:
        """Return naive IST wall-clock time."""
        return datetime.now(IST).replace(tzinfo=None)

    @staticmethod
    def _normalize(val: Union[str, Enum]) -> str:
        return val.value if isinstance(val, Enum) else val

    @staticmethod
    def extract_order_id(resp: Any) -> str:
        if isinstance(resp, dict) and "order_id" in resp:
            return resp["order_id"]
        return str(resp)

    @handle_kite_errors(default=[])
    @retry_with("HISTORICAL")
    def fetch_historical_data(
        self,
        instrument_token: Any,
        from_date: datetime,
        to_date: datetime,
        interval: str = "5minute",
        oi: bool = False,
    ) -> List[dict]:
        return self.kite.historical_data(
            instrument_token=int(instrument_token),
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            oi=oi,
        )

    # ------------------------------
    # LTP helpers
    # ------------------------------
    @handle_kite_errors(default=None)
    @retry_with("BASIC")
    def fetch_latest_price(self, instrument_key: Union[str, List[str]]) -> Optional[float]:
        data = self.kite.ltp(instrument_key)
        if isinstance(instrument_key, list):
            return None
        if not data or instrument_key not in data:
            return None
        return data[instrument_key].get("last_price")

    @handle_kite_errors(default={})
    @retry_with("BASIC")
    def fetch_latest_prices(self, instrument_keys: List[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        data = self.kite.ltp(instrument_keys)
        if not isinstance(data, dict):
            return out
        for k in instrument_keys:
            try:
                v = data.get(k) or {}
                lp = v.get("last_price")
                if lp is not None:
                    out[k] = float(lp)
            except Exception:
                continue
        return out

    # ------------------------------
    # Quote helpers
    # ------------------------------
    @handle_kite_errors(default={})
    @retry_with("BASIC")
    def fetch_quote(self, quote_keys: List[str]) -> Dict[str, Any]:
        return self.kite.quote(quote_keys)

    @staticmethod
    def best_bid_ask_ltp_from_quote_record(rec: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        if not isinstance(rec, dict):
            return None, None, None

        ltp = rec.get("last_price")

        depth = rec.get("depth")
        if not isinstance(depth, dict):
            return None, None, float(ltp) if ltp is not None else None

        best_bid = None
        best_ask = None

        buy = depth.get("buy")
        if isinstance(buy, list) and buy:
            try:
                best_bid = buy[0].get("price")
            except Exception:
                best_bid = None

        sell = depth.get("sell")
        if isinstance(sell, list) and sell:
            try:
                best_ask = sell[0].get("price")
            except Exception:
                best_ask = None

        return (
            float(best_bid) if best_bid is not None else None,
            float(best_ask) if best_ask is not None else None,
            float(ltp) if ltp is not None else None,
        )

    # ------------------------------
    # Orders
    # ------------------------------
    @handle_kite_errors(default=None)
    @retry_with("ORDER")
    def place_order(
        self,
        exchange: str,
        tradingsymbol: str,
        transaction_type: Union[str, Enum],
        quantity: int,
        order_type: Union[str, Enum],
        product: Union[str, Enum],
        variety: Union[str, Enum],
        price: float = None,
        trigger_price: float = None,
    ) -> dict:
        transaction_type = self._normalize(transaction_type)
        order_type = self._normalize(order_type)
        product = self._normalize(product)
        variety = self._normalize(variety)

        kwargs = {
            "variety": variety,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": order_type,
            "price": price,
            "trigger_price": trigger_price,
            "product": product,
        }

        # Apply broker-side market protection only for MARKET orders.
        if str(order_type).upper() == OrderType.MARKET.value:
            kwargs["market_protection"] = DEFAULT_MARKET_PROTECTION

        order_id = self.kite.place_order(**kwargs)

        return {
            "order_id": order_id,
            "timestamp": self._now_ist_naive().isoformat(),
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
            "variety": variety,
            "price": price,
            "trigger_price": trigger_price,
            "market_protection": kwargs.get("market_protection"),
        }

    @handle_kite_errors(default=None)
    @retry_with("BASIC")
    def fetch_latest_status_of_order(self, order_id: str) -> Optional[OrderStatus]:
        history = self.kite.order_history(order_id) or []
        if not history:
            return None
        last = history[-1] if isinstance(history[-1], dict) else None
        if not isinstance(last, dict):
            return None
        st = last.get("status")
        if not st:
            return None
        return OrderStatus.from_string(st)

    @handle_kite_errors(default=[])
    @retry_with("BASIC")
    def fetch_order_history(self, order_id: str) -> List[dict]:
        return self.kite.order_history(order_id)

    @handle_kite_errors(default=None)
    @retry_with("ORDER")
    def cancel_order(self, order_id: str, variety: Union[str, OrderVariety]) -> Optional[str]:
        return self.kite.cancel_order(
            order_id=order_id,
            variety=self._normalize(variety),
        )

    @handle_kite_errors(default=None)
    @retry_with("ORDER")
    def modify_order_price(self, order_id: str, price: float, variety: Union[str, OrderVariety]) -> dict:
        var = self._normalize(variety)
        self.kite.modify_order(order_id=order_id, price=price, variety=var)
        return {
            "order_id": order_id,
            "price": price,
            "variety": var,
            "timestamp": self._now_ist_naive().isoformat(),
        }

    @handle_kite_errors(default=None)
    @retry_with("ORDER")
    def modify_order_trigger_price(self, order_id: str, trigger_price: float, variety: Union[str, OrderVariety]) -> dict:
        var = self._normalize(variety)
        self.kite.modify_order(order_id=order_id, trigger_price=trigger_price, variety=var)
        return {
            "order_id": order_id,
            "trigger_price": trigger_price,
            "variety": var,
            "timestamp": self._now_ist_naive().isoformat(),
        }

    @handle_kite_errors(default=None)
    @retry_with("ORDER")
    def modify_order_price_and_trigger_price(
        self,
        order_id: str,
        price: float,
        trigger_price: float,
        variety: Union[str, OrderVariety],
    ) -> dict:
        var = self._normalize(variety)
        self.kite.modify_order(
            order_id=order_id,
            price=price,
            trigger_price=trigger_price,
            variety=var,
        )
        return {
            "order_id": order_id,
            "price": price,
            "trigger_price": trigger_price,
            "variety": var,
            "timestamp": self._now_ist_naive().isoformat(),
        }

    @handle_kite_errors(default=None)
    @retry_with("ORDER")
    def modify_order_type(self, order_id: str, variety: Union[str, OrderVariety], order_type: Union[str, OrderType]) -> dict:
        var = self._normalize(variety)
        typ = self._normalize(order_type)
        self.kite.modify_order(order_id=order_id, variety=var, order_type=typ)
        return {
            "order_id": order_id,
            "order_type": typ,
            "variety": var,
            "timestamp": self._now_ist_naive().isoformat(),
        }

    # ------------------------------
    # Positions
    # ------------------------------
    @handle_kite_errors(default={})
    @retry_with("BASIC")
    def fetch_positions(self) -> Dict[str, Any]:
        """Wrapper around KiteConnect.positions(). Returns {'net': [...], 'day': [...]}."""
        return self.kite.positions()