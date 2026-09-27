"""I2C 16x2 LCD and SSD1306 OLED display service with software simulation fallback."""

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class I2CLCDDriver:
    """Hardware driver for HD44780 16x2 LCD with PCF8574 I2C backpack."""

    # LCD Commands
    LCD_CLEARDISPLAY = 0x01
    LCD_RETURNHOME = 0x02
    LCD_ENTRYMODESET = 0x04
    LCD_DISPLAYCONTROL = 0x08
    LCD_FUNCTIONSET = 0x20
    LCD_SETDDRAMADDR = 0x80

    # Flags for display entry mode
    LCD_ENTRYLEFT = 0x02
    LCD_ENTRYSHIFTDECREMENT = 0x00

    # Flags for display on/off control
    LCD_DISPLAYON = 0x04
    LCD_CURSOROFF = 0x00
    LCD_BLINKOFF = 0x00

    # Flags for function set
    LCD_4BITMODE = 0x00
    LCD_2LINE = 0x08
    LCD_5x8DOTS = 0x00

    # Flags for backlight control
    LCD_BACKLIGHT = 0x08
    LCD_NOBACKLIGHT = 0x00
    ENABLE_BIT = 0x04

    def __init__(self, bus_num: int = 1, address: int = 0x27):
        self.bus_num = bus_num
        self.address = address
        self.backlight_state = self.LCD_BACKLIGHT
        self.bus = None

        # 1. Try rpi_lcd
        try:
            from rpi_lcd import LCD  # type: ignore
            self._rpi_lcd = LCD(address=address, bus=bus_num)
            self._driver_type = "rpi_lcd"
            logger.info("Initialized LCD via rpi_lcd on bus %d, address 0x%02X", bus_num, address)
            return
        except (ImportError, Exception):
            self._rpi_lcd = None

        # 2. Try smbus2 or smbus
        smbus_mod = None
        for mod_name in ("smbus2", "smbus"):
            try:
                smbus_mod = __import__(mod_name)
                break
            except ImportError:
                continue

        if smbus_mod is None:
            raise RuntimeError("No SMBus library available (smbus2 or smbus required for direct I2C LCD)")

        self.bus = smbus_mod.SMBus(self.bus_num)
        self._driver_type = "smbus"
        self._init_hd44780()
        logger.info("Initialized HD44780 LCD via %s on bus %d, address 0x%02X", smbus_mod.__name__, bus_num, address)

    def _write_byte(self, data: int) -> None:
        if self.bus:
            self.bus.write_byte(self.address, data | self.backlight_state)

    def _pulse_enable(self, data: int) -> None:
        self._write_byte(data | self.ENABLE_BIT)
        time.sleep(0.0005)
        self._write_byte(data & ~self.ENABLE_BIT)
        time.sleep(0.0001)

    def _write_nibble(self, nibble: int, mode: int) -> None:
        # mode: 0 = command, 1 = data
        high_bits = (nibble & 0x0F) << 4
        self._write_byte(high_bits | mode)
        self._pulse_enable(high_bits | mode)

    def send_byte(self, value: int, mode: int) -> None:
        self._write_nibble((value >> 4) & 0x0F, mode)
        self._write_nibble(value & 0x0F, mode)

    def _init_hd44780(self) -> None:
        time.sleep(0.05)
        # 4-bit initialization sequence
        self._write_nibble(0x03, 0)
        time.sleep(0.005)
        self._write_nibble(0x03, 0)
        time.sleep(0.001)
        self._write_nibble(0x03, 0)
        self._write_nibble(0x02, 0)  # Set 4-bit mode

        # Function set: 4-bit, 2 lines, 5x8 font
        self.send_byte(self.LCD_FUNCTIONSET | self.LCD_4BITMODE | self.LCD_2LINE | self.LCD_5x8DOTS, 0)
        # Display control: display on, cursor off, blink off
        self.send_byte(self.LCD_DISPLAYCONTROL | self.LCD_DISPLAYON | self.LCD_CURSOROFF | self.LCD_BLINKOFF, 0)
        # Entry mode: left-to-right, no shift
        self.send_byte(self.LCD_ENTRYMODESET | self.LCD_ENTRYLEFT | self.LCD_ENTRYSHIFTDECREMENT, 0)
        self.clear()

    def clear(self) -> None:
        if self._rpi_lcd:
            self._rpi_lcd.clear()
            return
        if self.bus:
            self.send_byte(self.LCD_CLEARDISPLAY, 0)
            time.sleep(0.002)

    def display_lines(self, line1: str, line2: str) -> None:
        l1 = line1.ljust(16)[:16]
        l2 = line2.ljust(16)[:16]

        if self._rpi_lcd:
            self._rpi_lcd.text(l1, 1)
            self._rpi_lcd.text(l2, 2)
            return

        if self.bus:
            # Line 1 (DDRAM 0x00)
            self.send_byte(self.LCD_SETDDRAMADDR | 0x00, 0)
            for char in l1:
                self.send_byte(ord(char), 1)

            # Line 2 (DDRAM 0x40)
            self.send_byte(self.LCD_SETDDRAMADDR | 0x40, 0)
            for char in l2:
                self.send_byte(ord(char), 1)

    def close(self) -> None:
        if self.bus:
            try:
                self.bus.close()
            except Exception:
                pass
            self.bus = None


