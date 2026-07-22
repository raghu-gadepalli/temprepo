# logconfig.py
import os
import sys
import logging
import logging.config
from config import LoggingConfig

STANDARD_FORMAT = "%(asctime)s %(levelname)s [%(name)s:%(lineno)d] %(message)s"

def setup_logging(log_file: str = None, log_level: str = None):
    # 1) Determine numeric level
    level_name = (log_level or os.getenv("LOG_LEVEL", LoggingConfig.LOG_LEVEL)).upper()
    LOG_LEVEL = getattr(logging, level_name, logging.INFO)

    # 2) Use full path for logfile
    LOG_FILE = log_file or os.getenv("LOG_FILE", LoggingConfig.LOG_FILE)

    # 3) Ensure log directory exists
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir and not os.path.isdir(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception as e:
            print(f"Warning: could not create log directory '{log_dir}': {e}", file=sys.stderr)

    # 4) Build handlers
    handlers = {
        "file": {
            "class":       "logging.handlers.RotatingFileHandler",
            "level":       LOG_LEVEL,
            "formatter":   "standard",
            "filename":    LOG_FILE,
            "mode":        "a",
            "maxBytes":    10 * 1024 * 1024,
            "backupCount": 5,
        }
    }
    if LoggingConfig.LOG_TO_CONSOLE:
        handlers["console"] = {
            "class":     "logging.StreamHandler",
            "level":     LOG_LEVEL,
            "formatter": "standard",
            "stream":    "ext://sys.stderr",
        }

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {"format": STANDARD_FORMAT},
        },
        "handlers": handlers,
        "root": {
            "level":    LOG_LEVEL,
            "handlers": list(handlers.keys()),
        },
        "loggers": {
            # suppress overly chatty modules
            "services.zerodha.kiteconnect_service": {
                "level":     "WARNING",
                "handlers":  [],
                "propagate": True,
            },
        },
    }

    # 5) Apply config, but dont let failures kill the app
    try:
        logging.config.dictConfig(logging_config)
    except Exception as e:
        print(f"Warning: logging setup failed: {e}", file=sys.stderr)
        logging.basicConfig(level=LOG_LEVEL)
