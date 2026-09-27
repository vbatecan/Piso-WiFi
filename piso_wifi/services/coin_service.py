"""Coin acceptor hardware listener, pulse accumulator, and payment session manager."""

from datetime import datetime
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from piso_wifi.config import CoinSlotConfig

logger = logging.getLogger(__name__)


class CoinSlotService:
    """Manages coin slot pulse detection, active insert-coin sessions, and balance crediting."""

    def __init__(
        self,
        config: Optional[CoinSlotConfig] = None,
        user_service: Optional[Any] = None,
        minutes_per_peso: float = 5.0,
        buzzer_service: Optional[Any] = None,
        display_service: Optional[Any] = None,
        anti_fishing_detector: Optional[Any] = None,
    ):
        self.config = config or CoinSlotConfig()
        self.user_service = user_service
        self.minutes_per_peso = minutes_per_peso
        self.buzzer_service = buzzer_service
        self.display_service = display_service
        self.anti_fishing = anti_fishing_detector

        self._lock = threading.RLock()
        self._active_session: Optional[Dict[str, Any]] = None
        self._pending_pulses = 0
        self._last_pulse_timestamp = 0.0
        self._debounce_timer: Optional[threading.Timer] = None
        self._relay_state = False

        # Diagnostic counters
        self.total_pulses_detected = 0
        self.total_pesos_credited = 0
        self.recent_events: List[Dict[str, Any]] = []

        # GPIO State
        self.gpio_available = False
        self._init_gpio()

    def _init_gpio(self) -> None:
        """Attempt to initialize hardware GPIO if configured in hardware mode."""
        if self.config.mode != "gpio" or not self.config.enabled:
            logger.info("CoinSlot running in '%s' mode (hardware GPIO listener not active)", self.config.mode)
            return

        try:
            # 1. Try RPi.GPIO or gpiod
            import RPi.GPIO as GPIO  # type: ignore
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.config.signal_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            if self.config.relay_pin:
                GPIO.setup(self.config.relay_pin, GPIO.OUT, initial=GPIO.LOW)

            # Interrupt handler
            GPIO.add_event_detect(
                self.config.signal_pin,
                GPIO.FALLING,
                callback=lambda ch: self.on_pulse_detected(),
                bouncetime=20,
            )
            self.gpio_available = True
            logger.info("Hardware GPIO coin listener initialized on BCM pin %d", self.config.signal_pin)
        except (ImportError, RuntimeError, Exception) as e:
            logger.warning("Hardware GPIO unavailable (%s). Falling back to software simulated mode.", e)
            self.gpio_available = False

    def set_relay(self, state: bool) -> bool:
        """Set physical or virtual relay state (controls coin slot 12V power or LED)."""
        with self._lock:
            self._relay_state = state
            if self.gpio_available and self.config.relay_pin:
                try:
                    import RPi.GPIO as GPIO  # type: ignore
                    GPIO.output(self.config.relay_pin, GPIO.HIGH if state else GPIO.LOW)
                except Exception as e:
                    logger.debug("Failed toggling hardware relay pin: %s", e)
            logger.info("Coin slot relay state set to: %s", "ON" if state else "OFF")
            return True

    def start_payment_session(
        self,
        mac_address: str,
        duration_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start an 'Insert Coin' countdown session for a connected device MAC address."""
        norm_mac = mac_address.strip().upper()
        duration = duration_seconds or self.config.session_timeout_seconds

        with self._lock:
            now = time.time()
            expires_at = now + duration

            self._active_session = {
                "mac_address": norm_mac,
                "started_at": now,
                "expires_at": expires_at,
                "duration_seconds": duration,
                "accumulated_pesos": 0,
                "accumulated_minutes": 0.0,
            }

            # Enable 12V power / LED relay to allow coin insertion
            self.set_relay(True)

            # Hardware buzzer & display updates
            if self.buzzer_service:
                try:
                    self.buzzer_service.beep_session_start()
                except Exception as e:
                    logger.debug("Buzzer session start beep failed: %s", e)

            if self.display_service:
                try:
                    self.display_service.update_coin_session(duration, 0)
                except Exception as e:
                    logger.debug("Display coin session update failed: %s", e)

            logger.info("Started coin payment session for %s (timeout: %ds)", norm_mac, duration)
            return {
                "success": True,
                "session": self.get_active_session(),
            }

    def get_active_session(self) -> Optional[Dict[str, Any]]:
        """Return the current active insert-coin session or None if expired."""
        with self._lock:
            if not self._active_session:
                return None

            remaining = self._active_session["expires_at"] - time.time()
            if remaining <= 0:
                logger.info("Coin payment session for %s timed out", self._active_session["mac_address"])
                self._active_session = None
                self.set_relay(False)
                if self.display_service:
                    try:
                        self.display_service.update_idle()
                    except Exception:
                        pass
                return None

            session_copy = dict(self._active_session)
            session_copy["remaining_seconds"] = max(0, int(remaining))
            return session_copy

    def cancel_payment_session(self) -> bool:
        """Cancel active payment session immediately and turn off relay."""
        with self._lock:
            if self._active_session:
                logger.info("Canceled coin payment session for %s", self._active_session["mac_address"])
                self._active_session = None
            self.set_relay(False)
            if self.display_service:
                try:
                    self.display_service.update_idle()
                except Exception:
                    pass
            return True

    def on_pulse_detected(
        self,
        timestamp: Optional[float] = None,
        pulse_width_ms: Optional[float] = None,
        is_simulated: bool = False,
    ) -> bool:
        """Record an incoming electrical pulse from the coin selector."""
        with self._lock:
            # Validate pulse via anti-fishing if in hardware gpio mode and not simulated
            if self.anti_fishing and not is_simulated and self.config.mode == "gpio":
                res = self.anti_fishing.record_pulse(
                    timestamp=timestamp, pulse_width_ms=pulse_width_ms
                )
                if not res.is_valid:
                    logger.warning("Anti-fishing rejected pulse: %s", res.reason)
                    if self.buzzer_service:
                        try:
                            self.buzzer_service.beep_tamper()
                        except Exception:
                            pass
                    return False

            self._pending_pulses += 1
            self.total_pulses_detected += 1
            self._last_pulse_timestamp = time.time() if timestamp is None else float(timestamp)

            # Extend session duration while customer is actively inserting coins
            if self._active_session:
                self._active_session["expires_at"] = time.time() + self.config.session_timeout_seconds

            # Cancel prior debounce timer
            if self._debounce_timer and self._debounce_timer.is_alive():
                self._debounce_timer.cancel()

            # Schedule debounce flush
            timeout_sec = self.config.pulse_timeout_ms / 1000.0
            self._debounce_timer = threading.Timer(timeout_sec, self._flush_pending_pulses)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()
            return True

    def _flush_pending_pulses(self) -> None:
        """Flush accumulated pulses after idle debounce window and credit the user."""
        with self._lock:
            pulses = self._pending_pulses
            self._pending_pulses = 0

            if pulses <= 0:
                return

            pulses_per_peso = max(1, self.config.pulses_per_peso)
            pesos = pulses // pulses_per_peso
            leftover = pulses % pulses_per_peso

            if leftover > 0:
                logger.debug("Discarding %d unmatched pulse(s)", leftover)

            if pesos <= 0:
                return

            target_mac = self._active_session["mac_address"] if self._active_session else None
            minutes_credited = pesos * self.minutes_per_peso

            success = False
            if target_mac and self.user_service and hasattr(self.user_service, "add_time"):
                try:
                    success = self.user_service.add_time(target_mac, pesos)
                except Exception as e:
                    logger.error("Error crediting balance for %s: %s", target_mac, e)

            if self._active_session:
                self._active_session["accumulated_pesos"] += pesos
                self._active_session["accumulated_minutes"] += minutes_credited

            self.total_pesos_credited += pesos

            # Audible chime on coin credited
            if self.buzzer_service:
                try:
                    self.buzzer_service.beep_coin(pesos)
                except Exception as e:
                    logger.debug("Buzzer coin chime failed: %s", e)

            # Visual display update
            if self.display_service:
                try:
                    if self._active_session:
                        rem_sec = max(0, int(self._active_session["expires_at"] - time.time()))
                        self.display_service.update_coin_session(
                            rem_sec, self._active_session["accumulated_pesos"]
                        )
                    else:
                        self.display_service.update_payment_success(pesos, minutes_credited)
                except Exception as e:
                    logger.debug("Display coin session update failed: %s", e)

            event = {
                "timestamp": datetime.now().isoformat(),
                "pulses": pulses,
                "pesos": pesos,
                "minutes": minutes_credited,
                "mac_address": target_mac or "None (Unassigned)",
                "success": success,
            }
            self.recent_events.insert(0, event)
            if len(self.recent_events) > 20:
                self.recent_events.pop()

            logger.info(
                "Coin Credit: %d pulses -> ₱%d (%.1f mins) to MAC: %s (Success: %s)",
                pulses, pesos, minutes_credited, target_mac, success,
            )

    def simulate_coin(
        self,
        denomination: int,
        mac_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Simulate coin insertion for testing, diagnostics, and web payment top-ups."""
        if denomination not in (1, 5, 10, 20):
            return {
                "success": False,
                "error": f"Invalid coin denomination: ₱{denomination}. Supported: ₱1, ₱5, ₱10, ₱20.",
            }

        with self._lock:
            # If MAC address provided and no active session, start one automatically
            if mac_address and not self._active_session:
                self.start_payment_session(mac_address, duration_seconds=30)

            target_mac = self._active_session["mac_address"] if self._active_session else mac_address

            # Emulate pulse stream (bypassing optical/timing anti-fishing checks for simulation)
            pulses = denomination * self.config.pulses_per_peso
            for _ in range(pulses):
                self.on_pulse_detected(is_simulated=True)

            # Force immediate flush for interactive testing
            if self._debounce_timer and self._debounce_timer.is_alive():
                self._debounce_timer.cancel()
            self._flush_pending_pulses()

            minutes = denomination * self.minutes_per_peso
            return {
                "success": True,
                "denomination": denomination,
                "pulses": pulses,
                "minutes_credited": minutes,
                "mac_address": target_mac,
                "session": self.get_active_session(),
            }

    def close(self) -> None:
        """De-energize relay, stop debounce timers, and release GPIO pins."""
        with self._lock:
            if self._debounce_timer and self._debounce_timer.is_alive():
                self._debounce_timer.cancel()
            self.set_relay(False)
            if self.gpio_available and self.config.relay_pin:
                try:
                    import RPi.GPIO as GPIO  # type: ignore
                    GPIO.remove_event_detect(self.config.signal_pin)
                    GPIO.cleanup([self.config.signal_pin, self.config.relay_pin])
                except Exception:
                    pass
            self.gpio_available = False

    def get_status(self) -> Dict[str, Any]:
        """Export comprehensive coin slot operational status for diagnostics."""
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "mode": self.config.mode,
                "board_preset": self.config.board_preset,
                "signal_pin": self.config.signal_pin,
                "relay_pin": self.config.relay_pin,
                "relay_active": self._relay_state,
                "gpio_available": self.gpio_available,
                "pulses_per_peso": self.config.pulses_per_peso,
                "pulse_timeout_ms": self.config.pulse_timeout_ms,
                "total_pulses": self.total_pulses_detected,
                "total_pesos": self.total_pesos_credited,
                "active_session": self.get_active_session(),
                "recent_events": list(self.recent_events[:5]),
            }