class SSD1306Driver:
    """Hardware driver for SSD1306 0.96" OLED (128x64 or 128x32)."""

    def __init__(self, bus_num: int = 1, address: int = 0x3C, width: int = 128, height: int = 64):
        self.bus_num = bus_num
        self.address = address
        self.width = width
        self.height = height
        self.device = None
        self._driver_type = None

        # 1. Try luma.oled
        try:
            from luma.core.interface.serial import i2c  # type: ignore
            from luma.oled.device import ssd1306  # type: ignore

            serial = i2c(port=bus_num, address=address)
            self.device = ssd1306(serial, width=width, height=height)
            self._driver_type = "luma"
            logger.info("Initialized SSD1306 OLED via luma.oled on bus %d, address 0x%02X", bus_num, address)
            return
        except (ImportError, Exception):
            pass

        # 2. Try Adafruit_SSD1306
        try:
            import Adafruit_SSD1306  # type: ignore

            self.device = Adafruit_SSD1306.SSD1306_128_64(rst=None, i2c_bus=bus_num, i2c_address=address)
            self.device.begin()
            self.device.clear()
            self.device.display()
            self._driver_type = "adafruit"
            logger.info("Initialized SSD1306 OLED via Adafruit_SSD1306 on bus %d, address 0x%02X", bus_num, address)
            return
        except (ImportError, Exception):
            pass

        raise RuntimeError("No compatible SSD1306 driver found (luma.oled or Adafruit_SSD1306 required)")

    def clear(self) -> None:
        if self._driver_type == "luma" and self.device:
            self.device.clear()
        elif self._driver_type == "adafruit" and self.device:
            self.device.clear()
            self.device.display()

    def display_lines(self, line1: str, line2: str) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore

            image = Image.new("1", (self.width, self.height))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, self.width, self.height), outline=0, fill=0)

            try:
                font = ImageFont.load_default()
            except Exception:
                font = None

            # Render 2 lines centered/formatted on OLED screen
            draw.text((0, 8), line1[:20], font=font, fill=255)
            draw.text((0, 32), line2[:20], font=font, fill=255)

            if self._driver_type == "luma" and self.device:
                self.device.display(image)
            elif self._driver_type == "adafruit" and self.device:
                self.device.image(image)
                self.device.display()
        except ImportError:
            logger.debug("PIL not available for OLED rendering; skipped physical render")

    def close(self) -> None:
        if self._driver_type == "luma" and self.device:
            try:
                self.device.cleanup()
            except Exception:
                pass


