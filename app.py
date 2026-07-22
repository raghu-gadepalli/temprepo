# app.py
import logging

from flask import Flask, session
from flask_sqlalchemy import SQLAlchemy

from config import AppConfig, LoggingConfig
from logconfig import setup_logging
from utils.access import is_operator, is_admin

# --- Logging Setup ---
LOG_LEVEL = logging._nameToLevel[LoggingConfig.LOG_LEVEL]
setup_logging(log_file=LoggingConfig.LOG_FILE, log_level=LoggingConfig.LOG_LEVEL)
logging.getLogger("werkzeug").setLevel(LOG_LEVEL)

logger = logging.getLogger(__name__)
logger.info("Logging initialized successfully in app.py")

# --- Flask App Initialization ---
app = Flask(__name__)
app.config.from_object(AppConfig)

# --- SQLAlchemy Initialization ---
db = SQLAlchemy()
db.init_app(app)

logger.info(
    "Flask and SQLAlchemy initialized with config: %s",
    AppConfig.SQLALCHEMY_DATABASE_URI
)

# --- Blueprint Registration ---
from routes import main_bp, dash_bp, user_bp, zero_bp, admin_bp, oms_bp

app.register_blueprint(main_bp)
app.register_blueprint(dash_bp)
app.register_blueprint(user_bp)
app.register_blueprint(zero_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(oms_bp)

logger.info("Blueprints registered successfully")


# --- Template Globals / Context ---
@app.context_processor
def inject_access_context():
    return {
        "is_operator": lambda: is_operator(),
        "is_admin": lambda: is_admin(),
    }

from utils.filters import format_inr

# Register Jinja filter
app.jinja_env.filters["inr"] = format_inr

# --- Entry Point for Development ---
if __name__ == "__main__":
    app.run(debug=True)