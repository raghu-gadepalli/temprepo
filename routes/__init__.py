# routes/__init__.py

from .routes import main_bp
from .admin import admin_bp
from .userbp import user_bp
from .zero import zero_bp
from .dash import dash_bp
from .oms import oms_bp

__all__ = [
    "main_bp",
    "admin_bp",
    "user_bp",
    "zero_bp",
    "dash_bp",
    "oms_bp",
]