class DisplayService:
    """Manages I2C LCD/OLED character screen outputs with software simulation mode.

    Supports formatted states:
      - IDLE: Line 1 = "Piso-WiFi Ready", Line 2 = "IP: 192.168.4.1"
      - INSERT_COIN: Line 1 = "INSERT COIN NOW", Line 2 = "Time: 30s | ₱5"
      - ACTIVE_SESSION: Line 1 = "Time Remaining", Line 2 = "01h 45m"
    """

    def __init__(
        self,
        display_type: str = "lcd_16x2",
        i2c_bus: int = 1,
        i2c_address: Optional[int] = None,
        enabled: bool = True,
        fallback_to_simulated: bool = True,
    ):
        """Initialize display service.

        Args:
            display_type: 'lcd_16x2', 'ssd1306_oled', 'simulated', or 'auto'.
            i2c_bus: I2C bus number (typically 1 on Raspberry Pi).
            i2c_address: Device address (default 0x27 for LCD, 0x3C for OLED).
            enabled: Master switch for display functionality.
            fallback_to_simulated: Automatically switch to software simulation if hardware fails.
        """
        self.display_type = display_type.lower()
        self.i2c_bus = i2c_bus
        self.enabled = enabled
        self.fallback_to_simulated = fallback_to_simulated

        # Assign default addresses if omitted
        if i2c_address is not None:
            self.i2c_address = i2c_address
        elif "oled" in self.display_type or "ssd1306" in self.display_type:
            self.i2c_address = 0x3C
        else:
            self.i2c_address = 0x27

        self._lock = threading.RLock()
        self.driver: Optional[Union[I2CLCDDriver, SSD1306Driver]] = None
        self.is_hardware: bool = False
        self.simulated: bool = False

        # Current screen state buffer
        self.current_line1: str = ""
        self.current_line2: str = ""
        self.current_state: str = "OFF"
        self.history: List[Dict[str, Any]] = []

        if self.enabled:
            self._init_driver()

    def _init_driver(self) -> None:
        """Attempt to initialize hardware driver or fallback to simulated mode."""
        if self.display_type == "simulated":
            self.simulated = True
            self.is_hardware = False
            logger.info("DisplayService configured in software simulated mode")
            return

        # Attempt hardware driver
        try:
            if "oled" in self.display_type or "ssd1306" in self.display_type:
                self.driver = SSD1306Driver(bus_num=self.i2c_bus, address=self.i2c_address)
            else:
                self.driver = I2CLCDDriver(bus_num=self.i2c_bus, address=self.i2c_address)

            self.is_hardware = True
            self.simulated = False
            logger.info("DisplayService hardware driver initialized (%s)", self.display_type)
        except Exception as e:
            if self.fallback_to_simulated:
                logger.warning(
                    "Physical I2C display not detected (%s). Falling back to software simulated mode.",
                    e,
                )
                self.driver = None
                self.is_hardware = False
                self.simulated = True
            else:
                logger.error("Failed to initialize physical I2C display: %s", e)
                self.driver = None
                self.is_hardware = False
                self.simulated = False

    def is_available(self) -> bool:
        """Return True if display service is enabled and operational (physical or simulated)."""
        return self.enabled and (self.is_hardware or self.simulated)

    def update_idle(self, ip: str = "192.168.4.1") -> None:
        """Render IDLE screen state.

        Line 1: "Piso-WiFi Ready"
        Line 2: "IP: <ip>"
        """
        line1 = "Piso-WiFi Ready"
        line2 = f"IP: {ip}"
        self._render(line1, line2, state="IDLE")

    def update_coin_session(self, remaining_seconds: int, pesos: int) -> None:
        """Render INSERT_COIN screen state.

        Line 1: "INSERT COIN NOW"
        Line 2: "Time: <seconds>s | ₱<pesos>"
        """
        line1 = "INSERT COIN NOW"
        line2 = f"Time: {remaining_seconds}s | ₱{pesos}"
        self._render(line1, line2, state="INSERT_COIN")

    def update_active_session(
        self,
        remaining_time: Union[int, float, str],
        formatted_time: Optional[str] = None,
    ) -> None:
        """Render ACTIVE_SESSION screen state.

        Line 1: "Time Remaining"
        Line 2: "01h 45m" (or formatted from remaining seconds)
        """
        line1 = "Time Remaining"
        if formatted_time is not None:
            line2 = formatted_time
        elif isinstance(remaining_time, str):
            line2 = remaining_time
        else:
            total_seconds = max(0, int(remaining_time))
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            line2 = f"{hours:02d}h {minutes:02d}m"

        self._render(line1, line2, state="ACTIVE_SESSION")

    def update_status(self, line1: str, line2: str = "") -> None:
        """Render custom status text lines."""
        self._render(line1, line2, state="STATUS")

    def clear(self) -> None:
        """Clear the display output."""
        with self._lock:
            self.current_line1 = ""
            self.current_line2 = ""
            self.current_state = "CLEARED"

            if self.driver:
                try:
                    self.driver.clear()
                except Exception as e:
                    logger.debug("Error clearing physical display: %s", e)

            self.history.append({
                "timestamp": time.time(),
                "state": "CLEARED",
                "line1": "",
                "line2": "",
            })
            if len(self.history) > 50:
                self.history.pop(0)

    def get_lines(self) -> Tuple[str, str]:
        """Return the current lines shown on display."""
        with self._lock:
            return self.current_line1, self.current_line2

    def get_rendered_text(self) -> str:
        """Return ASCII representation of the display buffer."""
        with self._lock:
            l1 = self.current_line1.ljust(16)[:16]
            l2 = self.current_line2.ljust(16)[:16]
            return f"+----------------+\n|{l1}|\n|{l2}|\n+----------------+"

    def _render(self, line1: str, line2: str, state: str) -> None:
        """Internal render dispatch handling hardware and software buffers."""
        if not self.enabled:
            return

        with self._lock:
            self.current_line1 = line1
            self.current_line2 = line2
            self.current_state = state

            # Update physical hardware if present
            if self.is_hardware and self.driver:
                try:
                    self.driver.display_lines(line1, line2)
                except Exception as e:
                    logger.error("Physical display update error: %s", e)
                    if self.fallback_to_simulated:
                        self.is_hardware = False
                        self.simulated = True

            # Record in history buffer for diagnostics and testing
            self.history.append({
                "timestamp": time.time(),
                "state": state,
                "line1": line1,
                "line2": line2,
            })
            if len(self.history) > 50:
                self.history.pop(0)

            logger.debug("Display [%s]: L1='%s' | L2='%s'", state, line1, line2)

    def close(self) -> None:
        """Release any hardware bus resources."""
        with self._lock:
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass
                self.driver = None
