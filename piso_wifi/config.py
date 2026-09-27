"""Central configuration management for Piso-WiFi."""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

# Load .env file automatically
load_dotenv()


@dataclass
class BandwidthConfig:
    """Bandwidth presets and constraints in kbps."""
    default_download_kbps: int = 2048   # 2 Mbps
    default_upload_kbps: int = 1024     # 1 Mbps
    premium_download_kbps: int = 8096   # 8 Mbps
    premium_upload_kbps: int = 8096     # 8 Mbps
    min_bandwidth_kbps: int = 32
    max_bandwidth_kbps: int = 100000    # 100 Mbps


@dataclass
class NetworkConfig:
    """Network interface and wireless configuration."""
    ap_interface: str = field(
        default_factory=lambda: os.getenv("WIFI_INTERFACE", "wlan0")
    )
    internet_interface: Optional[str] = field(
        default_factory=lambda: os.getenv("INTERNET_INTERFACE") or "auto"
    )
    ssid: str = field(
        default_factory=lambda: os.getenv("AP_SSID", "PisoWiFi")
    )
    password: str = field(
        default_factory=lambda: os.getenv("AP_PASSWORD", "pisowifi123")
    )
    ip: str = field(
        default_factory=lambda: os.getenv("AP_IP", "192.168.4.1")
    )
    network_mask: str = field(
        default_factory=lambda: os.getenv("NETWORK_MASK", "255.255.255.0")
    )
    dhcp_start: str = field(
        default_factory=lambda: os.getenv("DHCP_RANGE_START", "192.168.4.2")
    )
    dhcp_end: str = field(
        default_factory=lambda: os.getenv("DHCP_RANGE_END", "192.168.4.20")
    )
    hostapd_conf: str = "/etc/hostapd/hostapd.conf"
    dnsmasq_conf: str = "/etc/dnsmasq.conf"
    dnsmasq_leases: str = "/var/lib/misc/dnsmasq.leases"
    bandwidth: BandwidthConfig = field(default_factory=BandwidthConfig)

@dataclass
class CoinSlotConfig:
    """Coin acceptor hardware and pulse detection configuration."""
    enabled: bool = field(
        default_factory=lambda: os.getenv("COIN_SLOT_ENABLED", "true").lower() in ("1", "true", "yes")
    )
    mode: str = field(
        default_factory=lambda: os.getenv("COIN_SLOT_MODE", "simulated").lower()
    )  # 'gpio', 'simulated', 'disabled'
    board_preset: str = field(
        default_factory=lambda: os.getenv("COIN_BOARD_PRESET", "raspberry_pi").lower()
    )  # 'raspberry_pi', 'orange_pi', 'custom'
    signal_pin: int = field(
        default_factory=lambda: int(os.getenv("COIN_SIGNAL_PIN", "18"))
    )  # Default: GPIO 18 (RPi) or GPIO 7 (OPi)
    relay_pin: Optional[int] = field(
        default_factory=lambda: int(os.getenv("COIN_RELAY_PIN", "0")) if os.getenv("COIN_RELAY_PIN") and os.getenv("COIN_RELAY_PIN") != "0" else None
    )
    pulses_per_peso: int = field(
        default_factory=lambda: int(os.getenv("PULSES_PER_PESO", "1"))
    )
    pulse_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("COIN_PULSE_TIMEOUT_MS", "400"))
    )
    session_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("COIN_SESSION_TIMEOUT_SECONDS", "60"))
    )


@dataclass
class AppConfig:
    """Global Piso-WiFi application configuration."""
    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "config/piso_wifi.db")
    )
    secret_key: str = field(
        default_factory=lambda: os.getenv("SECRET_KEY", "your-secret-key-here")
    )
    admin_username: str = field(
        default_factory=lambda: os.getenv("ADMIN_USERNAME", "admin")
    )
    admin_password: str = field(
        default_factory=lambda: os.getenv("ADMIN_PASSWORD", "admin123")
    )
    host: str = field(
        default_factory=lambda: os.getenv("HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: int(os.getenv("PORT", "5000"))
    )
    debug: bool = field(
        default_factory=lambda: os.getenv("FLASK_DEBUG", "False").lower() in ("1", "true", "yes")
    )
    check_interval: int = 5  # seconds
    minutes_per_peso: float = 1.0
    setup_completed: bool = field(
        default_factory=lambda: os.getenv("SETUP_COMPLETED", "false").lower() in ("1", "true", "yes")
    )
    coin_slot: CoinSlotConfig = field(default_factory=CoinSlotConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)

    def to_dict(self) -> dict:
        """Convert AppConfig to dictionary."""
        return {
            "db_path": self.db_path,
            "secret_key": self.secret_key,
            "admin_username": self.admin_username,
            "admin_password": self.admin_password,
            "host": self.host,
            "port": self.port,
            "debug": self.debug,
            "check_interval": self.check_interval,
            "minutes_per_peso": self.minutes_per_peso,
            "setup_completed": self.setup_completed,
            "coin_slot": {
                "enabled": self.coin_slot.enabled,
                "mode": self.coin_slot.mode,
                "board_preset": self.coin_slot.board_preset,
                "signal_pin": self.coin_slot.signal_pin,
                "relay_pin": self.coin_slot.relay_pin,
                "pulses_per_peso": self.coin_slot.pulses_per_peso,
                "pulse_timeout_ms": self.coin_slot.pulse_timeout_ms,
                "session_timeout_seconds": self.coin_slot.session_timeout_seconds,
            },
            "network": {
                "ap_interface": self.network.ap_interface,
                "internet_interface": self.network.internet_interface,
                "ssid": self.network.ssid,
                "password": self.network.password,
                "ip": self.network.ip,
                "network_mask": self.network.network_mask,
                "dhcp_start": self.network.dhcp_start,
                "dhcp_end": self.network.dhcp_end,
                "bandwidth": {
                    "default_download_kbps": self.network.bandwidth.default_download_kbps,
                    "default_upload_kbps": self.network.bandwidth.default_upload_kbps,
                    "premium_download_kbps": self.network.bandwidth.premium_download_kbps,
                    "premium_upload_kbps": self.network.bandwidth.premium_upload_kbps,
                },
            },
        }



def save_env_file(updates: dict, env_path: str = ".env") -> bool:
    """Save or update environment key-value pairs persistently in .env file.

    Preserves existing lines and comments if .env exists, or creates a new
    .env file using .env.example as a template if available.
    Also updates os.environ immediately in the current process.
    """
    try:
        # Load existing lines if file exists
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        elif os.path.exists(".env.example"):
            with open(".env.example", "r", encoding="utf-8") as f:
                lines = f.readlines()

        existing_keys = set()
        new_lines = []

        # Update existing keys
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, _ = stripped.split("=", 1)
                key = key.strip()
                existing_keys.add(key)
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}\n")
                    os.environ[key] = str(updates[key])
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # Append new keys that were not present in existing lines
        for key, val in updates.items():
            if key not in existing_keys:
                new_lines.append(f"{key}={val}\n")
                os.environ[key] = str(val)

        # Ensure directory exists if needed
        parent_dir = os.path.dirname(env_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to write env file %s: %s", env_path, e)
        return False


def load_app_config(env_path: str = ".env") -> AppConfig:
    """Reload .env file and return fresh AppConfig instance."""
    if os.path.exists(env_path):
        load_dotenv(env_path, override=True)
    return AppConfig()

