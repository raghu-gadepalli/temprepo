import logging
import os
import sys

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.user import UserSchema


def test_fetch_user():
    # Fetch a single user by userid (suitable for login or validation)
    user = UserSchema.fetch_user("admin")
    logger.info("Fetched User: %s", user)


def test_insert_user():
    # Create a new user instance.
    # Although the schema defines an id field, it will be auto-generated,
    # so a dummy value may be passed here.
    new_user = UserSchema(
        id=0,  # Dummy value; will be ignored during creation.
        userid="userid53",
        name="Test-1 User",
        email="testuser@example.com",
        mobile="1234567890",
        password="securepass",
        broker_login=1,
        broker_name="TestBroker",
        apikey="testapikey",
        secretkey="testsecretkey",
        access_token="testaccess",
        intraday_only=1,  # 0 or 1
        stocks="AAPL,GOOGL",
        equity=1,       # 0 or 1
        futures=0,      # 0 or 1
        options=0,      # 0 or 1
        autotrade=1,    # 0 or 1
        active=1,       # 0 or 1
        logged_in=0,    # 0 or 1
        logged_time=None  # Alternatively, datetime.now() can be used
    )
    # Convert the Pydantic model to a dictionary before insertion.
    inserted_user = UserSchema.create_user(new_user.model_dump())
    logger.info("Inserted User: %s", inserted_user)


def test_update_user():
    # Update the user identified by userid "userid53" with new mobile and email.
    update_data = {"mobile": "9828374", "email": "raghu@r"}
    updated_user = UserSchema.update_user("admin", update_data)
    logger.info("Updated User: %s", updated_user)


def test_delete_user():
    # Soft delete the user based on the unique userid.
    message = UserSchema.delete_user("admin")
    logger.info("Delete Message: %s", message)


if __name__ == "__main__":
    logger.info("----- Insertion Test -----")
    test_insert_user()

    # Uncomment below to run additional tests:
    logger.info("----- Fetch Test -----")
    test_fetch_user()
    
    logger.info("----- Update Test -----")
    test_update_user()
    
    logger.info("----- Delete Test -----")
    test_delete_user()
