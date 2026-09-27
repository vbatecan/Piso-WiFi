"""Comprehensive unit tests for Piso-WiFi Hardware Extensions and Anti-Cheat.

Covers:
- DisplayService (I2C 16x2 LCD, SSD1306 OLED, formatted screen states, software simulation fallback)
- BuzzerService (GPIO active/passive buzzer, non-blocking threaded playback, coin/session/warning chimes)
- AntiFishingDetector (pulse width validation, interval constraints, oscillation and reversal tampering detection)
"""

import time
import threading
from unittest.mock import MagicMock, patch
import pytest

from piso_wifi.hardware.display_service import DisplayService, I2CLCDDriver, SSD1306Driver
from piso_wifi.hardware.buzzer_service import BuzzerService, ToneStep
from piso_wifi.hardware.anti_fishing import AntiFishingDetector, ValidationResult


# ============================================================================
# DisplayService Unit Tests
# ============================================================================

class TestDisplayService:
    """Test suite for DisplayService states, drivers, and simulation."""

    def test_init_simulated_mode(self):
        """Test default fallback to simulated mode when no I2C hardware is present."""
        display = DisplayService(display_type="simulated", enabled=True)
        assert display.is_available() is True
        assert display.simulated is True
        assert display.is_hardware is False
        assert display.current_state == "OFF"
        assert display.current_line1 == ""
        assert display.current_line2 == ""
        display.close()

    def test_disabled_service(self):
        """Test that disabled DisplayService returns is_available() False and ignores renders."""
        display = DisplayService(enabled=False)
        assert display.is_available() is False
        display.update_idle()
        assert display.current_line1 == ""
        display.close()

    def test_idle_state_formatting(self):
        """Test IDLE screen state formatted strings."""
        display = DisplayService(display_type="simulated")

        # Default IP
        display.update_idle()
        assert display.current_state == "IDLE"
        assert display.current_line1 == "Piso-WiFi Ready"
        assert display.current_line2 == "IP: 192.168.4.1"

        # Custom IP
        display.update_idle(ip="10.0.0.1")
        assert display.current_line1 == "Piso-WiFi Ready"
        assert display.current_line2 == "IP: 10.0.0.1"

        lines = display.get_lines()
        assert lines == ("Piso-WiFi Ready", "IP: 10.0.0.1")
        display.close()

    def test_coin_session_state_formatting(self):
        """Test INSERT_COIN screen state formatted strings."""
        display = DisplayService(display_type="simulated")

        display.update_coin_session(remaining_seconds=30, pesos=5)
        assert display.current_state == "INSERT_COIN"
        assert display.current_line1 == "INSERT COIN NOW"
        assert display.current_line2 == "Time: 30s | ₱5"

        display.update_coin_session(remaining_seconds=12, pesos=20)
        assert display.current_line1 == "INSERT COIN NOW"
        assert display.current_line2 == "Time: 12s | ₱20"
        display.close()

    def test_active_session_state_formatting(self):
        """Test ACTIVE_SESSION screen state formatted strings."""
        display = DisplayService(display_type="simulated")

        # String input
        display.update_active_session("01h 45m")
        assert display.current_state == "ACTIVE_SESSION"
        assert display.current_line1 == "Time Remaining"
        assert display.current_line2 == "01h 45m"

        # Seconds input (6300s = 1 hour, 45 minutes)
        display.update_active_session(6300)
        assert display.current_line1 == "Time Remaining"
        assert display.current_line2 == "01h 45m"

        # Seconds input (3660s = 1 hour, 1 minute)
        display.update_active_session(3660)
        assert display.current_line1 == "Time Remaining"
        assert display.current_line2 == "01h 01m"

        # Explicit formatted_time argument
        display.update_active_session(100, formatted_time="02h 30m")
        assert display.current_line2 == "02h 30m"
        display.close()

    def test_custom_status_and_clear(self):
        """Test custom status line updates and screen clear."""
        display = DisplayService(display_type="simulated")

        display.update_status("System Booting", "Please Wait...")
        assert display.current_state == "STATUS"
        assert display.current_line1 == "System Booting"
        assert display.current_line2 == "Please Wait..."

        display.clear()
        assert display.current_state == "CLEARED"
        assert display.current_line1 == ""
        assert display.current_line2 == ""
        display.close()

    def test_rendered_ascii_output(self):
        """Test get_rendered_text produces 16x2 framed ASCII representation."""
        display = DisplayService(display_type="simulated")
        display.update_idle("192.168.4.1")

        text = display.get_rendered_text()
        assert "+----------------+" in text
        assert "|Piso-WiFi Ready |" in text
        assert "|IP: 192.168.4.1 |" in text
        display.close()

    def test_oled_display_type_fallback(self):
        """Test SSD1306 OLED mode falls back to simulated if hardware drivers not present."""
        display = DisplayService(display_type="ssd1306_oled", enabled=True, fallback_to_simulated=True)
        assert display.is_available() is True
        assert display.i2c_address == 0x3C
        display.update_status("OLED Test", "128x64")
        assert display.current_line1 == "OLED Test"
        display.close()

    def test_fallback_disabled_raises_or_marks_unavailable(self):
        """Test that if fallback_to_simulated is False and no I2C device exists, service is unavailable."""
        with patch.object(DisplayService, "_init_driver") as mock_init:
            display = DisplayService(display_type="lcd_16x2", fallback_to_simulated=False)
            display.is_hardware = False
            display.simulated = False
            assert display.is_available() is False
            display.close()

    def test_display_thread_safety(self):
        """Test concurrent updates from multiple threads do not cause race conditions."""
        display = DisplayService(display_type="simulated")

        def writer(worker_id: int):
            for i in range(20):
                display.update_status(f"Worker {worker_id}", f"Count {i}")

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert display.current_state == "STATUS"
        assert len(display.history) == 50  # Capped at 50 entries
        display.close()


