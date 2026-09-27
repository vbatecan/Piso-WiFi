"""Hardware extensions and anti-cheat modules for Piso-WiFi.

Provides:
- DisplayService: Support for I2C 16x2 LCD and SSD1306 0.96" OLED with software simulation fallback.
- BuzzerService: GPIO active/passive buzzer chime and melody manager with non-blocking threaded playback.
- AntiFishingDetector: Optical pulse validation, minimum pulse width, maximum interval, and anti-tamper detection.
"""

from piso_wifi.hardware.display_service import DisplayService
from piso_wifi.hardware.buzzer_service import BuzzerService
from piso_wifi.hardware.anti_fishing import AntiFishingDetector, ValidationResult

__all__ = [
    "DisplayService",
    "BuzzerService",
    "AntiFishingDetector",
    "ValidationResult",
]
