"""Time management and background monitoring service for Piso-WiFi."""

import logging
import threading
import time
from typing import Any, Dict, Optional

from piso_wifi.models.enums import UserStatus
from piso_wifi.services.user_service import UserService


class TimeService:
    """Service that periodically monitors connected devices and deducts time balance."""

    def __init__(
        self,
        user_service: Optional[Any] = None,
        network_controller: Optional[Any] = None,
        check_interval: int = 5,
    ):
        self.logger = logging.getLogger(__name__)
        self.check_interval = check_interval

        # Dependency injection with backward-compatible defaults
        if user_service is None:
            self.user_service = UserService()
        else:
            self.user_service = user_service

        # Backward compatibility alias
        self.user_manager = self.user_service

        if network_controller is None:
            try:
                from piso_wifi.network.controller import NetworkController
                self.network_controller = NetworkController()
            except (ImportError, ModuleNotFoundError):
                from network_controller import NetworkController
                self.network_controller = NetworkController()
        else:
            self.network_controller = network_controller

        # Tracking state
        self.last_deduction: Dict[str, float] = {}
        self.last_check: float = 0.0

        # Thread lifecycle
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        """Check if background worker thread is active."""
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    @property
    def running(self) -> bool:
        """Backward-compatibility alias for is_running."""
        return self.is_running

    def start(self) -> None:
        """Start background daemon thread for time monitoring."""
        if self.is_running:
            self.logger.warning("TimeService thread already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.logger.info("TimeService started.")

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Gracefully stop background thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.logger.info("TimeService stopped.")

    def _run(self) -> None:
        """Main background loop."""
        while not self._stop_event.is_set():
            try:
                self._check_and_deduct_time()
            except Exception as e:
                self.logger.error(f"Error in time service run loop: {e}", exc_info=True)

            # Wait check_interval or wake immediately on stop event
            if self._stop_event.wait(timeout=self.check_interval):
                break

    def _check_and_deduct_time(self) -> None:
        """Check connected devices and deduct elapsed time."""
        try:
            current_time = time.time()
            self.last_check = current_time

            connected_devices = self.network_controller.get_connected_devices()
            if connected_devices is None:
                connected_devices = []

            connected_macs = set()

            for device in connected_devices:
                if isinstance(device, dict):
                    mac = device.get("mac_address")
                else:
                    mac = getattr(device, "mac_address", None)

                if not mac:
                    continue

                mac = mac.upper()
                connected_macs.add(mac)

                try:
                    user_info = None
                    if hasattr(self.user_service, "get_user_info"):
                        user_info = self.user_service.get_user_info(mac)

                    if user_info and getattr(user_info, "status", None) == UserStatus.PAUSED:
                        self.logger.debug(f"User {mac} is paused, skipping deduction and blocking traffic")
                        self.network_controller.block_mac(mac)
                        if mac in self.last_deduction:
                            del self.last_deduction[mac]
                        continue

                    balance = self.user_service.check_balance(mac)
                    self.logger.debug(f"Current balance for {mac}: {balance}")

                    if balance <= 0:
                        self.logger.info(f"Balance zero or negative for {mac}, blocking...")
                        self.network_controller.block_mac(mac)
                        if mac in self.last_deduction:
                            del self.last_deduction[mac]
                    else:
                        # Device has positive balance
                        if mac not in self.last_deduction:
                            self.last_deduction[mac] = current_time
                        else:
                            last_time = self.last_deduction[mac]
                            elapsed_minutes = (current_time - last_time) / 60.0

                            if elapsed_minutes >= 1.0:
                                minutes_to_deduct = int(elapsed_minutes)
                                if self.user_service.deduct_time(mac, minutes_to_deduct):
                                    self.last_deduction[mac] = current_time
                                    new_balance = self.user_service.check_balance(mac)
                                    self.logger.info(
                                        f"Deducted {minutes_to_deduct} minute(s) from {mac}, remaining balance: {new_balance}"
                                    )
                                    if new_balance <= 0:
                                        self.logger.info(f"Balance depleted for {mac}, blocking...")
                                        self.network_controller.block_mac(mac)
                                        if mac in self.last_deduction:
                                            del self.last_deduction[mac]

                except Exception as e:
                    self.logger.error(f"Error processing device {mac}: {e}", exc_info=True)

            # Clean up disconnected devices from tracking dictionary
            disconnected = set(self.last_deduction.keys()) - connected_macs
            for mac in disconnected:
                del self.last_deduction[mac]

        except Exception as e:
            self.logger.error(f"Error in _check_and_deduct_time: {e}", exc_info=True)


# Backward-compatibility alias
TimeManager = TimeService
