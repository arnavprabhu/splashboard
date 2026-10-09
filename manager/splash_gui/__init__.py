"""Splashboard manager."""

__version__ = "0.2.0"

# `GET /health` carries this so the CLI and the menu bar app can tell our manager
# from another server on the same port (oMLX and others also answer /health).
SERVICE = "splash-gui-manager"
