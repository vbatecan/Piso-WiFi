"""Quality of Service (QoS) and Bandwidth Shaping for Piso-WiFi.

Controls per-client download and upload limits using Linux Traffic Control (tc),
Hierarchical Token Bucket (HTB), SFQ fair queuing, and ingress policing.
"""

import logging
from typing import Optional

from piso_wifi.network.command_runner import CommandRunner, SystemCommandRunner

logger = logging.getLogger(__name__)


class TrafficShaper:
    """Manages Linux Traffic Control (tc) queues, classes, and filters."""

    def __init__(self, runner: Optional[CommandRunner] = None):
        self.runner = runner or SystemCommandRunner()

    @staticmethod
    def calculate_class_id(mac: str) -> int:
        """Derive a stable 20-1019 class ID from the MAC address."""
        mac_hex = mac.replace(":", "").replace("-", "")
        return int(mac_hex[-4:], 16) % 1000 + 20

    def setup_qos(self, ap_iface: str, default_download: int = 2048) -> None:
        """Initialize root HTB qdisc, parent class, and default unclassified band."""
        logger.info(
            "Initializing QoS traffic shaper on %s with default rate %d kbps",
            ap_iface,
            default_download,
        )

        # Clear any preexisting qdiscs
        self.runner.run(f"tc qdisc del dev {ap_iface} root", ignore_errors=True)
        self.runner.run(f"tc qdisc del dev {ap_iface} ingress", ignore_errors=True)

        # Create root HTB qdisc with default class 1:10
        self.runner.run(
            f"tc qdisc add dev {ap_iface} root handle 1: htb default 10"
        )

        # Main parent class (100 Mbps total trunk)
        self.runner.run(
            f"tc class add dev {ap_iface} parent 1: classid 1:1 htb rate 100mbit burst 15k"
        )

        # Default class for unclassified traffic
        self.runner.run(
            f"tc class add dev {ap_iface} parent 1:1 classid 1:10 htb rate {default_download}kbit ceil {default_download}kbit burst 15k"
        )

        # Add ingress qdisc for upload rate limiting
        self.runner.run(f"tc qdisc add dev {ap_iface} ingress")
        logger.info("QoS shaper initialized on %s", ap_iface)

    def set_bandwidth_limit(
        self,
        ap_iface: str,
        mac: str,
        ip: str,
        download_kbps: int,
        upload_kbps: int,
    ) -> bool:
        """Configure per-client download and upload rate limits."""
        try:
            class_id = self.calculate_class_id(mac)
            logger.info(
                "Setting bandwidth limit for %s (%s) [Class 1:%d]: Down=%d kbps, Up=%d kbps",
                mac,
                ip,
                class_id,
                download_kbps,
                upload_kbps,
            )

            # Clean previous rules for this client to prevent duplication
            self.remove_bandwidth_limit(ap_iface, mac, ip)

            # Create client-specific HTB class
            self.runner.run(
                f"tc class add dev {ap_iface} parent 1:1 classid 1:{class_id} htb rate {download_kbps}kbit ceil {download_kbps}kbit burst 15k"
            )

            # Add stochastic fair queuing (sfq) to prevent packet starvation
            self.runner.run(
                f"tc qdisc add dev {ap_iface} parent 1:{class_id} handle {class_id}: sfq perturb 10"
            )

            # Match incoming/outgoing packets by IP to route into client HTB class
            self.runner.run(
                f"tc filter add dev {ap_iface} parent 1: protocol ip prio 1 u32 match ip dst {ip} flowid 1:{class_id}"
            )
            self.runner.run(
                f"tc filter add dev {ap_iface} parent 1: protocol ip prio 1 u32 match ip src {ip} flowid 1:{class_id}"
            )

            # Ingress police filter for upload speed throttling
            self.runner.run(
                f"tc filter add dev {ap_iface} parent ffff: protocol ip prio 1 u32 match ip src {ip} police rate {upload_kbps}kbit burst 15k drop flowid :1"
            )

            # Ensure forward chain allows traffic for this authenticated IP
            self.runner.run(f"iptables -A FORWARD -s {ip} -j ACCEPT")
            self.runner.run(f"iptables -A FORWARD -d {ip} -j ACCEPT")

            return True

        except Exception as e:
            logger.error("Failed to set bandwidth limit for %s: %s", mac, e)
            return False

    def remove_bandwidth_limit(
        self, ap_iface: str, mac: str, ip: Optional[str] = None
    ) -> bool:
        """Remove traffic shaping classes and filters for a client."""
        try:
            class_id = self.calculate_class_id(mac)
            logger.info("Removing bandwidth limits for %s (Class 1:%d)", mac, class_id)

            self.runner.run(
                f"tc filter del dev {ap_iface} parent 1: protocol ip prio 1",
                ignore_errors=True,
            )
            self.runner.run(
                f"tc class del dev {ap_iface} classid 1:{class_id}",
                ignore_errors=True,
            )
            self.runner.run(
                f"tc qdisc del dev {ap_iface} parent 1:{class_id}",
                ignore_errors=True,
            )

            if ip:
                self.runner.run(
                    f"tc filter del dev {ap_iface} parent ffff: protocol ip prio 1 u32 match ip src {ip}",
                    ignore_errors=True,
                )
                self.runner.run(
                    f"iptables -D FORWARD -s {ip} -j ACCEPT", ignore_errors=True
                )
                self.runner.run(
                    f"iptables -D FORWARD -d {ip} -j ACCEPT", ignore_errors=True
                )

            return True

        except Exception as e:
            logger.error("Failed to remove bandwidth limits for %s: %s", mac, e)
            return False


# Alias for compatibility
QoSManager = TrafficShaper
