"""Wireless client discovery and DHCP inspection for Piso-WiFi.

Monitors active wireless stations using iw, parses dnsmasq DHCP leases,
queries system ARP tables, and tracks client connection metrics.
"""

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from piso_wifi.network.command_runner import CommandRunner, SystemCommandRunner

logger = logging.getLogger(__name__)


class DeviceDict(dict):
    """Dictionary representing device info that also supports checking values with 'in'."""

    def __contains__(self, item: Any) -> bool:
        if super().__contains__(item):
            return True
        for val in self.values():
            if item == val:
                return True
            if isinstance(val, str) and item in val:
                return True
        return False


class DeviceDiscovery:
    """Discovers connected wireless stations and maps them to IP/DHCP leases."""

    def __init__(self, runner: Optional[CommandRunner] = None):
        self.runner = runner or SystemCommandRunner()

    @staticmethod
    def is_valid_mac(mac: str) -> bool:
        """Validate whether a string matches a standard 6-byte MAC address format."""
        if not mac or not isinstance(mac, str):
            return False
        return bool(
            re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", mac.strip())
        )

    def get_dhcp_leases(
        self,
        leases_file: str = "/var/lib/misc/dnsmasq.leases",
        subnet_prefix: str = "192.168.4.",
    ) -> Dict[str, Dict[str, Any]]:
        """Read and parse active leases from dnsmasq.leases file.

        Returns a dictionary mapping uppercase MAC addresses to their IP and hostname.
        """
        dhcp_info: Dict[str, Dict[str, Any]] = {}
        if not os.path.exists(leases_file):
            logger.debug("Leases file %s does not exist", leases_file)
            return dhcp_info

        try:
            current_time = int(time.time())
            with open(leases_file, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        try:
                            lease_expiry = int(parts[0])
                        except ValueError:
                            continue

                        mac = parts[1].upper()
                        ip = parts[2]
                        hostname = parts[3] if parts[3] != "*" else "Unknown"

                        # Filter for active leases matching the hotspot subnet
                        if lease_expiry > current_time and (
                            not subnet_prefix or ip.startswith(subnet_prefix)
                        ):
                            dhcp_info[mac] = {
                                "ip": ip,
                                "hostname": hostname,
                                "lease_expiry": lease_expiry,
                            }

            logger.debug("Found %d active DHCP leases", len(dhcp_info))
            return dhcp_info

        except Exception as e:
            logger.warning("Error reading DHCP leases file %s: %s", leases_file, e)
            return dhcp_info

    def get_station_dump(self, ap_iface: str) -> List[str]:
        """Query kernel wireless subsystem for currently associated stations."""
        macs: List[str] = []
        try:
            output = self.runner.execute(
                f"iw dev {ap_iface} station dump", ignore_errors=True
            )
            for line in output.splitlines():
                if "Station" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        candidate_mac = parts[1].upper()
                        if self.is_valid_mac(candidate_mac):
                            macs.append(candidate_mac)

            logger.debug(
                "Discovered %d stations via iw station dump on %s",
                len(macs),
                ap_iface,
            )
            return macs

        except Exception as e:
            logger.warning("Station dump query failed on %s: %s", ap_iface, e)
            return macs

    def get_station_info(self, ap_iface: str, mac: str) -> Dict[str, Any]:
        """Fetch wireless physical layer metrics (signal strength, bytes, time)."""
        info: Dict[str, Any] = {
            "signal": None,
            "rx_bytes": None,
            "tx_bytes": None,
            "connected_time": None,
        }

        try:
            raw = self.runner.execute(
                f"iw dev {ap_iface} station get {mac}", ignore_errors=True
            )
            signal_match = re.search(r"signal:\s*([-\d]+)\s*dBm", raw)
            if signal_match:
                info["signal"] = f"{signal_match.group(1)} dBm"

            rx_match = re.search(r"rx bytes:\s*(\d+)", raw)
            if rx_match:
                info["rx_bytes"] = int(rx_match.group(1))

            tx_match = re.search(r"tx bytes:\s*(\d+)", raw)
            if tx_match:
                info["tx_bytes"] = int(tx_match.group(1))

            time_match = re.search(r"connected time:\s*(\d+)\s*seconds", raw)
            if time_match:
                info["connected_time"] = int(time_match.group(1))

            return info

        except Exception as e:
            logger.debug("Failed to retrieve station info for %s: %s", mac, e)
            return info

    def get_ip_for_mac(
        self, mac: str, leases_file: Optional[str] = None
    ) -> Optional[str]:
        """Resolve IP address for a MAC address using leases or system ARP cache."""
        mac_upper = mac.upper()

        # 1. Check leases file first
        if leases_file and os.path.exists(leases_file):
            leases = self.get_dhcp_leases(leases_file, subnet_prefix="")
            if mac_upper in leases:
                return leases[mac_upper]["ip"]

        # 2. Inspect ARP cache (support standard linux arp -n or arp -a)
        try:
            arp_output = self.runner.execute("arp -n", ignore_errors=True)
            for line in arp_output.splitlines():
                if mac_upper in line.upper():
                    parts = line.strip().split()
                    if parts and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", parts[0]):
                        return parts[0]

            # Try parsing BSD-style arp output '? (192.168.4.5) at 00:11:22... [ether]'
            for line in arp_output.splitlines():
                m = re.search(r"\(([\d.]+)\)\s+at\s+([0-9a-fA-F:]+)", line)
                if m and m.group(2).upper() == mac_upper:
                    return m.group(1)

        except Exception as e:
            logger.debug("ARP lookup failed for %s: %s", mac, e)

        return None

    def get_connected_devices(
        self,
        ap_iface: str,
        leases_file: str = "/var/lib/misc/dnsmasq.leases",
    ) -> List[Dict[str, Any]]:
        """Return full list of connected clients with IP, hostname, and metrics."""
        connected_devices: List[Dict[str, Any]] = []

        # Get active DHCP leases for fast lookup
        dhcp_info = self.get_dhcp_leases(leases_file)

        # Get stations connected to AP interface
        mac_list = self.get_station_dump(ap_iface)

        # Fallback to ARP entries if station dump yielded no MACs
        if not mac_list:
            try:
                arp_output = self.runner.execute("arp -n", ignore_errors=True)
                if not arp_output:
                    arp_output = self.runner.execute("arp -a", ignore_errors=True)
                for line in arp_output.splitlines():
                    found_macs = re.findall(
                        r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", line
                    )
                    for candidate in found_macs:
                        cand_upper = candidate.upper()
                        if self.is_valid_mac(cand_upper) and cand_upper not in mac_list:
                            mac_list.append(cand_upper)
            except Exception as e:
                logger.debug("Fallback ARP extraction failed: %s", e)

        for mac in mac_list:
            stats = self.get_station_info(ap_iface, mac)
            dhcp_entry = dhcp_info.get(mac, {})

            ip = dhcp_entry.get("ip") or self.get_ip_for_mac(mac, leases_file) or "Unknown"
            hostname = dhcp_entry.get("hostname") or "Unknown"

            device_info = DeviceDict({
                "mac_address": mac,
                "ip": ip,
                "hostname": hostname,
                "connected": True,
                "signal": stats.get("signal"),
                "rx_bytes": stats.get("rx_bytes"),
                "tx_bytes": stats.get("tx_bytes"),
                "connected_time": stats.get("connected_time"),
            })
            connected_devices.append(device_info)

        return connected_devices
