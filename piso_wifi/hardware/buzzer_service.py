"""GPIO active and passive buzzer service with threaded non-blocking playback."""

from dataclasses import dataclass
import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ToneStep:
    """Represents a single tone or pause step."""
    duration_on: float      # seconds buzzer is active
    duration_off: float     # seconds of silence following tone
    frequency: int = 1500   # Hz (for passive PWM buzzer)


class BuzzerService:
    """Manages audio feedback for coin insertion, session starts, and low-time warnings.

    Supports physical active/passive buzzers via Raspberry Pi GPIO with non-blocking,
    background thread playback and software simulation fallback.
    """

    # Standard coin denomination to beep count mapping
    COIN_BEEPS = {
        1: 1,    # 1 beep for ₱1
        5: 2,    # 2 beeps for ₱5
        10: 3,   # 3 beeps for ₱10
        20: 4,   # 4 beeps for ₱20
    }

    def __init__(
        self,
        buzzer_pin: int = 24,
        passive: bool = False,
        enabled: bool = True,
        mode: str = "auto",
    ):
        """Initialize BuzzerService.

        Args:
            buzzer_pin: BCM GPIO pin number (default BCM 24).
            passive: True if using a passive piezo buzzer (PWM frequency),
                     False if using an active buzzer (HIGH/LOW logic level).
            enabled: Master switch for buzzer sounds.
            mode: 'auto', 'gpio', or 'simulated'.
        """
        self.buzzer_pin = buzzer_pin
        self.passive = passive
        self.enabled = enabled
        self.mode = mode.lower()

        self.is_hardware = False
        self.simulated = False
        self._gpio = None
        self._pwm = None

        self._lock = threading.RLock()
        self.history: List[Dict[str, Any]] = []

        # Playback queue and background worker
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        if self.enabled:
            self._init_gpio()
            self._start_worker()

    def _init_gpio(self) -> None:
        """Attempt to initialize hardware GPIO pin."""
        if self.mode == "simulated":
            self.simulated = True
            self.is_hardware = False
            logger.info("BuzzerService running in software simulated mode")
            return

        try:
            import RPi.GPIO as GPIO  # type: ignore

            self._gpio = GPIO
            self._gpio.setmode(self._gpio.BCM)
            self._gpio.setup(self.buzzer_pin, self._gpio.OUT, initial=self._gpio.LOW)

            if self.passive:
                self._pwm = self._gpio.PWM(self.buzzer_pin, 1500)
                self._pwm.start(0)

            self.is_hardware = True
            self.simulated = False
            logger.info("Initialized hardware GPIO buzzer on BCM pin %d (passive=%s)", self.buzzer_pin, self.passive)
        except Exception as e:
            logger.warning(
                "Hardware GPIO buzzer unavailable on pin %d (%s). Falling back to software simulated mode.",
                self.buzzer_pin,
                e,
            )
            self.is_hardware = False
            self.simulated = True
            self._gpio = None
            self._pwm = None

    def _start_worker(self) -> None:
        """Start daemon worker thread for non-blocking sound playback."""
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._process_queue,
            name="BuzzerWorkerThread",
            daemon=True,
        )
        self._worker_thread.start()

    def _process_queue(self) -> None:
        """Worker loop processing tone tasks sequentially in background."""
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if task is None or self._stop_event.is_set():
                break

            steps, completion_event = task
            try:
                for step in steps:
                    if self._stop_event.is_set():
                        break
                    self._play_tone_step(step)
            finally:
                if completion_event:
                    completion_event.set()
                self._queue.task_done()

    def _play_tone_step(self, step: ToneStep) -> None:
        """Execute physical or simulated tone step."""
        if self.is_hardware and self._gpio:
            try:
                if self.passive and self._pwm:
                    self._pwm.ChangeFrequency(max(50, step.frequency))
                    self._pwm.ChangeDutyCycle(50)
                    time.sleep(step.duration_on)
                    self._pwm.ChangeDutyCycle(0)
                else:
                    self._gpio.output(self.buzzer_pin, self._gpio.HIGH)
                    time.sleep(step.duration_on)
                    self._gpio.output(self.buzzer_pin, self._gpio.LOW)
            except Exception as e:
                logger.debug("Hardware buzzer output error: %s", e)
        else:
            # Simulated delay
            time.sleep(step.duration_on)

        if step.duration_off > 0 and not self._stop_event.is_set():
            time.sleep(step.duration_off)

    def is_available(self) -> bool:
        """Return True if buzzer service is enabled and operational."""
        return self.enabled and (self.is_hardware or self.simulated)

    def is_playing(self) -> bool:
        """Return True if buzzer worker is actively executing or has items queued."""
        return not self._queue.empty()

    def wait_done(self, timeout: float = 2.0) -> bool:
        """Block until all queued tones finish playing (useful in tests)."""
        deadline = time.time() + timeout
        while not self._queue.empty():
            time.sleep(0.02)
            if time.time() > deadline:
                return False
        return True

    def stop(self) -> None:
        """Stop any playing tone immediately and clear the queue."""
        with self._lock:
            # Drain queue
            while not self._queue.empty():
                try:
                    task = self._queue.get_nowait()
                    if task and task[1]:
                        task[1].set()
                    self._queue.task_done()
                except queue.Empty:
                    break

            # Turn off hardware outputs
            if self.is_hardware and self._gpio:
                try:
                    if self.passive and self._pwm:
                        self._pwm.ChangeDutyCycle(0)
                    else:
                        self._gpio.output(self.buzzer_pin, self._gpio.LOW)
                except Exception:
                    pass

    def _dispatch(self, steps: List[ToneStep], chime_name: str, meta: Optional[Dict[str, Any]] = None, blocking: bool = False) -> None:
        """Dispatch tone sequence to queue or play synchronously if blocking=True."""
        if not self.enabled:
            return

        event_data = {
            "timestamp": time.time(),
            "chime": chime_name,
            "steps_count": len(steps),
        }
        if meta:
            event_data.update(meta)

        with self._lock:
            self.history.append(event_data)
            if len(self.history) > 50:
                self.history.pop(0)

        completion_event = threading.Event() if blocking else None
        self._queue.put((steps, completion_event))

        if blocking and completion_event:
            completion_event.wait(timeout=5.0)

    def beep_coin(self, denomination: int, blocking: bool = False) -> None:
        """Play coin detection chimes.

        - 1 beep for ₱1
        - 2 beeps for ₱5
        - 3 beeps for ₱10
        - 4 beeps for ₱20
        """
        beep_count = self.COIN_BEEPS.get(denomination, max(1, denomination))
        steps = [
            ToneStep(duration_on=0.08, duration_off=0.05, frequency=1600)
            for _ in range(beep_count)
        ]
        self._dispatch(
            steps=steps,
            chime_name="beep_coin",
            meta={"denomination": denomination, "beeps": beep_count},
            blocking=blocking,
        )
        logger.debug("Buzzer: beep_coin(₱%d) -> %d beeps", denomination, beep_count)

    def beep_session_start(self, blocking: bool = False) -> None:
        """Play high pitch double beep welcoming user to active session."""
        steps = [
            ToneStep(duration_on=0.06, duration_off=0.04, frequency=2200),
            ToneStep(duration_on=0.09, duration_off=0.04, frequency=2700),
        ]
        self._dispatch(
            steps=steps,
            chime_name="beep_session_start",
            meta={"pattern": "double_high_beep"},
            blocking=blocking,
        )
        logger.debug("Buzzer: beep_session_start (high pitch double beep)")

    def beep_warning(self, blocking: bool = False) -> None:
        """Play low-time warning beep pattern."""
        steps = [
            ToneStep(duration_on=0.09, duration_off=0.06, frequency=1200),
            ToneStep(duration_on=0.09, duration_off=0.06, frequency=1200),
            ToneStep(duration_on=0.15, duration_off=0.06, frequency=900),
        ]
        self._dispatch(
            steps=steps,
            chime_name="beep_warning",
            meta={"pattern": "low_time_warning"},
            blocking=blocking,
        )
        logger.debug("Buzzer: beep_warning (low-time warning pattern)")

    def beep(self, duration: float = 0.1, count: int = 1, pause: float = 0.05, frequency: int = 1500, blocking: bool = False) -> None:
        """Play custom beep tone."""
        steps = [
            ToneStep(duration_on=duration, duration_off=pause, frequency=frequency)
            for _ in range(count)
        ]
        self._dispatch(
            steps=steps,
            chime_name="custom_beep",
            meta={"duration": duration, "count": count, "frequency": frequency},
            blocking=blocking,
        )

    def close(self) -> None:
        """Release worker thread and GPIO resources."""
        self.stop()
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._queue.put(None)
            self._worker_thread.join(timeout=1.0)

        if self.is_hardware and self._gpio:
            try:
                if self._pwm:
                    self._pwm.stop()
                    self._pwm = None
                self._gpio.cleanup(self.buzzer_pin)
            except Exception:
                pass
            self._gpio = None