# ============================================================================
# BuzzerService Unit Tests
# ============================================================================

class TestBuzzerService:
    """Test suite for BuzzerService melodies, threading, and simulation."""

    def test_buzzer_init_simulated(self):
        """Test BuzzerService initialization in simulated software mode."""
        buzzer = BuzzerService(buzzer_pin=24, mode="simulated", enabled=True)
        assert buzzer.is_available() is True
        assert buzzer.simulated is True
        assert buzzer.is_hardware is False
        assert buzzer.buzzer_pin == 24
        buzzer.close()

    def test_buzzer_disabled(self):
        """Test that disabled BuzzerService ignores play calls."""
        buzzer = BuzzerService(enabled=False)
        assert buzzer.is_available() is False
        buzzer.beep_coin(5)
        assert len(buzzer.history) == 0
        buzzer.close()

    def test_beep_coin_denominations(self):
        """Verify coin denominations produce exactly:

        ₱1 -> 1 beep, ₱5 -> 2 beeps, ₱10 -> 3 beeps, ₱20 -> 4 beeps.
        """
        buzzer = BuzzerService(mode="simulated")

        # ₱1 -> 1 beep
        buzzer.beep_coin(1, blocking=True)
        assert len(buzzer.history) == 1
        assert buzzer.history[-1]["chime"] == "beep_coin"
        assert buzzer.history[-1]["denomination"] == 1
        assert buzzer.history[-1]["beeps"] == 1

        # ₱5 -> 2 beeps
        buzzer.beep_coin(5, blocking=True)
        assert len(buzzer.history) == 2
        assert buzzer.history[-1]["denomination"] == 5
        assert buzzer.history[-1]["beeps"] == 2

        # ₱10 -> 3 beeps
        buzzer.beep_coin(10, blocking=True)
        assert len(buzzer.history) == 3
        assert buzzer.history[-1]["denomination"] == 10
        assert buzzer.history[-1]["beeps"] == 3

        # ₱20 -> 4 beeps
        buzzer.beep_coin(20, blocking=True)
        assert len(buzzer.history) == 4
        assert buzzer.history[-1]["denomination"] == 20
        assert buzzer.history[-1]["beeps"] == 4

        buzzer.close()

    def test_beep_session_start(self):
        """Test high pitch double beep chime for active session start."""
        buzzer = BuzzerService(mode="simulated")
        buzzer.beep_session_start(blocking=True)

        assert len(buzzer.history) == 1
        assert buzzer.history[0]["chime"] == "beep_session_start"
        assert buzzer.history[0]["pattern"] == "double_high_beep"
        assert buzzer.history[0]["steps_count"] == 2
        buzzer.close()

    def test_beep_warning(self):
        """Test low-time warning beep pattern."""
        buzzer = BuzzerService(mode="simulated")
        buzzer.beep_warning(blocking=True)

        assert len(buzzer.history) == 1
        assert buzzer.history[0]["chime"] == "beep_warning"
        assert buzzer.history[0]["pattern"] == "low_time_warning"
        assert buzzer.history[0]["steps_count"] == 3
        buzzer.close()

    def test_non_blocking_execution(self):
        """Test that calling beep_coin returns immediately without blocking caller."""
        buzzer = BuzzerService(mode="simulated")

        start = time.time()
        # 4 beeps take > 500ms to play physically, but call must return immediately (< 50ms)
        buzzer.beep_coin(20, blocking=False)
        elapsed = time.time() - start

        assert elapsed < 0.1  # Fast non-blocking return
        # Wait for worker queue to finish
        buzzer.wait_done(timeout=2.0)
        assert len(buzzer.history) == 1
        buzzer.close()

    def test_buzzer_stop_clears_queue(self):
        """Test stop() clears queued beeps immediately."""
        buzzer = BuzzerService(mode="simulated")

        # Queue multiple sounds
        for _ in range(5):
            buzzer.beep_coin(20, blocking=False)

        buzzer.stop()
        assert buzzer._queue.empty() is True
        buzzer.close()

    def test_custom_beep(self):
        """Test custom beep with explicit duration and count."""
        buzzer = BuzzerService(mode="simulated")
        buzzer.beep(duration=0.05, count=3, pause=0.02, frequency=2000, blocking=True)

        assert len(buzzer.history) == 1
        assert buzzer.history[0]["chime"] == "custom_beep"
        assert buzzer.history[0]["count"] == 3
        buzzer.close()


