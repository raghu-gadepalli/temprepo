from datetime import datetime, timedelta, date

# -----------------------------
# NSE HOLIDAYS (example list)
# -----------------------------
NSE_HOLIDAYS = {
    date(2026, 1, 15),   # Pongal
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),  
    date(2026, 3, 31),  
    date(2026, 4, 3),  
    date(2026, 4, 14),  
    date(2026, 5, 1),  
    date(2026, 5, 28),  
    date(2026, 6, 26),  
    date(2026, 9, 14),  
    date(2026, 10, 2),  
    date(2026, 10, 20),  
    date(2026, 11, 10),  
    date(2026, 11, 24),  
    date(2026, 12, 25)
}

# -----------------------------
# SPECIAL TRADING DAYS
# e.g. Muhurat trading / rare Saturday sessions
# -----------------------------
SPECIAL_TRADING_DAYS = {
    date(2026, 2, 1),
}


# -----------------------------
# CHECK IF TRADING DAY
# -----------------------------
def is_trading_day(d: date):

    # Special working day override
    if d in SPECIAL_TRADING_DAYS:
        return True

    # Weekend check
    if d.weekday() >= 5:
        return False

    # Holiday check
    if d in NSE_HOLIDAYS:
        return False

    return True


# -----------------------------
# GET PREVIOUS TRADING DAY
# -----------------------------
def previous_trading_day(d: date):

    d = d - timedelta(days=1)

    while not is_trading_day(d):
        d -= timedelta(days=1)

    return d


# -----------------------------
# MAIN FUNCTION
# -----------------------------
def get_trading_day():

    now = datetime.now()

    market_open = now.replace(
        hour=9,
        minute=15,
        second=0,
        microsecond=0
    )

    today = now.date()

    # Before market open → previous day
    if now < market_open:
        today = today - timedelta(days=1)

    # Ensure valid trading day
    while not is_trading_day(today):
        today -= timedelta(days=1)

    return today

def get_previous_trading_day():
    return previous_trading_day(get_trading_day())

def get_order_variety():

    now = datetime.now()
    market_open = now.replace(hour=9,minute=15,second=0,microsecond=0)
    market_close = now.replace(hour=15,minute=30,second=0,microsecond=0)

    if market_open <= now <= market_close:
        return "regular"
    else:
        return "amo"

        