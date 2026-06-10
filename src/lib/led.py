"""
led.py -- MIDILAB
Common-anode RGB LED driver with asyncio-friendly animation helpers.

Common-anode wiring: shared anode to 3.3V (via resistors on each cathode).
Cathode pins act as sinks: LOW = ON, HIGH = OFF.
PWM duty is inverted: duty 0 = full brightness, 65535 = off.

Resistor values (at 3.3V supply):
  Red   (Vf ~2.0V): 1k  -- approx 1.3mA
  Green (Vf ~2.1V): 1k  -- approx 1.2mA
  Blue  (Vf ~3.0V): 220R -- approx 1.4mA

Color tuples are (R, G, B) each 0-255.
Global brightness ceiling is applied before every PWM write.
"""

import asyncio
from machine import Pin, PWM

# Color presets (module-level constants -- uppercase by convention for constants)
OFF      = (  0,   0,   0)
RED      = (255,   0,   0)
GREEN    = (  0, 255,   0)
BLUE     = (  0,   0, 255)
WHITE    = (255, 255, 255)
AMBER    = (255,  80,   0)
CYAN     = (  0, 200, 200)
MAGENTA  = (180,   0, 180)
DIM_BLUE = (  0,   0,  40)   # boot/standby indicator

# Mode to color mapping (dict used for O(1) lookup by mode name string)
MODE_COLORS = {
    'scale':          RED,
    'chord':          GREEN,
    'chord_derived':  GREEN,    # breathing green -- handled by start_breathe()
    'combo':          GREEN,    # breathing green/red -- handled by start_breathe()
    'combo_c':        GREEN,    # breathing green/red faster -- handled by start_breathe()
    'chromatic_lab':  WHITE,    # placeholder -- actual mode uses rainbow_cycle() animation
    'patch':          WHITE,
    'menu':           AMBER,
    'config':         MAGENTA,
    'theremin':       CYAN,
    'boot':           DIM_BLUE,
    'ready':          GREEN,
    'saved':          WHITE,
    'error':          RED,
}


