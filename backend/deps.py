"""
Centralized dependency exports.

Use this module to import dependencies instead of importing from main.py
to avoid circular imports.

Example:
    from deps import get_current_user, get_db, settings
"""

from config import settings
from db import get_db
from auth import get_current_user, require_hr, require_admin

__all__ = [
    "settings",
    "get_db",
    "get_current_user",
    "require_hr",
    "require_admin",
]