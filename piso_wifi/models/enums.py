"""Enumerations representing domain constants and states."""

from enum import Enum


class PlanType(str, Enum):
    """Bandwidth and service tier plans."""
    DEFAULT = "default"
    PREMIUM = "premium"

    @classmethod
    def from_str(cls, value: str) -> "PlanType":
        """Parse string to PlanType safely."""
        try:
            return cls(value.lower().strip())
        except (ValueError, AttributeError):
            return cls.DEFAULT


class UserStatus(str, Enum):
    """Connection/authorization status of a user."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"
    PAUSED = "paused"

    @classmethod
    def from_str(cls, value: str) -> "UserStatus":
        """Parse string to UserStatus safely."""
        try:
            return cls(value.lower().strip())
        except (ValueError, AttributeError):
            return cls.INACTIVE


class DeductionType(str, Enum):
    """Origin of a time deduction."""
    AUTO = "auto"
    MANUAL = "manual"


class DeviceSignalQuality(str, Enum):
    """Signal strength classification."""
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    UNKNOWN = "unknown"

    @classmethod
    def classify(cls, dbm_val: float) -> "DeviceSignalQuality":
        if dbm_val >= -50:
            return cls.EXCELLENT
        elif dbm_val >= -65:
            return cls.GOOD
        elif dbm_val >= -80:
            return cls.FAIR
        else:
            return cls.POOR
