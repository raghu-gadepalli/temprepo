import sys
import os

# Ensure autotrades directory is at the front of sys.path
sys.path.insert(0, '/var/www/autotrades')

# Add the virtual environment's site-packages to the sys.path
venv_site_packages = '/var/www/autotrades/venv/lib/python3.10/site-packages'  # Adjust for your Python version
sys.path.insert(0, venv_site_packages)

# Import the Flask application instance directly.
from app import app as application
