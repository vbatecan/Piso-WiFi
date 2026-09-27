"""Main Network Controller coordinating firewall, QoS, AP, and device discovery.

Composes modular sub-managers and provides backward-compatible interfaces
for Piso-WiFi core services and Flask endpoints.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Set

from piso_wifi.config import NetworkConfig
from piso_wifi.network.access_point import AccessPointManager
from piso_wifi.network.command_runner import CommandRunner, SystemCommandRunner
from piso_wifi.network.discovery import DeviceDiscovery
from piso_wifi.network.firewall import FirewallManager
from piso_wifi.network.qos import TrafficShaper

logger = logging.getLogger(__name__)


class NetworkController:
    """Unified network subsystem orchestrator for Piso-WiFi."""

    # Bandwidth plan defaults (kbps)
    DEFAULT_DOWNLOAD_SPEED = 2048  # 2 Mbps
    DEFAULT_UPLOAD_SPEED = 1024    # 1 Mbps
    PREMIUM_DOWNLOAD_SPEED = 8096  # 8 Mbps
    PREMIUM_UPLOAD_SPEED = 8096    # 8 Mbps

    def __init__(
        self,
        config: Optional[NetworkConfig] = None,
        runner: Optional[CommandRunner] = None,
        auto_start: bool = False,
        skip_system_checks: bool = False,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config = config or NetworkConfig()
        self.runner = runner or SystemCommandRunner()

        # Composed subsystem managers
        self.firewall = FirewallManager(runner=self.runner)
        self.traffic_shaper = TrafficShaper(runner=self.runner)
        self.access_point = AccessPointManager(runner=self.runner)
        self.discovery = DeviceDiscovery(runner=self.runner)

        # Backward compatibility properties
        self.ap_interface = self.config.ap_interface
        self.internet_interface = self.config.internet_interface
        if not self.internet_interface or str(self.internet_interface).lower() == "auto":
            self.internet_interface = self.resolve_internet_interface()

        self.ssid = self.config.ssid
        self.password = self.config.password
        self.ip = self.config.ip
        self.hostapd_conf = self.config.hostapd_conf
        self.dnsmasq_conf = self.config.dnsmasq_conf
        self.dnsmasq_leases = self.config.dnsmasq_leases
        self.ap_isolate = getattr(self.config, "ap_isolate", 1)

        # Track active clients
        self.connected_devices: Set[str] = set()

        if auto_start:
            if not skip_system_checks:
                self._verify_requirements()
            self._configure_ap()
            self.start_ap()
            if not self._check_ap_status():
                raise RuntimeError("Access Point failed to start properly")

    # --- Backward compatibility helpers ---

    def _execute_command(self, command: str, ignore_errors: bool = False) -> str:
        """Execute a shell command via the injected CommandRunner."""
        return self.runner.execute(command, ignore_errors=ignore_errors)

    def _is_valid_mac(self, mac: str) -> bool:
        """Validate MAC address format."""
        return self.discovery.is_valid_mac(mac)

    def _verify_requirements(self) -> None:
        """Verify host requirements and permissions."""
        self.access_point.verify_requirements(self.ap_interface)

    def configure_ap(self) -> None:
        """Configure hostapd and dnsmasq files (with client isolation)."""
        self._configure_ap()

    def _configure_ap(self) -> None:
        """Configure hostapd and dnsmasq files."""
        self.access_point.configure_ap(self.config)

    def _check_ap_status(self) -> bool:
        """Check AP interface and daemon health."""
        return self.access_point.check_ap_status(self.ap_interface, self.ip)

    def _check_hostapd_running(self) -> bool:
        """Check hostapd process status."""
        ps_out = self.runner.execute("ps aux | grep '[h]ostapd'", ignore_errors=True)
        return bool(ps_out and "hostapd" in ps_out)

    def _dump_debug_info(self) -> str:
        """Export subsystem diagnostics."""
        return self.access_point.dump_debug_info(self.ap_interface)

    def resolve_internet_interface(self) -> str:
        """Resolve the effective internet uplink interface."""
        if not self.internet_interface or str(self.internet_interface).lower() == "auto":
            self.internet_interface = self.access_point.detect_default_uplink(
                self.ap_interface
            )
        return self.internet_interface

    def _detect_uplink_interface(self) -> str:
        """Backward-compatible helper to detect uplink interface."""
        return self.access_point.detect_default_uplink(self.ap_interface)

    def get_uplink_status(self) -> Dict[str, Any]:
        """Return operational details and connectivity status for the uplink interface."""
        uplink = self.resolve_internet_interface()
        return self.access_point.get_uplink_info(uplink)

    def _setup_qos(self) -> None:
        """Initialize traffic shaper qdisc and classes."""
        self.traffic_shaper.setup_qos(
            self.ap_interface, self.DEFAULT_DOWNLOAD_SPEED
        )

    # --- Public Network Subsystem API ---

    def start_ap(self) -> None:
        """Start AP daemon, apply firewall rules, and initialize QoS."""
        uplink = self.resolve_internet_interface()
        self.access_point.start_ap(self.config)
        self.firewall.setup_nat_and_forwarding(
            self.ap_interface, uplink, self.ip
        )
        self.traffic_shaper.setup_qos(
            self.ap_interface, self.DEFAULT_DOWNLOAD_SPEED
        )
        self.logger.info("WiFi Access Point fully started and secured (uplink: %s)", uplink)

    def stop_ap(self) -> None:
        """Stop AP daemon and tear down interfaces."""
        self.access_point.stop_ap(self.ap_interface, self.internet_interface)
        self.logger.info("WiFi Access Point stopped")

    def get_connected_devices(self) -> List[Dict[str, Any]]:
        """Return connected stations and automatically block newly connected unauthenticated clients."""
        devices = self.discovery.get_connected_devices(
            self.ap_interface, self.dnsmasq_leases
        )

        current_macs = {device["mac_address"] for device in devices}
        new_devices = current_macs - self.connected_devices
        disconnected = self.connected_devices - current_macs

        for mac in new_devices:
            self.logger.info("New device discovered: %s - applying default block", mac)
            self.block_mac(mac)

        for mac in disconnected:
            self.logger.info("Device disconnected: %s", mac)

        self.connected_devices = current_macs
        return devices

    def block_mac(self, mac_address: str) -> bool:
        """Block forwarding for the specified MAC address."""
        return self.firewall.block_mac(mac_address)

    def unblock_mac(self, mac_address: str) -> bool:
        """Permit forwarding for the specified MAC address."""
        return self.firewall.unblock_mac(mac_address)

    def set_bandwidth_limit(
        self,
        mac_address: str,
        download_kbps: Optional[int] = None,
        upload_kbps: Optional[int] = None,
    ) -> bool:
        """Set download and upload limits for a connected client."""
        down = download_kbps if download_kbps is not None else self.DEFAULT_DOWNLOAD_SPEED
        up = upload_kbps if upload_kbps is not None else self.DEFAULT_UPLOAD_SPEED

        ip = self.discovery.get_ip_for_mac(mac_address, self.dnsmasq_leases)
        if not ip:
            self.logger.error("Cannot set bandwidth: no IP found for MAC %s", mac_address)
            return False

        return self.traffic_shaper.set_bandwidth_limit(
            self.ap_interface, mac_address, ip, down, up
        )

    def remove_bandwidth_limit(self, mac_address: str) -> bool:
        """Remove traffic shaping restrictions for a client."""
        ip = self.discovery.get_ip_for_mac(mac_address, self.dnsmasq_leases)
        return self.traffic_shaper.remove_bandwidth_limit(
            self.ap_interface, mac_address, ip
        )