# ============================================================================
# AntiFishingDetector Unit Tests
# ============================================================================

class TestAntiFishingDetector:
    """Test suite for AntiFishingDetector tampering detection."""

    def test_valid_single_pulse(self):
        """Test normal valid single pulse (e.g. 50ms pulse for ₱1)."""
        detector = AntiFishingDetector(min_pulse_width_ms=15.0, max_interval_ms=120.0)

        res = detector.record_pulse(timestamp=100.0, pulse_width_ms=50.0)
        assert res.is_valid is True
        assert bool(res) is True
        assert res.reason == "OK"
        assert res.tamper_detected is False
        assert detector.is_tampered() is False
        assert detector.tamper_count == 0

    def test_valid_multi_pulse_coin_sequence(self):
        """Test normal 5-pulse sequence for ₱5 coin with 60ms intervals."""
        detector = AntiFishingDetector(
            min_pulse_width_ms=15.0,
            min_interval_ms=20.0,
            max_interval_ms=120.0,
        )

        base_time = 1000.0
        # 5 pulses at 60ms (0.06s) intervals
        for i in range(5):
            t = base_time + (i * 0.060)
            res = detector.record_pulse(timestamp=t, pulse_width_ms=45.0)
            assert res.is_valid is True
            assert res.tamper_detected is False

        assert detector.current_sequence_count == 5
        assert detector.is_tampered() is False
        assert detector.tamper_count == 0

    def test_pulse_width_too_narrow_flags_tampering(self):
        """Test pulse width < 15ms (noise, bounce, or wire probe) flags tampering."""
        detector = AntiFishingDetector(min_pulse_width_ms=15.0)

        # Pulse width 8ms (< 15ms threshold)
        res = detector.record_pulse(timestamp=100.0, pulse_width_ms=8.0)
        assert res.is_valid is False
        assert bool(res) is False
        assert res.tamper_detected is True
        assert "too narrow" in res.reason
        assert detector.is_tampered() is True
        assert detector.tamper_count == 1

    def test_pulses_oscillate_too_fast_flags_tampering(self):
        """Test pulses arriving faster than physical coin speed (< 20ms interval)."""
        detector = AntiFishingDetector(
            min_pulse_width_ms=15.0,
            min_interval_ms=20.0,
            max_interval_ms=120.0,
        )

        # Pulse 1 at t=10.000s
        res1 = detector.record_pulse(timestamp=10.000, pulse_width_ms=30.0)
        assert res1.is_valid is True

        # Pulse 2 arrives 8ms later (0.008s < 0.020s min interval) -> chatter/tampering
        res2 = detector.record_pulse(timestamp=10.008, pulse_width_ms=30.0)
        assert res2.is_valid is False
        assert res2.tamper_detected is True
        assert "oscillating too fast" in res2.reason
        assert detector.is_tampered() is True
        assert detector.tamper_count == 1

    def test_reverse_timestamp_flags_tampering(self):
        """Test timestamps arriving out-of-order or in reverse flags tampering."""
        detector = AntiFishingDetector()

        res1 = detector.record_pulse(timestamp=20.0, pulse_width_ms=40.0)
        assert res1.is_valid is True

        # Timestamp reversed (19.5 < 20.0)
        res2 = detector.record_pulse(timestamp=19.5, pulse_width_ms=40.0)
        assert res2.is_valid is False
        assert res2.tamper_detected is True
        assert "Reverse timestamp" in res2.reason
        assert detector.is_tampered() is True

    def test_reverse_coin_motion_direction_flags_tampering(self):
        """Test coin pull-back (direction='reverse') flags tampering."""
        detector = AntiFishingDetector()

        res = detector.record_pulse(timestamp=50.0, pulse_width_ms=40.0, direction="reverse")
        assert res.is_valid is False
        assert res.tamper_detected is True
        assert "Reverse coin motion" in res.reason
        assert detector.is_tampered() is True
        assert detector.tamper_count == 1

    def test_max_interval_sequence_demarcation(self):
        """Test interval > 120ms demarcates a new coin sequence without false tampering."""
        detector = AntiFishingDetector(max_interval_ms=120.0)

        # Coin 1: ₱1
        res1 = detector.record_pulse(timestamp=10.0, pulse_width_ms=40.0)
        assert res1.is_valid is True
        assert detector.current_sequence_count == 1

        # 500ms pause (next coin inserted by customer)
        res2 = detector.record_pulse(timestamp=10.5, pulse_width_ms=40.0)
        assert res2.is_valid is True
        # Started new coin sequence count
        assert detector.current_sequence_count == 1
        assert detector.tamper_detected is False

    def test_sequence_pulse_overflow_flags_tampering(self):
        """Test continuous pulse train exceeding 20 pulses without pause flags tampering."""
        detector = AntiFishingDetector(
            min_interval_ms=20.0,
            max_interval_ms=120.0,
            max_pulses_per_sequence=20,
        )

        base = 100.0
        # Send 20 pulses (₱20 max coin)
        for i in range(20):
            res = detector.record_pulse(timestamp=base + (i * 0.050), pulse_width_ms=25.0)
            assert res.is_valid is True

        # 21st pulse in the same train without an idle gap -> tampering!
        res21 = detector.record_pulse(timestamp=base + (20 * 0.050), pulse_width_ms=25.0)
        assert res21.is_valid is False
        assert res21.tamper_detected is True
        assert "exceeded 20 pulses" in res21.reason
        assert detector.is_tampered() is True

    def test_record_edge_transitions(self):
        """Test GPIO edge tracking with FALLING and RISING events."""
        detector = AntiFishingDetector(min_pulse_width_ms=15.0)

        # 1. Falling edge starts pulse timer
        edge_res1 = detector.record_edge("FALLING", timestamp=200.000)
        assert edge_res1 is None  # Waiting for pulse to complete
        assert detector._last_falling_edge == 200.000

        # 2. Rising edge completes 50ms pulse
        edge_res2 = detector.record_edge("RISING", timestamp=200.050)
        assert edge_res2 is not None
        assert edge_res2.is_valid is True
        assert edge_res2.pulse_width_ms == pytest.approx(50.0, rel=1e-3)
        assert edge_res2.tamper_detected is False

        # 3. Rising edge with only 8ms duration (< 15ms)
        detector.record_edge("FALLING", timestamp=201.000)
        tamper_edge = detector.record_edge("RISING", timestamp=201.008)
        assert tamper_edge is not None
        assert tamper_edge.is_valid is False
        assert tamper_edge.tamper_detected is True
        assert "too narrow" in tamper_edge.reason

        # 4. Spurious rising edge without falling edge
        anomaly = detector.record_edge("RISING", timestamp=202.000)
        assert anomaly is not None
        assert anomaly.is_valid is False
        assert anomaly.tamper_detected is True
        assert "without prior falling edge" in anomaly.reason

    def test_tamper_lockout_duration(self):
        """Test lockout duration rejects subsequent pulses until cooldown elapses."""
        detector = AntiFishingDetector(
            min_pulse_width_ms=15.0,
            lockout_duration_seconds=5.0,
        )

        # Trigger tamper
        detector.record_pulse(timestamp=100.0, pulse_width_ms=5.0)
        assert detector.is_tampered() is True

        # Pulse during lockout (at t=102.0s < 105.0s)
        locked_res = detector.record_pulse(timestamp=102.0, pulse_width_ms=50.0)
        assert locked_res.is_valid is False
        assert "lockout active" in locked_res.reason

        # Reset tamper manually
        detector.reset_tamper()
        assert detector.is_tampered() is False
        clean_res = detector.record_pulse(timestamp=103.0, pulse_width_ms=50.0)
        assert clean_res.is_valid is True

    def test_get_stats_and_reset(self):
        """Test stats dictionary reporting and full reset."""
        detector = AntiFishingDetector()
        detector.record_pulse(timestamp=10.0, pulse_width_ms=40.0)
        detector.record_pulse(timestamp=10.005, pulse_width_ms=40.0)  # Chatter tamper

        stats = detector.get_stats()
        assert stats["tamper_detected"] is True
        assert stats["tamper_count"] == 1
        assert stats["history_length"] == 2

        detector.reset()
        reset_stats = detector.get_stats()
        assert reset_stats["tamper_detected"] is False
        assert reset_stats["tamper_count"] == 0
        assert reset_stats["history_length"] == 0

    def test_validation_result_unpacking_and_indexing(self):
        """Test ValidationResult tuple unpacking and dict-like indexing."""
        res = ValidationResult(is_valid=True, reason="OK", pulse_width_ms=30.0)
        valid, reason = res
        assert valid is True
        assert reason == "OK"
        assert res["pulse_width_ms"] == 30.0
        assert res["is_valid"] is True
