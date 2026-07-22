from sqlalchemy import text
from database.database import get_trades_db


def get_fo_underlyings():

    with get_trades_db() as db:

        result = db.execute(text("""
            SELECT DISTINCT name
            FROM symbols
            WHERE segment = 'NFO'
            ORDER BY name
        """))

        return [r[0] for r in result]


def get_fo_expiry(name):

    with get_trades_db() as db:

        result = db.execute(text("""
            SELECT DISTINCT expiry
            FROM symbols
            WHERE name = :name
            ORDER BY expiry
        """), {"name": name})

        return [r[0] for r in result]


def get_fo_strikes(name, expiry, option_type):

    with get_trades_db() as db:

        result = db.execute(text("""
            SELECT strike
            FROM symbols
            WHERE name = :name
            AND expiry = :expiry
            AND instrument_type = :type
            ORDER BY strike
        """), {
            "name": name,
            "expiry": expiry,
            "type": option_type
        })

        return [r[0] for r in result]


def get_symbol(name, expiry, strike, option_type):

    with get_trades_db() as db:

        result = db.execute(text("""
            SELECT tradingsymbol, lot_size
            FROM symbols
            WHERE name=:name
            AND expiry=:expiry
            AND strike=:strike
            AND instrument_type=:type
        """), {
            "name": name,
            "expiry": expiry,
            "strike": strike,
            "type": option_type
        }).fetchone()

        return result
        