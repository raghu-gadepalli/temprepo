from kiteconnect import KiteConnect
from sqlalchemy import text
from database.database import get_trades_db
from config import AppConfig


def get_kite_client_by_id(userid):

    with get_trades_db() as db:

        result = db.execute(text("""
            SELECT apikey, access_token
            FROM users
            WHERE userid = :userid
        """), {"userid": userid}).fetchone()

    #print ( "Client for : ", result );

    api_key = result[0]
    access_token = result[1]

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    #print ( kite.orders () );
    
    return kite


def kite_get_ltp(exchange, symbol):

    ## Get the Client 
    kite = get_kite_client_by_id(AppConfig.DATA_USER)

    if not kite:
        ## Client not active or logged in
        return {
            "ltp": None,
            "error": "DATA_USER_NOT_LOGGED_IN"
        }

    try:
        instrument = f"{exchange}:{symbol}"
        quote = kite.ltp([instrument])
        #ltp = list(quote.values())[0]["last_price"]
        print ( "Quote ; ", quote )
        if ( instrument in quote ):
            return {
                "ltp": quote[instrument]["last_price"]
            }
        else: 
            return {
                "ltp": None,
                "error": "LTP_FETCH_FAILED"
            }        
    except Exception as e:
        print ( "LTP fetch failed: ", e )
        return {
            "ltp": None,
            "error": "LTP_FETCH_FAILED"
        }
