"""Backward-compatibility facade for UserManager."""

from piso_wifi.services.user_service import UserService, UserManager

__all__ = ["UserManager", "UserService"]