class RGBLED:
    """
    Common-anode RGB LED driven by three PWM channels.
    All color methods accept (r, g, b) tuples, each component 0-255.
    """

    def __init__(self, pin_r, pin_g, pin_b, freq=1000, brightness=153):
        # List comprehension: builds the list by evaluating the expression for each pin value
        self._pwm = [PWM(Pin(p, Pin.OUT), freq=freq) for p in (pin_r, pin_g, pin_b)]
        self._brightness  = brightness  # 0-255 global ceiling applied to every write
        self._current     = OFF
        self._mode_color  = DIM_BLUE
        self._blink_task   = None
        self._breathe_task = None
        self._rainbow_task = None
        self.off()

    # Internal helpers

    def _duty(self, component_value):
        """
        Scale a 0-255 component to an inverted 16-bit PWM duty cycle.
        Result: 65535 = fully OFF, 0 = fully ON (inverted because sink logic).
        """
        scaled = int(component_value * self._brightness / 255)
        return 65535 - (scaled << 8)  # << 8 multiplies by 256; shifts 8-bit value into 16-bit range

    def _write(self, r, g, b):
        self._pwm[0].duty_u16(self._duty(r))
        self._pwm[1].duty_u16(self._duty(g))
        self._pwm[2].duty_u16(self._duty(b))

    def _cancel_background(self):
        """Cancel any running blink, breathe, or rainbow animation task."""
        if self._blink_task:
            self._blink_task.cancel()
            self._blink_task = None
        if self._breathe_task:
            self._breathe_task.cancel()
            self._breathe_task = None
        if self._rainbow_task:
            self._rainbow_task.cancel()
            self._rainbow_task = None

    # Public immediate methods

    def set(self, color):
        """Set LED to a color tuple immediately. Cancels any background animation."""
        self._cancel_background()
        self._current = color
        self._write(*color)  # *color unpacks the tuple: _write(r, g, b) not _write((r,g,b))

    def off(self):
        self.set(OFF)

    def set_brightness(self, value):
        """Update global brightness ceiling (0-255) and re-apply current color."""
        self._brightness = max(0, min(255, value))
        self._write(*self._current)

    def set_for_mode(self, mode_key):
        """Set color for a named mode string. Cancels any animation."""
        color = MODE_COLORS.get(mode_key, DIM_BLUE)  # .get with default handles unknown mode names safely
        self._mode_color = color
        self.set(color)

    # Async animation methods

    async def blink(self, color, on_ms=150, off_ms=150, count=3):
        """
        Blink a color N times then restore the previous color.
        Can be awaited or launched as a fire-and-forget task.
        """
        prev = self._current
        for _ in range(count):  # _ is a conventional name for a loop variable you don't use
            self._write(*color)
            await asyncio.sleep_ms(on_ms)
            self._write(0, 0, 0)
            await asyncio.sleep_ms(off_ms)
        self._write(*prev)
        self._current = prev

    async def pulse(self, color, steps=40, period_ms=800):
        """Single fade-in / fade-out pulse. Returns when complete."""
        prev = self._current
        half = period_ms // (2 * steps)
        r, g, b = color  # tuple unpacking into named variables
        for i in range(steps):
            scale = i / steps
            self._write(int(r * scale), int(g * scale), int(b * scale))
            await asyncio.sleep_ms(half)
        for i in range(steps, -1, -1):
            scale = i / steps
            self._write(int(r * scale), int(g * scale), int(b * scale))
            await asyncio.sleep_ms(half)
        self._write(*prev)
        self._current = prev

    async def _breathe_loop(self, color, period_ms):
        """Internal loop for start_breathe(). Cancelled externally by _cancel_background()."""
        steps = 50
        half  = period_ms // (2 * steps)
        r, g, b = color
        while True:
            for i in range(steps):
                scale = i / steps
                self._write(int(r * scale), int(g * scale), int(b * scale))
                await asyncio.sleep_ms(half)
            for i in range(steps, -1, -1):
                scale = i / steps
                self._write(int(r * scale), int(g * scale), int(b * scale))
                await asyncio.sleep_ms(half)

    def start_breathe(self, color=None, period_ms=2000):
        """
        Start a continuous slow breathe animation as a background asyncio task.
        Replaces any previous animation. Stop by calling set() or off().
        """
        self._cancel_background()
        if color is None:
            color = self._mode_color
        # asyncio.create_task() launches a coroutine as a background task -- fire and forget
        self._breathe_task = asyncio.create_task(self._breathe_loop(color, period_ms))

    async def note_flash(self, color=None, duration_ms=40):
        """
        Brief full-brightness spike on note-on -- visual MIDI activity indicator.
        Does not cancel background animations; overlays them momentarily.
        Launch with asyncio.create_task(led.note_flash()) from the note engine.
        """
        if color is None:
            color = WHITE
        self._pwm[0].duty_u16(65535 - (color[0] << 8))
        self._pwm[1].duty_u16(65535 - (color[1] << 8))
        self._pwm[2].duty_u16(65535 - (color[2] << 8))
        await asyncio.sleep_ms(duration_ms)
        self._write(*self._current)  # restore previous color

    async def _rainbow_loop(self, period_ms=8000):
        """
        Continuously cycles through all hues by rotating through the HSV colour wheel.
        One full rotation takes period_ms milliseconds.
        Pure hue rotation at full saturation and brightness -- every colour in sequence.
        """
        steps     = 360
        step_ms   = period_ms // steps
        while True:
            for hue in range(steps):
                # HSV to RGB conversion inline -- hue 0-359, sat=1, val=1
                # Divides the wheel into 6 sectors of 60 degrees each
                h = hue / 60.0
                sector = int(h)           # which 60-degree sector (0-5)
                frac   = h - sector       # fractional position within sector
                p = 0
                q = int(255 * (1 - frac))
                t = int(255 * frac)
                if   sector == 0: r, g, b = 255,   t,   p
                elif sector == 1: r, g, b =   q, 255,   p
                elif sector == 2: r, g, b =   p, 255,   t
                elif sector == 3: r, g, b =   p,   q, 255
                elif sector == 4: r, g, b =   t,   p, 255
                else:             r, g, b = 255,   p,   q
                self._write(r, g, b)
                await asyncio.sleep_ms(step_ms)

    def start_rainbow(self, period_ms=8000):
        """Start continuous rainbow hue cycle. Stop by calling set() or off()."""
        self._cancel_background()
        self._rainbow_task = asyncio.create_task(self._rainbow_loop(period_ms))

    def update_breath(self, breath_cc_value):
        """
        Modulate LED brightness in real time with breath CC value (0-127).
        Called from CCEngine on every breath reading. Does not affect _current color.
        """
        scaled = int(breath_cc_value * self._brightness / 127)
        r, g, b = self._current
        self._pwm[0].duty_u16(65535 - (int(r * scaled / 255) << 8))
        self._pwm[1].duty_u16(65535 - (int(g * scaled / 255) << 8))
        self._pwm[2].duty_u16(65535 - (int(b * scaled / 255) << 8))

    async def error_flash(self, count=5):
        """Fast red blink sequence for error conditions."""
        await self.blink(RED, on_ms=80, off_ms=80, count=count)

    async def saved_flash(self):
        """Three quick white flashes as settings-saved confirmation."""
        await self.blink(WHITE, on_ms=80, off_ms=80, count=3)
        self.set(self._mode_color)
