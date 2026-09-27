"""Domain entities and data structures for Piso-WiFi."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from piso_wifi.models.enums import PlanType, UserStatus, DeductionType


@dataclass
class BandwidthLimit:
    """Bandwidth rate limit for a client."""
    download_kbps: int
    upload_kbps: int

    def validate(self, min_kbps: int = 32, max_kbps: int = 100000) -> bool:
        """Check if limits are within valid system ranges."""
        return (
            min_kbps <= self.download_kbps <= max_kbps
            and min_kbps <= self.upload_kbps <= max_kbps
        )


@dataclass
class User:
    """Represents a registered WiFi user/device."""
    id: Optional[int] = None
    mac_address: str = ""
    time_balance: float = 0.0
    status: UserStatus = UserStatus.INACTIVE
    created_at: Optional[datetime] = None
    last_deduction: Optional[datetime] = None
    download_limit: int = 2048
    upload_limit: int = 1024
    plan: PlanType = PlanType.DEFAULT
    upgrade_requested: bool = False

    @property
    def has_time_remaining(self) -> bool:
        """Return True if user has any time left on balance."""
        return self.time_balance > 0.0

    @property
    def bandwidth(self) -> BandwidthLimit:
        """Get BandwidthLimit object."""
        return BandwidthLimit(download_kbps=self.download_limit, upload_kbps=self.upload_limit)


@dataclass
class Transaction:
    """A financial deposit / time-purchase transaction."""
    id: Optional[int] = None
    user_id: Optional[int] = None
    amount: float = 0.0
    minutes: int = 0
    created_at: Optional[datetime] = None


@dataclass
class TimeLog:
    """Audit log of time deducted from a device."""
    id: Optional[int] = None
    user_id: Optional[int] = None
    mac_address: str = ""
    minutes_deducted: float = 0.0
    balance_before: float = 0.0
    balance_after: float = 0.0
    deducted_at: Optional[datetime] = None
    deduction_type: DeductionType = DeductionType.AUTO


@dataclass
class DeviceInfo:
    """Information about a connected or monitored wireless station."""
    mac_address: str
    ip: str = "Unknown"
    hostname: str = "Unknown"
    connected: bool = True
    signal: Optional[str] = None
    time_balance: float = 0.0
    download_limit: int = 2048
    upload_limit: int = 1024
    plan: str = "default"
    upgrade_requested: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for template rendering and JSON serialization."""
        return {
            "mac_address": self.mac_address,
            "ip": self.ip,
            "hostname": self.hostname,
            "connected": self.connected,
            "signal": self.signal,
            "time_balance": self.time_balance,
            "download_limit": self.download_limit,
            "upload_limit": self.upload_limit,
            "plan": self.plan,
            "upgrade_requested": self.upgrade_requested,
        }


@dataclass
class Voucher:
    """Represents a time credit voucher/promo code."""
    code: str
    time_minutes: float
    is_used: bool = False
    used_by_mac: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        """Check if voucher has passed its expiration timestamp."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if voucher is unused and not expired."""
        return not self.is_used and not self.is_expired

    def to_dict(self) -> Dict[str, Any]:
        """Convert voucher to dictionary."""
        return {
            "code": self.code,
            "time_minutes": self.time_minutes,
            "is_used": self.is_used,
            "used_by_mac": self.used_by_mac,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_valid": self.is_valid,
        }
