"""Backward-compatibility facade for TimeManager."""

from piso_wifi.services.time_service import TimeService, TimeManager

__all__ = ["TimeManager", "TimeService"]