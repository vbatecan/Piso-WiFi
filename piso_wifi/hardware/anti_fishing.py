"""Anti-fishing and pulse integrity detector for Allan 1239 multi-coin selectors."""

from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Outcome of coin pulse validation."""
    is_valid: bool
    reason: str = "OK"
    pulse_width_ms: Optional[float] = None
    interval_ms: Optional[float] = None
    tamper_detected: bool = False
    timestamp: float = field(default_factory=time.time)

    def __bool__(self) -> bool:
        return self.is_valid

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def __iter__(self):
        yield self.is_valid
        yield self.reason


class AntiFishingDetector:
    """Detects coin-on-a-string fishing, sensor chatter, and line tampering.

    Allan 1239 multi-coin selectors produce clean square-wave pulse trains:
      - 1 pulse = ₱1
      - 5 pulses = ₱5
      - 10 pulses = ₱10
      - 20 pulses = ₱20

    Legitimate pulses have:
      - Pulse width >= 15ms (typically 20ms-100ms depending on selector switch).
      - Interval between pulses <= 120ms within a multi-pulse sequence.
      - Interval between pulses >= 20ms (oscillation faster than 20ms indicates chatter/string pull).
      - Monotonic, forward-moving sensor transitions (reversal indicates coin pull-back).
    """

    def __init__(
        self,
        min_pulse_width_ms: float = 15.0,
        max_pulse_width_ms: float = 250.0,
        min_interval_ms: float = 20.0,
        max_interval_ms: float = 120.0,
        max_pulses_per_sequence: int = 20,
        lockout_duration_seconds: float = 0.0,
    ):
        """Initialize AntiFishingDetector.

        Args:
            min_pulse_width_ms: Minimum valid pulse duration (default 15ms).
            max_pulse_width_ms: Maximum allowable pulse duration (default 250ms).
            min_interval_ms: Minimum interval between pulses (faster oscillation flags tampering).
            max_interval_ms: Maximum interval between pulses in a multi-pulse sequence (default 120ms).
            max_pulses_per_sequence: Maximum allowable pulses in a continuous sequence (default 20 for ₱20).
            lockout_duration_seconds: Duration to reject pulses following a confirmed tampering event.
        """
        self.min_pulse_width_ms = float(min_pulse_width_ms)
        self.max_pulse_width_ms = float(max_pulse_width_ms)
        self.min_interval_ms = float(min_interval_ms)
        self.max_interval_ms = float(max_interval_ms)
        self.max_pulses_per_sequence = int(max_pulses_per_sequence)
        self.lockout_duration_seconds = float(lockout_duration_seconds)

        self._lock = threading.RLock()

        # Pulse tracking state
        self.last_pulse_timestamp: Optional[float] = None
        self.last_pulse_width_ms: Optional[float] = None
        self.current_sequence_count: int = 0

        # Edge tracking for GPIO transitions
        self._last_falling_edge: Optional[float] = None
        self._edge_state: Optional[str] = None

        # Tampering metrics
        self.tamper_detected: bool = False
        self.tamper_count: int = 0
        self.last_tamper_reason: Optional[str] = None
        self.last_tamper_timestamp: float = 0.0
        self.last_tamper_realtime: float = 0.0
        self._last_event_time: Optional[float] = None

        # History buffer for analysis
        self.history: List[ValidationResult] = []

    def validate_pulse_width(self, pulse_width_ms: float) -> bool:
        """Check if pulse width is within valid physical limits."""
        return self.min_pulse_width_ms <= pulse_width_ms <= self.max_pulse_width_ms

    def validate_interval(self, interval_ms: float) -> bool:
        """Check if interval between pulses is not oscillating too fast."""
        return interval_ms >= self.min_interval_ms

    def is_tampered(self, now: Optional[float] = None) -> bool:
        """Return True if tampering has been flagged and is currently active."""
        with self._lock:
            if not self.tamper_detected:
                return False
            if self.lockout_duration_seconds > 0:
                if now is not None:
                    elapsed = float(now) - self.last_tamper_timestamp
                    return 0 <= elapsed < self.lockout_duration_seconds

                # Default to real time check, with synthetic fallback
                elapsed_real = time.time() - self.last_tamper_realtime
                if 0 <= elapsed_real < self.lockout_duration_seconds:
                    return True

                if self._last_event_time is not None:
                    elapsed_event = self._last_event_time - self.last_tamper_timestamp
                    if 0 <= elapsed_event < self.lockout_duration_seconds:
                        return True

                return False
            return self.tamper_detected

    def reset_tamper(self) -> None:
        """Clear tamper flag and reset sequence state."""
        with self._lock:
            self.tamper_detected = False
            self.last_tamper_reason = None
            self.current_sequence_count = 0
            self._last_falling_edge = None
            self._edge_state = None

    def reset(self) -> None:
        """Reset all tracking and tampering metrics."""
        with self._lock:
            self.reset_tamper()
            self.tamper_count = 0
            self.last_pulse_timestamp = None
            self.last_pulse_width_ms = None
            self.history.clear()

    def record_pulse(
        self,
        timestamp: Optional[float] = None,
        pulse_width_ms: Optional[float] = None,
        direction: str = "forward",
    ) -> ValidationResult:
        """Ingest and validate a coin pulse.

        Args:
            timestamp: Epoch timestamp of the pulse (defaults to time.time()).
            pulse_width_ms: Measured width of the pulse in milliseconds.
            direction: Direction of coin movement ('forward', 'reverse', etc.).

        Returns:
            ValidationResult with is_valid=True or is_valid=False and detailed reason.
        """
        now = time.time() if timestamp is None else float(timestamp)

        with self._lock:
            self._last_event_time = now
            # 1. Check lockout if configured
            if self.lockout_duration_seconds > 0 and self.tamper_detected:
                elapsed = now - self.last_tamper_timestamp
                if elapsed < self.lockout_duration_seconds:
                    res = ValidationResult(
                        is_valid=False,
                        reason=f"Tamper lockout active ({elapsed:.1f}s / {self.lockout_duration_seconds:.1f}s)",
                        tamper_detected=True,
                        timestamp=now,
                    )
                    self._record_history(res)
                    return res

            # 2. Check direction reversal (fishing string pull-back)
            norm_dir = direction.strip().lower()
            if norm_dir in ("reverse", "backward", "pullback", "back", "rev"):
                return self._flag_tamper("Reverse coin motion detected (fishing pull-back)", now)

            # 3. Check timestamp reversal / out-of-order delivery
            if self.last_pulse_timestamp is not None and now < self.last_pulse_timestamp:
                return self._flag_tamper(
                    f"Reverse timestamp detected ({now:.4f} < {self.last_pulse_timestamp:.4f})",
                    now,
                )

            # 4. Check pulse width constraints
            if pulse_width_ms is not None:
                pw = float(pulse_width_ms)
                if pw < self.min_pulse_width_ms:
                    return self._flag_tamper(
                        f"Pulse width {pw:.1f}ms too narrow (< {self.min_pulse_width_ms}ms, noise or string chatter)",
                        now,
                        pulse_width_ms=pw,
                    )
                if pw > self.max_pulse_width_ms:
                    res = ValidationResult(
                        is_valid=False,
                        reason=f"Pulse width {pw:.1f}ms exceeds maximum ({self.max_pulse_width_ms}ms)",
                        pulse_width_ms=pw,
                        tamper_detected=False,
                        timestamp=now,
                    )
                    self._record_history(res)
                    return res

            # 5. Check interval and oscillation frequency
            interval_ms: Optional[float] = None
            if self.last_pulse_timestamp is not None:
                interval_ms = (now - self.last_pulse_timestamp) * 1000.0

                # Pulses oscillating too fast (< min_interval_ms)
                if interval_ms < self.min_interval_ms:
                    return self._flag_tamper(
                        f"Pulses oscillating too fast ({interval_ms:.1f}ms < {self.min_interval_ms}ms)",
                        now,
                        interval_ms=interval_ms,
                        pulse_width_ms=pulse_width_ms,
                    )

                # Sequence tracking: if within max_interval_ms, belongs to same multi-pulse train
                if interval_ms <= self.max_interval_ms:
                    self.current_sequence_count += 1
                    if self.current_sequence_count > self.max_pulses_per_sequence:
                        return self._flag_tamper(
                            f"Pulse sequence exceeded {self.max_pulses_per_sequence} pulses without idle pause",
                            now,
                            interval_ms=interval_ms,
                            pulse_width_ms=pulse_width_ms,
                        )
                else:
                    # New coin sequence begins
                    self.current_sequence_count = 1
            else:
                self.current_sequence_count = 1

            # All checks passed
            self.last_pulse_timestamp = now
            self.last_pulse_width_ms = pulse_width_ms

            result = ValidationResult(
                is_valid=True,
                reason="OK",
                pulse_width_ms=pulse_width_ms,
                interval_ms=interval_ms,
                tamper_detected=False,
                timestamp=now,
            )
            self._record_history(result)
            return result

    def validate_pulse(
        self,
        timestamp: Optional[float] = None,
        pulse_width_ms: Optional[float] = None,
        direction: str = "forward",
    ) -> ValidationResult:
        """Alias for record_pulse."""
        return self.record_pulse(timestamp=timestamp, pulse_width_ms=pulse_width_ms, direction=direction)

    def ingest_pulse(
        self,
        timestamp: Optional[float] = None,
        pulse_width_ms: Optional[float] = None,
        direction: str = "forward",
    ) -> ValidationResult:
        """Alias for record_pulse."""
        return self.record_pulse(timestamp=timestamp, pulse_width_ms=pulse_width_ms, direction=direction)

    def record_edge(self, edge: Union[str, int], timestamp: Optional[float] = None) -> Optional[ValidationResult]:
        """Track hardware GPIO edge transitions (FALLING and RISING).

        Computes pulse width from FALLING edge to RISING edge and invokes record_pulse.
        Returns ValidationResult on RISING edge completion, or None on FALLING edge.
        """
        now = time.time() if timestamp is None else float(timestamp)
        edge_name = "FALLING" if edge in ("FALLING", "falling", 0, GPIO_FALLING := 0) else "RISING"

        with self._lock:
            if edge_name == "FALLING":
                self._last_falling_edge = now
                self._edge_state = "FALLING"
                return None

            # RISING edge: compute pulse width
            if self._last_falling_edge is None or self._edge_state != "FALLING":
                # Rising edge without a preceding falling edge -> reverse edge anomaly
                return self._flag_tamper("Rising edge without prior falling edge (reverse polarity or chatter)", now)

            if now < self._last_falling_edge:
                return self._flag_tamper("Reverse edge timestamps (rising edge occurred before falling edge)", now)

            pulse_width_ms = (now - self._last_falling_edge) * 1000.0
            self._edge_state = "RISING"
            self._last_falling_edge = None

            return self.record_pulse(timestamp=now, pulse_width_ms=pulse_width_ms)

    def _flag_tamper(
        self,
        reason: str,
        timestamp: float,
        pulse_width_ms: Optional[float] = None,
        interval_ms: Optional[float] = None,
    ) -> ValidationResult:
        """Mark tampering event, increment counters, and log warning."""
        self.tamper_detected = True
        self.tamper_count += 1
        self.last_tamper_reason = reason
        self.last_tamper_timestamp = timestamp
        self.last_tamper_realtime = time.time()
        self._last_event_time = timestamp
        self.current_sequence_count = 0

        logger.warning("AntiFishing alert: %s", reason)
        res = ValidationResult(
            is_valid=False,
            reason=reason,
            pulse_width_ms=pulse_width_ms,
            interval_ms=interval_ms,
            tamper_detected=True,
            timestamp=timestamp,
        )
        self._record_history(res)
        return res

    def _record_history(self, res: ValidationResult) -> None:
        """Record validation result in rolling log."""
        self.history.append(res)
        if len(self.history) > 100:
            self.history.pop(0)

    def get_stats(self) -> Dict[str, Any]:
        """Return diagnostic counters and tamper metrics."""
        with self._lock:
            return {
                "tamper_detected": self.is_tampered(),
                "tamper_count": self.tamper_count,
                "last_tamper_reason": self.last_tamper_reason,
                "last_tamper_timestamp": self.last_tamper_timestamp,
                "current_sequence_count": self.current_sequence_count,
                "min_pulse_width_ms": self.min_pulse_width_ms,
                "max_interval_ms": self.max_interval_ms,
                "min_interval_ms": self.min_interval_ms,
                "history_length": len(self.history),
            }
