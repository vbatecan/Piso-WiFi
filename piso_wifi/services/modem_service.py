"""Cellular 4G/LTE/5G USB modem detection and WAN failover monitor."""

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Known cellular USB drivers in Linux kernel
CELLULAR_DRIVERS = {
    "cdc_ether",
    "cdc_ncm",
    "huawei_cdc_ncm",
    "qmi_wwan",
    "rndis_host",
    "cdc_mbim",
    "sierra_net",
    "option",
}


class ModemService:
    """Detects attached USB 4G/LTE dongles (Huawei HiLink, ZTE, etc.) and monitors WAN failover."""

    def __init__(self, sys_net_path: str = "/sys/class/net"):
        self.sys_net_path = sys_net_path

    def _get_driver_name(self, iface: str) -> Optional[str]:
        """Read driver symlink from sysfs if device is USB-backed."""
        driver_path = os.path.join(self.sys_net_path, iface, "device", "driver")
        try:
            if os.path.islink(driver_path):
                return os.path.basename(os.readlink(driver_path))
        except (OSError, Exception):
            pass
        return None

    def _get_operstate(self, iface: str) -> str:
        state_file = os.path.join(self.sys_net_path, iface, "operstate")
        try:
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    return f.read().strip().lower()
        except Exception:
            pass
        return "unknown"

    def _get_carrier(self, iface: str) -> bool:
        carrier_file = os.path.join(self.sys_net_path, iface, "carrier")
        try:
            if os.path.exists(carrier_file):
                with open(carrier_file, "r") as f:
                    return f.read().strip() == "1"
        except Exception:
            pass
        return False

    def list_cellular_modems(self) -> List[Dict[str, Any]]:
        """List all network interfaces identified as 4G/LTE USB cellular modems."""
        modems = []
        if not os.path.exists(self.sys_net_path):
            return modems

        try:
            interfaces = os.listdir(self.sys_net_path)
        except Exception as e:
            logger.error(f"Error reading sysfs network directory: {e}")
            return modems

        for iface in sorted(interfaces):
            if iface in ("lo",) or iface.startswith("wlan") or iface.startswith("wlp"):
                continue

            driver = self._get_driver_name(iface)
            is_cellular = False

            # Pattern-based or driver-based identification
            if iface.startswith(("usb", "wwan", "lte")):
                is_cellular = True
            elif driver and driver.lower() in CELLULAR_DRIVERS:
                is_cellular = True

            if is_cellular:
                modems.append({
                    "interface": iface,
                    "driver": driver or "generic_usb",
                    "operstate": self._get_operstate(iface),
                    "carrier": self._get_carrier(iface),
                    "is_cellular": True,
                })

        return modems

    def check_wan_failover(
        self,
        primary_iface: str = "eth0",
        gateway_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assess primary wired WAN vs secondary cellular backup status."""
        modems = self.list_cellular_modems()
        has_cellular_backup = len(modems) > 0
        primary_carrier = self._get_carrier(primary_iface)

        status = "normal"
        active_wan = primary_iface

        if not primary_carrier and has_cellular_backup:
            status = "failover_active"
            active_wan = modems[0]["interface"]
        elif not primary_carrier:
            status = "offline"
            active_wan = "none"

        return {
            "status": status,
            "primary_interface": primary_iface,
            "primary_online": primary_carrier,
            "has_cellular_backup": has_cellular_backup,
            "active_wan": active_wan,
            "cellular_modems": modems,
        }
